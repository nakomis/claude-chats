"""Pack the whole of mxbai-embed-large for the ESP32-S3 (HOME-432 step 3), plus test vectors.

Same scheme as prep_layer.py, for all 24 layers: weights int8 per output row; matmul
inputs int8 per token except K outlier input dims (fp32, with their weight columns
dequantised to fp32). Token embeddings int8 per row. Everything else fp32.

mxbai.bin (little-endian; every section starts on a 512-byte sector boundary so the S3
can read sections straight off the card):
  sector 0: header  '<4sIIIIIII' magic 'MXB1', version 1, n_layer, K, n_vocab, d, ff, max_pos
            then '<I' * N_GLOBAL global section offsets, then '<I' * n_layer layer offsets,
            then CRC32s (zlib) of each padded global section and each layer block, same order
  globals: tok_q int8 [V][D], tok_s f32 [V], pos f32 [P][D], type0 f32 [D],
           embd_norm_w f32 [D], embd_norm_b f32 [D], vocab (NUL-separated WordPiece strings)
  layer block: sector of '<I' * 34 section offsets relative to the block, then per matrix
           [attn_q, attn_k, attn_v, attn_output, ffn_up, ffn_down] wq, ws, bias, oidx u16,
           wo f32 rows x K; then aon_w, aon_b, lon_w, lon_b

mxtest.bin: '<4sII' magic 'MXT1', n_tests, d; per test: '<I' n_tok, ids u32[n_tok],
            emb_emu f32[D] (numpy emulation of this scheme), emb_ref f32[D] (fp32 = Ollama)
"""
import struct
import sys
import zlib

import numpy as np

import mxbai as M

K = 8
SECTOR = 512
MATS = ['attn_q', 'attn_k', 'attn_v', 'attn_output', 'ffn_up', 'ffn_down']
CALIB = ["The cat sat on the radiator all afternoon.", "git push failed: permission denied (publickey)",
         "Traceback (most recent call last): KeyError: 'id'", "Shall I carry on with the kernel?",
         "ESP32-S3 N16R8 with octal PSRAM at 80 MHz", "Plane work item HOME-432 updated with results.",
         "Write in British English throughout: colour, licence, realise.",
         "SELECT id, content FROM messages WHERE created_at > now() - interval '1 day' LIMIT 50;",
         "Kappa is Chi and Tau's son: very dark, long fluffy fur, green-yellow eyes.",
         "I've pushed the branch and opened PR #9; the review found three issues, all fixed."]
TESTS = ["The cat sat on the radiator all afternoon.",
         "Shall I carry on with the kernel?",
         "git push failed: permission denied (publickey)",
         "Martin asked whether a cheap ESP32-S3 could take over the embedding job that Cal does today. "
         "The plan streams the weights of mxbai-embed-large from an SD card, one matrix at a time, and "
         "keeps a batch of messages in PSRAM so that each weight is used many times. Earlier benchmarks "
         "measured about one billion multiply-accumulates per second across both cores, which suggests "
         "roughly thirty seconds for a typical message. This test checks the arithmetic of one encoder "
         "layer against a reference computed on the Mac.",
         "  File \"/usr/lib/python3.11/site-packages/requests/adapters.py\", line 519, in send\n"
         "    raise ConnectionError(e, request=request)\nrequests.exceptions.ConnectionError: "
         "HTTPConnectionPool(host='localhost', port=11434): Max retries exceeded with url: /api/embed "
         "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x10f3c2d90>: "
         "Failed to establish a new connection: [Errno 61] Connection refused'))"]

# 1. outlier input dims per matmul per layer, from max |x| over the calibration texts (fp32 pass)
stats = {}
orig = M.mm


def rec(x, name, int8):
    stats.setdefault(name, []).append(np.abs(x).max(0))
    return orig(x, name, False)


M.mm = rec
for t in CALIB:
    M.embed(t)
M.mm = orig
OUT = {n: np.sort(np.argsort(-np.max(np.stack(v), 0))[:K]) for n, v in stats.items()}

# 2. quantise
Q = {}
for i in range(M.LAYERS):
    for m in MATS:
        name = f'blk.{i}.{m}'
        wq, ws = M.q_rows(M.T[name + '.weight'])
        o = OUT[name]
        Q[name] = (wq, ws.astype(np.float32), M.T[name + '.bias'].astype(np.float32), o,
                   (wq[:, o].astype(np.float32) * ws[:, None]).astype(np.float32))
tok_q, tok_s = M.q_rows(M.T['token_embd.weight'])
tok_s = tok_s.astype(np.float32)


def mm_emu(x, name, int8):
    wq, ws, b, o, wo = Q[name]
    xr = x.copy()
    xr[:, o] = 0
    s = np.abs(xr).max(1, keepdims=True) / 127
    s[s == 0] = 1
    xq = np.round(xr / s).astype(np.int32)
    return (xq @ wq.astype(np.int32).T) * s * ws[None, :] + x[:, o] @ wo.T + b


def embed_emu(ids):
    n = len(ids)
    x = tok_q[ids].astype(np.float32) * tok_s[ids, None] + M.T['position_embd.weight'][:n] + M.T['token_types.weight'][0]
    x = M.ln(x, M.T['token_embd_norm.weight'], M.T['token_embd_norm.bias'])
    M.mm = mm_emu
    for i in range(M.LAYERS):
        x = M.layer(x, i, int8=True)
    M.mm = orig
    return x[0].astype(np.float32)


tests = []
for t in TESTS:
    ids = M.TOK.encode(t).ids
    ref = M.embed(t).astype(np.float32)
    emu = embed_emu(ids)
    print(f"{len(ids):3d} tok: int8 emulation vs fp32/Ollama cos {M.cos(emu, ref):.6f}")
    tests.append((ids, emu, ref))

# 3. pack
def pad(b):
    return b + b'\0' * (-len(b) % SECTOR)


f = M._r.fields['tokenizer.ggml.tokens']
vocab = []
for i in f.data:
    t = bytes(f.parts[i]).decode()
    if t.startswith('▁'):
        t = t[1:]
    elif not (t.startswith('[') and t.endswith(']')):
        t = '##' + t
    vocab.append(t)
V, D = M.T['token_embd.weight'].shape
P = M.T['position_embd.weight'].shape[0]
glob = [tok_q.tobytes(), tok_s.tobytes(), M.T['position_embd.weight'].astype(np.float32).tobytes(),
        M.T['token_types.weight'][0].astype(np.float32).tobytes(),
        M.T['token_embd_norm.weight'].astype(np.float32).tobytes(),
        M.T['token_embd_norm.bias'].astype(np.float32).tobytes(),
        b'\0'.join(v.encode() for v in vocab) + b'\0']

layers = []
for i in range(M.LAYERS):
    secs = []
    for m in MATS:
        wq, ws, b, o, wo = Q[f'blk.{i}.{m}']
        secs += [wq.tobytes(), ws.tobytes(), b.tobytes(), o.astype(np.uint16).tobytes(), wo.tobytes()]
    for nm in ('attn_output_norm', 'layer_output_norm'):
        secs += [M.T[f'blk.{i}.{nm}.weight'].astype(np.float32).tobytes(),
                 M.T[f'blk.{i}.{nm}.bias'].astype(np.float32).tobytes()]
    offs, pos = [], SECTOR
    for s in secs:
        offs.append(pos)
        pos += len(pad(s))
    layers.append(pad(struct.pack(f'<{len(offs)}I', *offs)) + b''.join(pad(s) for s in secs))

hdr_len = SECTOR * 2
pos, goff = hdr_len, []
for g in glob:
    goff.append(pos)
    pos += len(pad(g))
loff = []
for L in layers:
    loff.append(pos)
    pos += len(L)
hdr = struct.pack('<4sIIIIIII', b'MXB1', 1, M.LAYERS, K, V, D, M.T['blk.0.ffn_up.weight'].shape[0], P)
hdr += struct.pack(f'<{len(goff)}I', *goff) + struct.pack(f'<{len(loff)}I', *loff)
crcs = [zlib.crc32(pad(g)) for g in glob] + [zlib.crc32(L) for L in layers]
hdr += struct.pack(f'<{len(crcs)}I', *crcs)
assert len(hdr) <= hdr_len
out = sys.argv[1] if len(sys.argv) > 1 else 'mxbai.bin'
with open(out, 'wb') as fo:
    fo.write(hdr + b'\0' * (hdr_len - len(hdr)))
    for g in glob:
        fo.write(pad(g))
    for L in layers:
        fo.write(L)
print(f"{out}: {pos / 1e6:.1f} MB, layer block {len(layers[0]) / 1e6:.2f} MB, vocab {len(vocab)}")

with open(sys.argv[2] if len(sys.argv) > 2 else 'mxtest.bin', 'wb') as fo:
    fo.write(struct.pack('<4sII', b'MXT1', len(tests), D))
    for ids, emu, ref in tests:
        fo.write(struct.pack('<I', len(ids)) + np.asarray(ids, np.uint32).tobytes() + emu.tobytes() + ref.tobytes())
