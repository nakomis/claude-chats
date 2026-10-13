# esp32-embedder

`mxbai-embed-large`, the model conversation-memory embeds every message with, running on an **ESP32-S3**: a dual-core 240 MHz microcontroller with 8 MB of PSRAM, on a board that costs from about £6 ([the one used](https://link.amazon/B00Ka0e27)\*; its camera isn't needed, only the microSD slot). The 335-million-parameter model doesn't fit in the chip's memory, so the weights stream off an SD card, one matrix at a time, on every pass.

It works. On five test texts the S3's embeddings match Ollama's to a cosine similarity of **0.9994–0.9996**. That is exactly what the int8 scheme predicts in numpy, so the microcontroller adds no error of its own.

Since CHATS-4 a pass holds a whole message (up to 512 tokens, Ollama's own limit), with Wi-Fi and TLS running alongside, and every result is checked bit for bit against known-good vectors.

This is spike code (HOME-432 → CHATS-1): proven on the bench, not yet a service. The plan to turn it into an embedding worker that takes over from Cal is in Plane under **CHATS-1**.

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
| `firmware/full/` | Kata's firmware: the whole model, streamed off SD; tokenises the test texts on the device and checks ids and vectors against `mxtest.bin`. `build.sh`, `sign.sh` and `flash.sh` build, sign and flash it; `tools/` is for CI |
| `firmware/layer0/` | One layer from a flash partition, plus the self-tests that found the PIE bug, an SD speed test and a GPIO pull-up probe |
| `firmware/bench/` | int8 matmul benchmark for the S3 (esp-nn) |
| `firmware/wroom-bench/` | int8 dot-product benchmark for the original ESP32 |
| `tok/test_tok.c` | The tokeniser on a tiny made-up vocab: what CI can check without the real one |
| `infra/` | CDK (pnpm): the GitHub Actions roles that publish the firmware, plus `scripts/provision.sh` and `scripts/ota-signing-key.sh` |

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

### Provisioning

Nothing secret is in the build: every board runs the same image, so CI can publish it. What's particular to a board lives in its own `prov` partition (NVS, namespace `kata`), which the firmware reads at boot. Today that's the house Wi-Fi; device identity (the IoT endpoint, Thing name, certificate and key) joins it later. The boards are `blue`, `green` and `red`, and each will be the Thing `kata-{env}-{board}`.

```bash
infra/scripts/provision.sh blue          # NPM_ENVIRONMENT=prod for a prod board
```

This reads the Wi-Fi SSID and password from SSM (`/Nakomis/Wifi/*`, in the sandbox account), builds `firmware/full/certs/kata-sandbox-blue/prov.bin` with ESP-IDF's NVS partition generator, and shreds the CSV it went through. `certs/` is gitignored. `prov.bin` holds the Wi-Fi password in plain text, and so will the board's flash.

A board that isn't provisioned still runs the model and its self-tests; it just says `prov: not provisioned` and skips Wi-Fi.

### Flashing

Images are signed (RSA-3072, the secure boot v2 scheme), USB builds included: once Fleet can update the boards, each one will only take an update signed by the key that signed the image it's running. So build and flash with the scripts, not `idf.py flash`, which would write the unsigned image:

```bash
cd firmware/full
./build.sh                                            # idf.py build, then sign.sh
./flash.sh /dev/cu.usbmodem1101 kata-sandbox-blue     # app + prov
```

`sign.sh` takes the key from the login Keychain (`kata-ota-signing`, account `sandbox` or `prod`), fetching it once from SSM if it isn't there, and checks the result against the committed `ota-signing/{env}.pub.pem`. `flash.sh` writes the bootloader, partition table, OTA data, the signed app and the board's `prov.bin`, never `nvs`. Like tinycrypt's, it doesn't reset the chip first (`--before no_reset`): hold **BOOT**, tap **RST**, release **BOOT**, then run it, or set `BEFORE=default_reset` to let esptool do that over the native USB port. It ends with a watchdog reset, because a plain reset over the native USB port leaves the chip in download mode. Watch the output with any serial monitor; if yours asserts RTS, the chip stays held in reset.

The partition table is tinycrypt's, with the room 16 MB allows: `nvs`, `phy_init`, `otadata`, `prov`, two 4 MB OTA app slots (the app is about 1.1 MB) and a core dump. Rollback is on. A board still on the old single-app layout needs this full flash once.

### Versioning

`firmware/full/version.txt` (`x.y.z`, starting at 0.1.0) labels local builds; ESP-IDF puts it in the app descriptor, and the firmware prints it at boot. It's only a seed. In CI the version comes from the shared deployment tracker (project `kata`): one patch past sandbox's latest, or a minor or major bump with `--bump-minor` or `--bump-major` alone on a line in the PR description. A version already in the bucket is never reused; CI steps past it.

### CI

`.github/workflows/esp32-embedder.yml` runs on every PR and push to main, and does its work only when something under `esp32-embedder/` changed:

- the tokeniser's host test (`tok/test_tok.c`, under ASan and UBSan)
- the firmware, built in `espressif/idf:v5.4.4`, with the image checks from `tools/image-checks.sh`: the right version, rollback and signed updates on, and no per-board strings (no `kata-{env}-` name, no IoT endpoint, and in sandbox, not the Wi-Fi SSID)
- the CDK type-checked and synthesised for both environments
- `CI Status`, the one check to require

On a PR, the build is signed with the sandbox key and published to `s3://nakomis-firmware-sandbox/kata/<x.y.z>/firmware.bin`. When the PR merges, the same unsigned bytes are signed with the prod key and published to `nakomis-firmware-prod`, provided main holds exactly the tree the PR built. Both are recorded in the deployment tracker. CI assumes `nakomis-claude-chats-github-ci-{sandbox,prod}` through GitHub's OIDC (the repo variables `AWS_SANDBOX_ROLE_ARN` and `AWS_PROD_ROLE_ARN`); until they're set, it builds and checks but publishes nothing. Setting up an environment, once:

```bash
cd infra && pnpm install
pnpm run deploy-sandbox                                   # the CI role
NPM_ENVIRONMENT=sandbox scripts/ota-signing-key.sh        # then commit firmware/full/ota-signing/sandbox.pub.pem
```

\* As an Amazon Associate I earn from qualifying purchases.
