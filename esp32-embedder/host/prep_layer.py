"""Pack mxbai layer 0 for the ESP32-S3 test, plus a test input and expected outputs.

Scheme (must match layer0.c on the S3): weights int8 per output row; the
inputs to each matmul are int8 per token, except K outlier input dims, which
stay fp32 (with their weight columns dequantised to fp32). Everything else fp32.

Blob layout (little-endian, every section 16-byte aligned): header of uint32
[magic 'MXL0', n_tokens, k, n_sections, offsets...] then the sections in the
order listed in SECTIONS.
"""
import struct
import sys

import numpy as np

import mxbai as M

K = 8
MATS = ['attn_q', 'attn_k', 'attn_v', 'attn_output', 'ffn_up', 'ffn_down']
CALIB = ["The cat sat on the radiator all afternoon.", "git push failed: permission denied (publickey)",
         "Traceback (most recent call last): KeyError: 'id'", "Shall I carry on with the kernel?",
         "ESP32-S3 N16R8 with octal PSRAM at 80 MHz", "Plane work item HOME-432 updated with results."]
TEXT = ("Martin asked whether a cheap ESP32-S3 could take over the embedding job that Cal does today. "
        "The plan streams the weights of mxbai-embed-large from an SD card, one matrix at a time, and "
        "keeps a batch of messages in PSRAM so that each weight is used many times. Earlier benchmarks "
        "measured about one billion multiply-accumulates per second across both cores, which suggests "
        "roughly thirty seconds for a typical message. This test checks the arithmetic of one encoder "
        "layer against a reference computed on the Mac.")

# 1. calibrate outlier input dims per matmul (max |x| over calibration texts, all layers' inputs to layer 0 only)
stats = {}
orig = M.mm


def rec(x, name, int8):
    stats.setdefault(name, []).append(np.abs(x).max(0))
    return orig(x, name, False)


M.mm = rec
for t in CALIB:
    M.layer(M.embed_input(t), 0)
M.mm = orig
OUT = {n: np.sort(np.argsort(-np.max(np.stack(v), 0))[:K]) for n, v in stats.items()}

# 2. quantised matrices
Q = {}
for m in MATS:
    name = 'blk.0.' + m
    w = M.T[name + '.weight']
    wq, ws = M.q_rows(w)
    o = OUT[name]
    Q[name] = (wq, ws.astype(np.float32), M.T[name + '.bias'], o, (wq[:, o].astype(np.float32) * ws[:, None]))


def mm_emu(x, name, int8):
    wq, ws, b, o, wo = Q[name]
    xr = x.copy()
    xr[:, o] = 0
    s = np.abs(xr).max(1, keepdims=True) / 127
    s[s == 0] = 1
    xq = np.round(xr / s).astype(np.int32)
    return (xq @ wq.astype(np.int32).T) * s * ws[None, :] + x[:, o] @ wo.T + b


x0 = M.embed_input(TEXT).astype(np.float32)
y_ref = M.layer(x0.copy(), 0, int8=False).astype(np.float32)
M.mm = mm_emu
y_emu = M.layer(x0.copy(), 0, int8=True).astype(np.float32)
M.mm = orig
n = x0.shape[0]
print(f"{n} tokens; layer-0 emulation vs fp32: min token cos "
      f"{min(M.cos(a, b) for a, b in zip(y_emu, y_ref)):.6f}")

# 3. pack
sections = []
for m in MATS:
    wq, ws, b, o, wo = Q['blk.0.' + m]
    sections += [wq.tobytes(), ws.tobytes(), b.astype(np.float32).tobytes(),
                 o.astype(np.uint16).tobytes(), np.ascontiguousarray(wo, np.float32).tobytes()]
for nm in ('attn_output_norm', 'layer_output_norm'):
    sections += [M.T[f'blk.0.{nm}.weight'].astype(np.float32).tobytes(),
                 M.T[f'blk.0.{nm}.bias'].astype(np.float32).tobytes()]
sections += [x0.tobytes(), y_emu.tobytes(), y_ref.tobytes()]

hdr_len = 16 + 4 * len(sections)
hdr_len = (hdr_len + 15) & ~15
offs, pos = [], hdr_len
for s in sections:
    offs.append(pos)
    pos = (pos + len(s) + 15) & ~15
blob = bytearray(pos)
struct.pack_into('<4sIII', blob, 0, b'MXL0', n, K, len(sections))
struct.pack_into(f'<{len(offs)}I', blob, 16, *offs)
for o, s in zip(offs, sections):
    blob[o:o + len(s)] = s
open(sys.argv[1] if len(sys.argv) > 1 else 'layer0.bin', 'wb').write(blob)
print(f"blob {len(blob) / 1e6:.2f} MB, {len(sections)} sections")
