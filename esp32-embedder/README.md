# esp32-embedder

`mxbai-embed-large`, the model conversation-memory embeds every message with, running on an **ESP32-S3**: a dual-core 240 MHz microcontroller with 8 MB of PSRAM, on a board that costs from about £6 ([the one used](https://link.amazon/B00Ka0e27)\*; its camera isn't needed, only the microSD slot). The 335-million-parameter model doesn't fit in the chip's memory, so the weights stream off an SD card, one matrix at a time, on every pass.

It works. On five test texts the S3's embeddings match Ollama's to a cosine similarity of **0.9994–0.9996**. That is exactly what the int8 scheme predicts in numpy, so the microcontroller adds no error of its own.

Since CHATS-4 a pass holds a whole message (up to 512 tokens, Ollama's own limit), with Wi-Fi and TLS running alongside, and every result is checked bit for bit against known-good vectors.

It started as spike code (HOME-432 → CHATS-1). Since CHATS-5 it is also a worker: it takes conversation-memory's jobs from SQS, embeds several in each pass, and hands each one to Cal with its vector (see [The worker](#the-worker)). That part is built and tested on the Mac, and still to be brought up on a board.

The board is called **Kata**.

## Results

ESP32-S3 N16R8 (ESP32-S3 CAM board), 32 GB SDHC card in the onboard slot, whole 24-layer model, [CLS] pooling:

| Tokens | cos vs Ollama | Time per pass |
|---|---|---|
| 10 | 0.999560 | 72.2 s |
| 13 | 0.999366 | 72.2 s |
| 13 | 0.999434 | 72.2 s |
| 115 | 0.999472 | 75.5 s |
| 140 | 0.999376 | 78.2 s |
| 391 | 0.999436 | 205 s |
| 512 | 0.999524 | 288 s |

**It is SD-bound.** The onboard slot is wired for 1-bit SDMMC, which tops out at 4.8 MB/s; the firmware gets about 4.3 MB/s. Every pass reads all ~310 MB of weights, so a pass costs about 72 s whatever it carries, until compute catches up at around 150 tokens. Compute alone is ~0.3 s per layer at 13 tokens and ~1.6 s per layer at 115.

So **batching is nearly free.** A pass carrying 140 tokens of queued messages costs 6 s more than one carrying 13. At conversation-memory's load (~900 embeddings a day, ~100 tokens each), one board manages about 2,400 a day. A 4-bit SDIO breakout (CHATS-6) should cut a pass to ~25 s.

## How it works

**The scheme.** Weights are int8 with one scale per output row. Each matmul's input is quantised to int8 per token, except for K = 8 "outlier" input dimensions per matrix, found by calibration, which stay fp32 with their weight columns dequantised. Without that, a handful of huge activation dimensions wreck per-token quantisation (cos 0.9958 instead of 0.9993). Everything else (attention, layernorm, GELU, residuals) is fp32. Token embeddings are int8 per row.

**The kernels** (`firmware/full/main/kernels.inc`):
- int8 dot products use the S3's PIE SIMD (`esp_nn_dot_s8_aligned_esp32s3`, 16 MACs per instruction), with rows split across both cores.
- The input is quantised once per matmul, on both cores, using the FPU's `round.s`.
- GELU is a lookup table folded into the up-projection epilogue.
- Attention splits heads across the cores, with register-blocked loops and a fast `exp`.

**Streaming** (`firmware/full/main/full.c`):
- A pass is a flat list of 576 units: (layer, matrix, 512 KB block of rows).
- While unit *u* computes, unit *u + 1* is already being read.
- An `sd` task on core 0 keeps the card busy, alternating two 16 KB DMA buffers in internal RAM.
- A `cp` task on core 1 copies each chunk to PSRAM and checks its CRC32 against `mxcrc.h`.

**Tokenising** (`tok/`): the device tokenises raw UTF-8 itself, giving the same ids as Ollama. Ollama runs mxbai through llama.cpp's `llama-server`, so `tok.c` is a port of llama.cpp's WordPiece (`llm_tokenizer_wpm_session`, tag b11351, the one Ollama 0.40.3 pins), not HuggingFace's. The differences that matter:
- literal `[CLS]`, `[SEP]` and the other specials in the text become special tokens
- llama.cpp's own Unicode tables: single-codepoint NFD, combining marks dropped, symbols split only below 0x7F
- greedy matching over UTF-8 bytes, with no 100-character `[UNK]` cap
- over 512 tokens, `[CLS]` + the first 510 + `[SEP]`

The Unicode tables (`tok_tables.h`, ~36 KB of flash) are generated from that tag's `unicode-data.cpp`. The vocab comes off the card (`mxbai.bin` section 6) into a hash table in PSRAM: about 575 KB, loaded in 0.15 s. A 140-token message tokenises in 2.3 ms.

It matches `llama-server`'s `/tokenize` id for id on 3,000 real messages (full and cut to 800 characters) and 3,034 stress strings. HF `tokenizers` disagreed on 2 of the 3,000 real messages, and on 908 of 1,034 stress strings.

**Tail quality** (CHATS-8). This compares 3,000 real messages, cut to 800 characters, with the device's arithmetic (`host/emu_bin.py`, read from `mxbai.bin` itself) against Ollama. Mean cos is 0.99938, the 1st percentile 0.99877 and the 0.1th percentile 0.99740.

The minimum is 0.982, on `login`, and the other bad cases are similar: very short messages, pasted hex, `^^`. They all spike an unreserved channel in layer 0's FFN.

None of it matters for search:
- Stored as a device vector among Ollama vectors for everything else, every message is found by its own Ollama query at rank 0, apart from 12 near-duplicates (rank ≤ 2).
- Top-10 neighbours agree about 96–97% of the time between a mixed index and an all-Ollama one.

Recalibrating the outliers on 300 real messages (`host/recal.py`) made the tail *worse* at K = 8 and only slightly better at K = 32. So the 10-text calibration stays, and the corpus doesn't need re-embedding when the embedder changes.

**Whole messages** (CHATS-4). A quarter of real messages are longer than the 168 tokens a pass used to hold. Activations now take ~11 KB per token in PSRAM instead of ~16, so 512 tokens fit:
- the residual stream is fp32, and matmuls add straight into it;
- q, k and v are fp16, and attention's output is written over q;
- the FFN's hidden layer is int8 with a scale per 16 rows (its outlier channels kept exact), requantised in place for the down-projection;
- attention is blocked, with an online softmax, so its scratch in internal RAM is fixed (64 queries × 32 keys) whatever the length.

In numpy (`host/lean.py`) the leaner layout costs nothing measurable against the old one. Internal RAM free after loading went from 34 KB to ~130 KB, which leaves room for Wi-Fi and TLS: with the radio up and an HTTPS request done, the lowest it has been is ~36 KB.

**Core voltage** (`firmware/full/main/vcore.c`). At boot, the firmware raises the chip's core voltage for 240 MHz to at least 1.25 V. See the first lesson below.

**Self-test** (`firmware/full/main/selftest.h`). Every healthy board produces the same output bits for the same text. Each test vector's CRC32 is checked against a committed table, and the board says loudly if any is wrong. The table changes whenever the kernels' arithmetic does: record it again from a known-good board's log with `host/selftest_crcs.py`.

## Lessons (the expensive way)

- **Some ESP32-S3s miscompute at 240 MHz with both cores flat out.** ESP-IDF sets each chip's core voltage for 240 MHz from its factory calibration (eFuse `DIG_DBIAS_HVT` + 3 steps, capped at 1.30 V). On three boards from one batch, the two given 1.218 V and 1.228 V got the odd dot product wrong whenever both cores ran PIE flat out: one garbage value in millions, different every run, no crash, no CRC error. The one given 1.249 V never did. At 160 MHz, or at 240 MHz raised to ≥ 1.25 V, all three are bit-exact (14 and more consecutive long passes each). Wi-Fi, SD activity and interrupts only made it more frequent. It took a day of software theories to find, and a second board to settle; `vcore_boost(1250)` is the fix. Read a board's calibration with `espefuse summary`; never burn eFuses for it.

- **Feed PIE from internal SRAM.** Early on, two cores reading flash-mapped or PSRAM weights with 128-bit vector loads occasionally got garbage dot products (probably the same core-voltage margin as above, found before it was understood). Each core copies a block of weight rows into its own internal buffer, and PIE only reads internal SRAM, which is faster anyway.
- **FatFs seeks are O(file size).** Seeking in a 345 MB file walks the FAT cluster chain, and one 1 MB matrix took 24 s. The firmware builds the cluster map once (`FF_USE_FASTSEEK`, `CREATE_LINKMAP`) and reads raw sectors.
- **SD DMA into PSRAM is ~6× slower** than into internal RAM (0.8 vs 4.8 MB/s), so reads bounce through internal buffers.
- **Internal SRAM is the scarce resource,** not PSRAM. The 100 KB input tile (also attention's scratch), weight-row buffers, bounce buffers and task stacks fight over ~320 KB. Task creation is checked: a failed `xTaskCreate` used to mean a silent hang.
- **Verify the card.** USB card readers have flipped bytes while writing the 345 MB file (3, then 35; another time 36, then 1). Copy through the Mac's built-in slot, remount and compare after copying, and the on-device CRCs catch anything else. A card that has got slow (a pass's "waiting on the card" well above ~60 s) is a card to retire.
- **The original ESP32 (WROOM-32) is ~18× slower** with plain C (76 MMAC/s on two cores), and `MALLOC_CAP_INTERNAL` can hand back IRAM, which faults on byte access.

## Layout

| Path | What |
|---|---|
| `host/mxbai.py` | numpy reference (equals Ollama to cos 1.000000) and int8 emulation, read straight from Ollama's GGUF |
| `host/prep_full.py` | Calibrates the outliers, quantises, checks the full int8 model against Ollama, and writes `mxbai.bin` (345 MB) and `mxtest.bin` (test vectors) |
| `host/gen_crc.py` | Writes `firmware/full/main/mxcrc.h`: the CRC32 of every section of a given `mxbai.bin` |
| `host/prep_layer.py` | Packs layer 0 alone for `firmware/layer0` |
| `tok/` | The tokeniser: `tok.c`/`tok.h` (portable C99), and `gen_tables.py`, which writes `tok_tables.h` from llama.cpp's Unicode data |
| `host/tok.py` | `tok.c` from Python (ctypes); `mxbai.py` tokenises through it |
| `host/ollama_ids.py` | Ground-truth ids for the corpus from a private `llama-server` (the binary Ollama ships) |
| `host/test_tok.py` | `tok.c` against `llama-server`, id for id: the corpus plus random and hand-picked stress strings |
| `host/emu_bin.py` | The device's arithmetic on the Mac, reading the weights back from `mxbai.bin` |
| `host/tail.py` | Device-vs-Ollama cosine over the corpus: mean, minimum, low percentiles |
| `host/recal.py` | Tries choosing the outlier channels on real messages, for several K |
| `host/lean.py` | The whole-message layout (fp16 q/k/v, block-int8 FFN hidden) emulated, against the old one |
| `host/gen_long.py` | Writes `firmware/full/main/longtest.h`: two long texts (391 and 512 tokens) with Ollama's and the emulation's vectors |
| `host/selftest_crcs.py` | Reads a board's log and writes the known-good vector CRCs into `firmware/full/main/selftest.h` |
| `host/serial_log.py` | Logs the board's serial output to a file without resetting it |
| `host/corpus.py` | Samples real messages from local Claude Code transcripts for tokeniser testing (stays local) |
| `firmware/full/` | Kata's firmware: the whole model, streamed off SD; tokenises the test texts on the device and checks ids and vectors against `mxtest.bin`; joins Fleet (commands through SQS, responses and status by HTTPS publish: CHATS-13) and takes updates from it. `build.sh`, `sign.sh` and `flash.sh` build, sign and flash it; `tools/` is for CI |
| `firmware/layer0/` | One layer from a flash partition, plus the self-tests that found the PIE bug, an SD speed test and a GPIO pull-up probe |
| `firmware/bench/` | int8 matmul benchmark for the S3 (esp-nn) |
| `firmware/wroom-bench/` | int8 dot-product benchmark for the original ESP32 |
| `tok/test_tok.c` | The tokeniser on a tiny made-up vocab: what CI can check without the real one |
| `otacmd/` | Fleet's command parser, copied from tinycrypt with its host tests (`test_ota_cmd.py`) |
| `worker/` | Kata the worker (CHATS-5), portable C: SigV4, the SQS client, the credentials provider, age decryption, the canonical text, the results message, the planner and the worker's state machine |
| `test/` | Host tests of `worker/` and of the segmented attention (`make`; mbedTLS and cJSON from ESP-IDF's tree), with AWS's SigV4 test suite (`sigv4/`, Apache 2.0) and age's test kit (`age-testkit/`, C2SP CCTV) |
| `host/convmem-wire/` | Track C's golden fixtures from home-infra `cal/convmem-wire` (TEST-ONLY keys): the contract with Cal |
| `infra/` | CDK (pnpm): the boards as IoT Things, and the GitHub Actions roles that publish the firmware, plus `scripts/provision.sh` and `scripts/ota-signing-key.sh` |

## Running it

You need [ESP-IDF](https://docs.espressif.com/projects/esp-idf/) 5.4.4 (what CI builds with; `vcore.c` reaches into ESP-IDF internals, so other versions need a check), Ollama with `mxbai-embed-large` pulled, and Python 3.11+.

```bash
cd host
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python prep_full.py mxbai.bin mxtest.bin        # a few minutes; prints cos vs Ollama per test
```

`firmware/full/main/mxcrc.h` (the CRC of every section of `mxbai.bin`) and `longtest.h` (the long test texts and their vectors) are generated but committed, so CI can build without the 345 MB model or Ollama. If you remake `mxbai.bin`, regenerate them (`gen_crc.py mxbai.bin ../firmware/full/main/mxcrc.h`, `gen_long.py`) and commit both.

Format a card FAT32 and copy `mxbai.bin` and `mxtest.bin` to its root. **Then eject it, reinsert it and compare the copy byte for byte.** Put the card in the board's slot.

To test the tokeniser on the Mac, start a private `llama-server` on Ollama's GGUF (the command is in `host/ollama_ids.py`), then:

```bash
.venv/bin/python ollama_ids.py && .venv/bin/python test_tok.py
```

The SD pins are those of the ESP32-S3 CAM board's onboard slot (1-bit: CLK 39, CMD 38, D0 40). For a 4-bit breakout, see CHATS-6.

### Identity

Each board is an AWS IoT Thing, `kata-{env}-{board}`: `blue`, `green` and `red` in sandbox, and `green` in prod (the live board, since 13 Oct). The list is `infra/boards.json`, and every deploy declares all of them, so a board left out has its Thing deleted (and the deploy fails while its certificate is still attached). `ClaudeChatsKataDeviceStack` makes the Things, with the attributes Fleet reads (`project=kata`, `deviceType=embedder`, `capabilities=ota,ota-signed`), and one IoT policy for all of them. Through policy variables, a board may connect only as its own Thing, and only use its own topics: it subscribes to `kata/{thing}/commands` and publishes to `kata/{thing}/responses` and `kata/{thing}/status`. There's no `ota-approval`: nobody is there to approve an update, so the board's own checks decide.

The same certificate also buys AWS credentials for CHATS-5: the IoT credentials provider assumes a role (`kata-worker-{env}`, one-hour credentials) through the role alias `kata-{env}`, and the board policy allows `iot:AssumeRoleWithCertificate` on it. The role can do two things. It can take jobs from conversation-memory's ingest queue (receive, delete, change visibility). And it can hand results on to the results queue (send only), where Cal stores them and deletes them only after the commit. Nothing else: no S3, no queue attributes, no dead-letter queues.

Both queues belong to home-infra's `ConversationMemoryIngestStack` (HOME-439), in the same account as the Things: prod boards work the live queue, and sandbox boards are for testing against sandbox's. The stack finds them through the SSM parameters home-infra publishes, `/conversation-memory/{env}/queue-arn` and `/conversation-memory/{env}/results-queue-arn`, so home-infra must be deployed first, or this deploy fails.

### Provisioning

Nothing secret is in the build: every board runs the same image, so CI can publish it. What's particular to a board lives in its own `prov` partition (NVS, namespace `kata`), which the firmware reads at boot: the house Wi-Fi, the IoT endpoint, the Thing name, and the board's certificate and private key. Also, for the worker (CHATS-5): the credentials-provider endpoint and role alias, the ingest and results queues' URLs (`queue_url`, `results_url`, from home-infra's `/conversation-memory/{env}/queue-url` and `results-queue-url`), and the board's age identity (`age_id`). Without any of them the board still runs, joins Fleet and self-tests, and says which is missing instead of starting the worker.

Each board has its **own age key** for decrypting conversation-memory jobs, so a lost board can be revoked on its own. Make it before provisioning:

```bash
infra/scripts/age-identity.sh blue       # --new to replace it, --revoke for a lost or retired board
```

The private key goes to SSM `/kata/{env}/boards/{board}/age-identity` (SecureString), and from there only into the board's `prov.bin`. The public key goes to `/conversation-memory/{env}/age-recipients/kata-{env}-{board}`. The forwarder encrypts each job to Cal's key (`/conversation-memory/{env}/age-recipient`) and to every parameter under `…/age-recipients/`, so adding a board needs no forwarder config, only a reinstall on each Mac. Jobs that were encrypted before then (or for a replaced key) still reach Cal: Kata hands them off as `unembedded/decrypt-failed`. The script is safe to re-run, and it never replaces a key unless you pass `--new`.

`--revoke` is for a lost board. It deactivates the board's IoT certificate and detaches it, which cuts off IoT Core and SQS; credentials already issued last up to an hour. It then deletes the recipient and the key. After that, reinstall the forwarder on every Mac.

**Which AWS account.** `NPM_ENVIRONMENT` alone decides it for every script here (`provision.sh`, `age-identity.sh`, `ota-signing-key.sh` and `sign.sh`): the profile is `nakom.is-{env}`.
- A pre-set `AWS_PROFILE` is ignored. Inheriting one once wrote the prod signing key into the sandbox account.
- To use another profile, set `KATA_AWS_PROFILE`.
- Before touching AWS, each script checks `sts get-caller-identity` against that environment's account (`infra/scripts/aws-env.sh`), says which account it's using, and stops on a mismatch.

```bash
infra/scripts/provision.sh blue          # NPM_ENVIRONMENT=prod for a prod board; --new for a fresh certificate
```

This is tinycrypt's `provision-key.sh`, and it's safe to re-run. It checks the Thing exists, makes a certificate the first time (`aws iot create-keys-and-certificate`), and attaches it to the Thing and the board policy. `--new` retires the old certificates first: use it if a board's key may have leaked. It reads the Wi-Fi SSID and password from SSM (`/Nakomis/Wifi/*`, in the sandbox account), builds `firmware/full/certs/kata-sandbox-blue/prov.bin` with ESP-IDF's NVS partition generator, and shreds the CSV it went through. The certificate and key are kept beside it, so a re-run reuses them. `certs/` is gitignored.

**It's insecure.** The private key is made on the Mac and flashed in plain text, and so are the Wi-Fi password and the age identity: anyone holding the board can read all three (accepted for now, 13 Oct, rather than burning eFuses for NVS encryption). The private key never passes through CloudFormation, though, and a board's certificate can be revoked on its own.

A board that isn't provisioned still runs the model and its self-tests; it just says `prov: not provisioned` and skips Wi-Fi. A board provisioned before identity existed gets Wi-Fi but not Fleet, and says so.

### Flashing

Images are signed (RSA-3072, the secure boot v2 scheme), USB builds included: each board only takes an update from Fleet if it's signed by the key that signed the image it's running. So build and flash with the scripts, not `idf.py flash`, which would write the unsigned image:

```bash
cd firmware/full
./build.sh                                            # idf.py build, then sign.sh
./flash.sh /dev/cu.usbmodem1101 kata-sandbox-blue     # app + prov
```

`sign.sh` takes the key from the login Keychain (`kata-ota-signing`, account `sandbox` or `prod`), fetching it once from SSM if it isn't there, and checks the result against the committed `ota-signing/{env}.pub.pem`. `flash.sh` writes the bootloader, partition table, OTA data, the signed app and the board's `prov.bin`, never `nvs`. Like tinycrypt's, it doesn't reset the chip first (`--before no_reset`): hold **BOOT**, tap **RST**, release **BOOT**, then run it, or set `BEFORE=default_reset` to let esptool do that over the native USB port. It ends with a watchdog reset, because a plain reset over the native USB port leaves the chip in download mode. Watch the output with any serial monitor; if yours asserts RTS, the chip stays held in reset.

The partition table is tinycrypt's, with the room 16 MB allows: `nvs`, `phy_init`, `otadata`, `prov`, two 4 MB OTA app slots (the app is about 1.1 MB) and a core dump. Rollback is on. A board still on the old single-app layout needs this full flash once.

### Fleet

Once it's provisioned, Kata is a device in Fleet (`nakomis-fleet`): it shows up there with its version and a status heartbeat, and its firmware is updated from there, not over USB. USB is for the first flash and for provisioning.

**No MQTT (CHATS-13).** Kata used to hold an MQTT session to IoT Core. Next to the model, internal RAM can't hold that beside the worker's SQS session, so MQTT stepped aside for every SQS phase and came back after: a stop and restart about once a minute, which fragmented internal RAM (the largest free block fell from 18 KB to 10 KB in a few hours, and Fleet updates could no longer start). Now it rides the worker's phases instead, with the same topics, so Fleet and BD-1 see no difference:
- **Commands in.** Fleet publishes on `kata/{thing}/commands` as before. An IoT rule (`kata_{env}_commands`, in the device stack) puts each one on the board's own SQS queue, `kata-{env}-{board}-commands`, beside the ingest queue. Kata reads it at the end of each SQS phase, on the same session, and hands each command to `ota.c`.
- **Responses and the heartbeat out.** These wait in an outbox (`main/fleet.c`). After each phase, once the SQS session has closed, Kata publishes them over HTTPS to its own topics on the IoT data endpoint (`POST /topics/kata/{thing}/responses?qos=1`, and the status with `retain=true`), signed with SigV4 (service `iotdata`) and the worker's role credentials. The role may publish only to the board's own topics (`${credentials-iot:ThingName}`). What can't be published goes back in the outbox for the next phase. At boot, before the self-test, one phase with no jobs sends `{"type":"version",…}` and the first heartbeat, so the board shows up in Fleet within seconds. A board with no worker (no model) still runs such a phase every minute, so it can still be updated.

So a command is answered at the board's next phase: within about 35 s when it's idle, up to a pass (a few minutes) when it's busy. The heartbeat is built every minute and after each step; the latest one is published after the next phase. It's **retained** (CHATS-5), so BD-1 can read a board's last status with IoT Core's `GetRetainedMessage` (HOME-441). The heartbeat carries the state (`checking` during the self-test, `embedding`, `updating` or `idle`), the self-test's verdict, uptime, the free internal RAM, its low-water mark and the free PSRAM, the per-stage low points (`heap_low`), the chip's temperature (`temp_c`), the core voltage `vcore.c` applied (`vcore_mv`), the card's health and the worker's (see [The worker](#the-worker)).

**Commands** are Fleet's ESP32 protocol, which mustn't change. Kata uses tinycrypt's parser (`otacmd/ota_cmd.c`, copied with its host tests) and answers `get_version`, `ping`, `ota_update`, `url_chunk` and `ota_cancel`. Anything else is refused: Kata takes no reboot, wipe or settings from the cloud.

**An update** (`main/ota.c`, tinycrypt's `ota.c` without the approval step):
1. Kata waits for the pass in progress to end (up to 7 minutes, as Fleet's presigned URL lasts 10).
2. It downloads the image over HTTPS into the spare slot, hashing it as it goes, and reports `ota_progress` every 10%.
3. It refuses the image unless the SHA-256 is the one Fleet sent, the signature is by the key that signed the running image, the image says it's `kata` at that version, and that version is newer than the one running.
4. It switches slots and restarts.

A failure is reported as `ota_error` with the reason, and Kata backs off before taking another update (30 s, doubling to 15 minutes). The same URL delivered twice is ignored, as IoT Core's redeliveries are.

**Memory** (CHATS-4) is why an update waits. Between passes, the model holds about 164 KB of internal SRAM: the 100 KB input tile, two 16 KB weight-row buffers and two 16 KB SD bounce buffers. Wi-Fi and MQTT's TLS session fit beside them. A second TLS session for the download, plus the flash writes, wouldn't leave a safe margin. So the update borrows those buffers (`main/model.h`): it frees them once the pass ends, downloads, and either restarts or allocates them again. If internal RAM is too fragmented by then for the 100 KB tile, Kata restarts, which loses nothing. The model is loaded before Wi-Fi starts, so the tile still finds 100 KB in one piece. mbedTLS works in PSRAM.

Here's where internal RAM should land. The image itself costs ~6 KB more static RAM than before Fleet (DIRAM 98.2 → 104.6 KB). The rest are estimates from CHATS-4's measurements, still to be confirmed on a board with `net_heap` and the heartbeat:

| When | Internal free (expected) |
|---|---|
| model loaded | ~88 KB |
| Wi-Fi up | ~48 KB |
| MQTT connected (task stack 6 KB, buffers 2.5 KB, the TLS and socket structures) | ~34 KB |
| lowest, an HTTPS request with MQTT up | ~16 KB |
| lowest during an update, with the buffers lent (less the 8 KB update task, the download's TLS and buffers) | ~175 KB |

**Rollback.** A new image starts on probation (`ESP_OTA_IMG_PENDING_VERIFY`). It's kept only when both of these hold, within 30 minutes of boot:
- **It's healthy.** The self-test is the health check, and it must say PASSED: all seven tests run, every vector bit for bit what `selftest.h` records, and no CRC or tokeniser mismatches. There's one fallback, for a build whose arithmetic changed and whose CRCs were zeroed until they're recorded again. That build's verdict is NOT VERIFIED, and it counts as healthy only if every unrecorded vector is within cos 0.999 of Ollama. A healthy board gives 0.99937–0.99978.
- **It has reached IoT Core.** Its first HTTPS publish has gone through (CHATS-13; it was MQTT's subscription to its commands). Otherwise it couldn't be updated again.

A failed self-test rolls back at once. A crash, or the deadline passing, rolls back too. The old image then reports `ota_error {message: "rolled back"}`, and a kept image reports `ota_complete`. The self-test takes about 15 minutes at 240 MHz, but the board publishes its version within seconds of booting (one phase with no jobs, before the self-test). Fleet sees the new version straight away, and a rollback afterwards still shows as one. Updates sent while an image is on probation are refused.

To check rollback, build with `CONFIG_KATA_OTA_FAIL_SELFTEST` (menuconfig, "Kata"): the image passes everything, then reports its self-test as failed. CI's image checks refuse to publish such a build, so it's published by hand to sandbox only, and never promoted.

**Not covered:** the 345 MB `mxbai.bin` on the SD card. A new model still means swapping the card.

### The worker

Kata takes over conversation-memory's embedding from Cal (CHATS-5). The design is in Plane (CHATS-5, 13 Oct): **custody hand-off through a results queue**. Kata receives a job from the ingest queue, embeds it, and sends a results message to a second queue. That message holds the original ingest body byte for byte (still age-encrypted, and still carrying the forwarder's signature), plus Kata's vector and tags. Only once SQS has accepted it does Kata delete the ingest job. Cal decrypts the envelope with its own key, stores the row with Kata's vector, commits, and only then deletes the result. **A job is always held by at least one queue until its row is committed.** Kata never sees Postgres, never touches S3 and never encrypts anything.

The state machine is portable C in `worker/` (`worker.c`), tested on the Mac against a fake SQS. The board's side is `firmware/full/main/kata_worker.c`. It runs in `app_main`'s task once the self-test is done. Each step is a network phase, then at most one pass.

**The network phase.** Internal RAM allows one TLS session at a time (CHATS-4; the spike measured 32.7 KB as the lowest free with Wi-Fi and one HTTPS session). So the phase works like this:
1. A phase won't start with under 24 KB of internal RAM free.
2. If the credentials expire within ten minutes, Kata fetches new ones from the IoT credentials provider. That's mTLS with the board's certificate and the role alias `kata-{env}`, about once an hour, and the connection is closed straight after.
3. Kata opens one HTTPS session to `sqs.eu-west-2` and keeps it alive across the phase's requests (`worker/sqs.c`: SQS's JSON protocol, signed with SigV4, `worker/sigv4.c`).
4. **Hand-off.** Kata sends the finished results with `SendMessageBatch`. A batch holds at most 10 messages and 256 KiB **in total**, so a big result (one carrying a `too-long` body) goes alone. Then it deletes, with `DeleteMessageBatch`, only the ingest jobs SQS accepted. A result SQS refused stays in the pool. Its job stays hidden (visibility extended), and no new pass starts until it's through.
5. **Top-up.** `ReceiveMessage` (visibility 900 s; a 20 s long poll only when the pool is empty) runs while the pool holds fewer than 150 tokens and fits its PSRAM budget. Each job is sorted as it arrives:

| The job | What Kata does |
|---|---|
| an S3 claim check | hands it to Cal unembedded, `claim-check` |
| delivered for the third time or more | `retries`: it may be what crashed Kata |
| a body over 40 KB | `too-long`: more than Kata's PSRAM can take |
| arrives while Kata is sick | `kata-unhealthy`: Cal embeds it at once |
| won't decrypt with this board's key, the uuids disagree, or it isn't a job | `decrypt-failed`: Cal decides. This includes jobs encrypted before the forwarder knew this board |
| delivered for the second time | pooled, but embedded in a pass of its own |
| anything else | decrypted (age, `worker/age.c`), made canonical, tokenised, pooled |
| not an envelope Cal could read | left alone: no result could carry it, so it reaches the ingest DLQ |

6. Kata extends the visibility of any pooled job that could come back before the next phase, and reads Fleet's commands from its commands queue (CHATS-13). Then it closes the session.
7. It publishes the outbox (responses, then the latest heartbeat) over HTTPS to IoT Core, on a session of its own, closed straight after. With nothing to publish, there's no session.

When the queue is empty, a cycle is a 20 s long poll and at least 15 s between phases. However busy the queue, phases are at least 15 s apart: each costs a TLS handshake or two.

**Strict JSON.** Kata reads an envelope, and the job inside it, at least as strictly as Cal's serde will:
- nothing but whitespace after the value, and no duplicate keys
- the fields Cal requires, with the types it requires (`v` an integer, `message_uuid` a string, and so on)
- at most 32 levels of nesting, checked before parsing (cJSON recurses once per level, on a 16 KB stack; the firmware also builds cJSON with `CJSON_NESTING_LIMIT=32`)

Otherwise a result would land in the results DLQ after its ingest job was deleted. A body that fails these checks isn't an envelope (it's left for the ingest DLQ), and a job that fails them goes to Cal unembedded (`decrypt-failed`). Unknown fields, such as the forwarder's signature, are fine, and pass through untouched.

**Memory for JSON.** On the board, `malloc` is internal RAM only (`CONFIG_SPIRAM_USE_CAPS_ALLOC`), and the network needs that. So cJSON allocates through `kw.h`, which means PSRAM, and the worker allocates only through `kw.h` (`make lint` checks). A parse that fails for want of memory is told apart from a bad body (`kw_alloc_failures`): that job is dropped from the pool and comes back later, rather than being judged. Decrypted plaintext is zeroed before it's freed, as is the age identity's text once parsed.

**The canonical text** (`worker/prep.c`) is what Cal embeds, exactly:
1. Each NUL becomes the two characters `\0` (`sanitise_nuls`; Kata rewrites the `\u0000` escape in the job's JSON before parsing it).
2. The text is cut to 800 characters (Unicode scalar values, not bytes; `ollama::truncate`).
3. It's tokenised. Over 512 tokens it keeps `[CLS]` + the first 510 + `[SEP]`, as Ollama does.

**The pass.** PR #21's planner (`worker/batch.c`) takes the oldest job and fills the pass up to about 150 tokens, the point where the card stops being the bottleneck. A job on its second delivery goes alone. The pass carries the messages as segments: positions restart in each, and attention stays within each (PR #21's segmented kernels). So every message gets the same bits it would in a pass of its own (`test/test_att.c`). A pass must come back clean:
- **A section CRC mismatch aborts it.** None of its vectors are delivered, and its jobs go to Cal as `kata-unhealthy` (cheap cards, replaced when they die: CHATS-5's card policy).
- A non-finite vector, or a raw norm outside Cal's 10–30 band (real `[CLS]` rows measure 15–19), makes it bad too.
- Vectors go **raw**, never normalised: Cal normalises them, and raw bits can be compared between boards.

**Sick.** Three bad passes in a row make Kata sick. So does a failed boot self-test: a board whose self-test failed outside probation still works the queue, sick. A sick Kata hands every job to Cal straight away (Martin, 13 Oct), so nothing waits for the 36 h overflow. Every 30 minutes it runs a health check: the five short self-test texts, each in a pass of its own as at boot, bit for bit against `selftest.h` (about 6 minutes). When that passes, it's well again. One batched pass would take 75 s, and PR #21's test shows a segment gets the same bits as a pass of its own. But that's proven on the Mac, not yet on the chip, and if it weren't true a sick board would never recover.

**Updates.** An `ota_update` parks the worker first. The worker hands off what it has finished, keeping undelivered results hidden while it retries, and gives the rest back to the queue (visibility 0), so no job is held through an update (CHATS-10's criterion 4). If the update fails, the worker carries on. A job given back comes round again with one more receive, so a second update straight after the first can push a job to its third delivery, which sends it to Cal unembedded.

**Visibility.** If extending a pooled job's visibility fails, no pass starts until it goes through, so a job can't come back to someone else mid-pass. A job whose extension is refused (its handle went stale) leaves the pool.

**A watchdog** restarts a worker that hasn't stepped for 15 minutes. Nothing is lost: a job is only deleted once its result is on the results queue. Steps aren't progress, though, so (CHATS-17) the worker also keeps `stalled_s`: how long it has had work (jobs held, or a receive that brought some) without a pass or a hand-off, worked out from the clock when it's read, so it rises even if nothing is stepping the worker. The watchdog restarts the board once that passes an hour.

**Crashes reboot (CHATS-17).** On 20 Oct 2026 an SD read on kata-prod-green timed out twice running (`sdmmc_read_sectors_dma ... 0x107`); `sd_task` aborted, and with the bring-up setting `PANIC_PRINT_HALT` the board halted for 28 h, heartbeat frozen, until its power was cut (her serial log on Cal, `/var/log/kata/kata.log`, has it). Now every SD read (`sd_sectors`, both the background reader and the direct reads) gets four more tries, 10 ms to 1 s apart, before the abort; a panic writes its core dump to flash and reboots 30 s later (`PANIC_PRINT_REBOOT`, `PANIC_REBOOT_DELAY_SECONDS`); and after a crash the heartbeat carries `crash: {task, pc, bt}` from that dump (after an `abort()` the pc is always the abort, so `bt`'s first frames say who called it; after a task-watchdog panic the task is whichever the interrupt landed in, not the one that stopped feeding it). The 20 Oct dump used 26.5 KB of the 64 KB partition. **A crash that recurs at once** (a dead or unseated card dies in `model_init`, before Wi-Fi) would otherwise loop forever unreported: after three crashes in a row (counted in RTC memory, cleared by a passing self-test, a power cut or an update's restart) the board boots without the model, so Wi-Fi, Fleet, the heartbeat (`crashes_in_a_row`) and `/json` come up and say so. A **guard task** (priority 7) feeds the hardware task watchdog (60 s, idle tasks not watched) only while the worker's own watchdog, an `esp_timer` callback, keeps running, so a wedged `esp_timer` task reboots the board too. To read a core dump: stop `kata-serial-log.service` on Cal first (it holds the port), `esptool.py --before no_reset --after watchdog_reset read_flash 0x820000 0x10000 coredump.bin`, start it again, and decode with `espcoredump.py` against the same build's ELF.

**The results message** is `convmem_wire::KataResult` v1 (home-infra `cal/convmem-wire`): `kind`, `message_uuid`, the `envelope` spliced in verbatim, `embedder`, `vector` (base64 of 1024 little-endian f32), `tokens`, `truncated_chars`, `reason`, `board` (the Thing name) and `fw` (the firmware version), plus `pass` and `ingest` for the logs. Cal stores `embedder`, `board` and `fw` with the row.

**The embedder tag** is `EMBEDDER_TAG` in `selftest.h`, `esp32s3-mxbai-int8-v1`. Its `-vN` goes up exactly when the self-test's CRC table does, which is when the kernels' arithmetic changes. `test/test_tag.c` pins the table's CRC to the tag, so CI fails if the table changes and the tag doesn't. Cal accepts `esp32s3-mxbai-int8*`.

**Status** (HOME-441), in every heartbeat:

| Field | What |
|---|---|
| `version`, `uptime_s`, `selftest` | firmware, seconds since boot, the self-test's verdict |
| `card` | `wait_last_s` and `wait_median_s` (the last 15 passes) waiting on the card, `crc_mismatches` and `read_retries` since boot. A healthy card waits about 60 s on the 1-bit slot. A slow card wait means the card *or the board*: swap the card first |
| `worker` | `state` (`idle`, `receiving`, `embedding`, `handing-off`, `checking`, `sick`, `parked`), `sick`, `embedder`, `pool`, `passes` and `messages` (`boot`, `today`), `unembedded` (by reason), `bad_passes`, `last_pass` (`n_seg`, `tokens`, `ms`, `card_wait_ms`, `at`), `last_handoff_at`, `failures` (`phase`, `send`, `delete`, `visibility`), `stalled_s` (CHATS-17), `last_error` |
| `crash`, `crashes_in_a_row` | after a reset by panic or watchdog: `task`, `pc` and the first `bt` frames from the core dump; and how many crashes in a row (at 3, no model: CHATS-17) |
| `internal_lowest`, `temp_c`, `vcore_mv` | internal RAM's low point since boot, the chip's temperature, the core voltage applied |

**Memory, measured (CHATS-13, 14 Oct 2026).** `heapwatch.c` keeps a low point per stage (`heap_low` in the heartbeat: `creds`, `sqs`, `iotpub`, `pass`, `idle`). With MQTT (Green, prod, 0.1.10, 2.8 h): lowest 9.4 KB, in the SQS phase and MQTT's handshake, and the largest free block shrinking from 18 KB to 10 KB. Without (Red, sandbox, the CHATS-13 spike): lowest 13.8 KB, in the HTTPS publish, and the largest block 21.5 KB throughout. Every TLS handshake still takes about 20 KB of internal RAM for a moment, although mbedTLS is in PSRAM. Internal RAM is three heaps (`heapwatch_map`, the spike): the main DRAM heap (~288 KB, the model's buffers, Wi-Fi and the rest packed in, ~11 KB free at its end), a ~21.5 KB region the boot stage hands back, and 8 KB of RTC fast memory. So no block is ever bigger than ~21.5 KB.

**Memory, expected before CHATS-5's bring-up** (kept for the reasoning):
- Static internal RAM is DIRAM 105.8 KB, up 1.2 KB on the Fleet build.
- During an SQS phase MQTT is down, which frees its task stack (6 KB) and its TLS session. The phase adds the HTTP client's 2 KB receive and 3 KB transmit buffers and a socket; mbedTLS is in PSRAM.
- So the low point should sit near the Fleet build's figure with MQTT up (~34 KB), and no lower than the spike's 32.7 KB. That's 8 KB or more above the 24 KB floor the worker checks.
- Bodies, results, ids, responses and parsed JSON all live in PSRAM. A receive's peak is about five times its bodies (the response, cJSON's tree, the copies, the results built from them). So the pool's budget is a fifth of the PSRAM free after loading less 128 KB (for mbedTLS: one session measured 46 KB), capped at 1 MB. A receive asks for at most budget ÷ 40 KB messages and keeps no more than it asked for. The SQS client gives its buffers back after any call over 64 KB.
- **The model leaves little PSRAM.** Its buffers take about 7.4 of the 8 MB (measured on Green, 13 Oct): the worker starts with about 366 KB free, a budget of about 47 KB. So Kata takes bodies of at most 40 KB (`WORKER_WORST_BODY`), and the forwarder sends at most 32,768 bytes of ciphertext inline (home-infra's `max-inline-bytes`, clamped by `convmem_wire::MAX_INLINE_BYTES_CEILING`). Anything bigger goes by S3 claim check, which Kata hands to Cal unembedded. In 30 days of conversation-memory (31,140 messages) the 99th percentile was 11.7 KB of content and the largest 66 KB, so about one message in 200 goes that way. A body over 40 KB that arrives anyway (a forwarder still on the old limit) goes to Cal as `too-long`.

**Bring-up** on a sandbox board, in order:
1. Deploy home-infra #356 (the results queue and its SSM parameters) to sandbox, then this repo's `ClaudeChatsKataDeviceStack` (`pnpm run deploy-sandbox`). The board policy's new `iot:RetainPublish` **must be live before the firmware**, or IoT Core drops a board that publishes a retained status.
2. `infra/scripts/age-identity.sh blue`, then `infra/scripts/provision.sh blue`. Check it doesn't warn about missing queues.
3. `firmware/full/build.sh`, then `flash.sh` with the board's prov.
4. Watch the boot log: the self-test, then `worker: starting as kata-sandbox-blue, tag esp32s3-mxbai-int8-v1; pool budget …`, the first phase's `heap worker: after an SQS phase` line, and `fleet: connected` after each phase.
5. Send a ~40 KB inline job (the largest the forwarder sends) and read that phase's `worker: phase moved up to …` and `heap …` lines. The internal free and its lowest-ever point show what the biggest body costs; nothing of it should come out of internal RAM.
6. Send a test job, encrypted to the board's key and Cal's, to the sandbox ingest queue. Check its result on the results queue: `kind: embedded`, and a vector within cos 0.999 of Ollama's for the same text. Then soak it, and compare bit for bit with a spare board.

### On the LAN: GET /json (CHATS-16)

A rare "is everything OK?" check, answered by the board itself rather than by its heartbeat. `https://kata.home.nakomis.com` (home-infra, HOME-513) is Leia's nginx: it checks the client certificate, terminates TLS, serves the page, and asks the board for `GET /json` over plain HTTP on the LAN. TLS on the board would cost a session's worth of internal RAM, which the SQS phases need. The board serves nothing else: no HTML, no files.

`firmware/full/main/statusd.c` is one task, with its stack in PSRAM, one connection at a time and one route. The HTTP itself (routing, response heads, who may ask) is `worker/httpreq.c`, host-tested in `test/test_httpreq.c`.

**What comes back.** The heartbeat's status, built at that moment (`fleet_status_build`, the same fields as `kata/<thing>/status`, which since CHATS-16 include the board's `ip` and Wi-Fi `mac`, for finding it on the LAN and reserving its address), plus a `now` object:

| Field | Meaning |
|---|---|
| `now.state` | `sqs` during an SQS phase, otherwise the model's `idle`, `embedding`, `checking` (the self-test or a health check) or `updating` |
| `now.model`, `now.worker` | the model's state, and the worker's (`idle`, `receiving`, `embedding`, `handing-off`, `sick`, `parked`, or `off`) |
| `now.in_phase` | an SQS phase is under way |
| `now.pass` | while a pass runs: `unit` of `units` (layer × matrix × row block), `elapsed_s`, and `eta_s` (elapsed scaled by the units left; the card makes units uneven, so it's rough) |
| `now.served_at` | Unix time, once SNTP has set the clock |
| `now.requests` | `served`, `busy` (503s) and `refused` since boot |
| `now.build_ms` | how long the reply took to build (not to send) |

**A pass pauses for it.** The task (priority 6) outranks the compute tasks (5), so a request pre-empts the pass for the milliseconds a reply takes, and the pass carries on exactly where it was. The vectors don't change.

**During an SQS phase** internal RAM runs lowest (about 12.8 KB free on Green, 21 Oct 2026). Then, below 20 KB free, the reply is a 503 with `Retry-After: 5` and `{"busy":"sqs","retry_after_s":5,"internal_free":N}`: still an answer from the board, just a short one. `busy` is `sqs` almost always; `gate`, `status` or `memory` mean the phase lock, the heartbeat's lock or PSRAM wasn't free. No phase starts while a reply is being built (`kata_worker_gate`), and a phase waits at most 5 s for one before giving up that turn.

One connection is served at a time, with up to four waiting. The request head must arrive within 3 s in all, however it trickles in; a peer that's refused or too slow is reset rather than closed, so it leaves no `TIME_WAIT` behind in internal RAM. `statusd` starts once Wi-Fi is up at boot: a board that boots offline doesn't serve `/json` until it's restarted.

**Who may ask.** The addresses in the `prov` key `status_allow` (separated by commas or spaces, at most four; `provision.sh` writes it from SSM `/kata/{env}/status-allow`), or with no such key, any peer on the board's own subnet. Only a missing key means the subnet: a list that's there but doesn't parse (or is empty, or can't be read) refuses everyone, and `provision.sh` stops on any SSM error other than the parameter not existing. A peer that may not ask is reset without a reply. Anything but `GET /json` gets 404, 405 (`HEAD` included) or 400.

```bash
curl -s http://<board>/json | jq .now
```

### Versioning

`firmware/full/version.txt` (`x.y.z`, starting at 0.1.0) labels local builds; ESP-IDF puts it in the app descriptor, and the firmware prints it at boot. It's only a seed. In CI the version comes from the shared deployment tracker (project `kata`): one patch past sandbox's latest, or a minor or major bump with `--bump-minor` or `--bump-major` alone on a line in the PR description. A version already in the bucket is never reused; CI steps past it.

### CI

`.github/workflows/esp32-embedder.yml` runs on every PR and push to main, and does its work only when something under `esp32-embedder/` changed:

- the tokeniser's host test (`tok/test_tok.c`, under ASan and UBSan)
- Fleet's command parser: tinycrypt's tests against the copy in `otacmd/` (with cJSON, under ASan and UBSan)
- the worker (`test/`, in the `espressif/idf:v5.4.4` image so mbedTLS and cJSON are the firmware's own): SigV4 against AWS's test suite, age against its test kit and track C's fixtures, the SQS and credentials clients and the worker against fakes, the results message against the fixtures, the embedder tag's pin, and PR #21's planner and segmented attention, under ASan, UBSan and LeakSanitizer
- the firmware, built in `espressif/idf:v5.4.4`, with the image checks from `tools/image-checks.sh`: the right version, rollback and signed updates on, no rollback test build, and no per-board strings (no `kata-{env}-` name, no IoT endpoint, and in sandbox, not the Wi-Fi SSID)
- the CDK type-checked and synthesised for both environments
- `CI Status`, the one check to require

On a PR, the build is signed with the sandbox key and published to `s3://nakomis-firmware-sandbox/kata/<x.y.z>/firmware.bin`. When the PR merges, the same unsigned bytes are signed with the prod key and published to `nakomis-firmware-prod`, provided main holds exactly the tree the PR built. Both are recorded in the deployment tracker. CI assumes `nakomis-claude-chats-github-ci-{sandbox,prod}` through GitHub's OIDC (the repo variables `AWS_SANDBOX_ROLE_ARN` and `AWS_PROD_ROLE_ARN`); until they're set, it builds and checks but publishes nothing. Setting up an environment, once:

```bash
cd infra && pnpm install
pnpm run deploy-sandbox                                   # the CI role, and the boards' Things and policy
NPM_ENVIRONMENT=sandbox scripts/ota-signing-key.sh        # then commit firmware/full/ota-signing/sandbox.pub.pem
scripts/provision.sh blue                                 # for each board: its certificate and prov.bin, then flash.sh
```

\* As an Amazon Associate I earn from qualifying purchases.
