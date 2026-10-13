"""CHATS-4: does a leaner activation layout keep accuracy? (emulation, before touching the device)

To fit a whole message (up to 512 tokens) in PSRAM, the firmware would keep:
  - x (the residual stream) in fp32, with matmul outputs added straight into it
  - q, k, v (and o, written over q) in fp16
  - the FFN hidden as int8 per (token, block of 16 rows) with its own scale, the
    ffn_down outlier channels kept in fp32, then requantised per token for ffn_down

That is ~11 KB per token instead of ~16. This runs the device arithmetic (emu_bin)
with those roundings added and compares device-vs-Ollama cosine with the
current layout on the same messages.

    python lean.py [n_texts]
"""
import json
import sys

import numpy as np

import emu_bin
import mxbai as M

BLOCK = 16                                   # rows per scale, as the firmware


def f16(x):
    return x.astype(np.float16).astype(np.float32)


def layer_lean(x, i):
    p = f'blk.{i}.'
    n = x.shape[0]
    mm = emu_bin._mm
    q, k, v = (f16(mm(x, p + m, True)).reshape(n, M.HEADS, -1).transpose(1, 0, 2) for m in ('attn_q', 'attn_k', 'attn_v'))
    a = q @ k.transpose(0, 2, 1) / np.sqrt(q.shape[-1])
    a = np.exp(a - a.max(-1, keepdims=True))
    a /= a.sum(-1, keepdims=True)
    o = f16((a @ v).transpose(1, 0, 2).reshape(n, M.D))
    x = M.ln(x + mm(o, p + 'attn_output', True), M.T[p + 'attn_output_norm.weight'], M.T[p + 'attn_output_norm.bias'])
    h = M.gelu(mm(x, p + 'ffn_up', True))
    # block int8 with its own scale, outlier channels of ffn_down left exact
    oi = emu_bin.Q[p + 'ffn_down'][3]
    keep = h[:, oi].copy()
    hb = h.copy()
    hb[:, oi] = 0
    for b0 in range(0, hb.shape[1], BLOCK):
        blk = hb[:, b0:b0 + BLOCK]
        s = np.abs(blk).max(1, keepdims=True) / 127
        s[s == 0] = 1
        hb[:, b0:b0 + BLOCK] = np.round(blk / s) * s
    hb[:, oi] = keep
    return M.ln(x + mm(hb, p + 'ffn_down', True), M.T[p + 'layer_output_norm.weight'], M.T[p + 'layer_output_norm.bias'])


def embed_lean(ids):
    ids = np.asarray(ids)
    G = emu_bin.G
    x = G['tok_q'][ids].astype(np.float32) * G['tok_s'][ids, None] + G['pos'][:len(ids)] + G['type0']
    x = M.ln(x, G['en_w'], G['en_b'])
    for i in range(M.LAYERS):
        x = layer_lean(x, i)
    return x[0]


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    texts = [t[:800] for t in json.load(open('corpus.json'))]
    tail = np.load('tail.npz')
    idx = np.random.default_rng(1).choice(len(texts), n, replace=False)
    idx = np.union1d(idx, np.argsort(tail['cos'])[:20])          # include the known worst
    base = tail['cos'][idx]
    lean = np.array([M.cos(embed_lean(M.TOK.encode(texts[i])), tail['ref'][i]) for i in idx])
    for name, c in (('current layout', base), ('lean layout', lean)):
        print(f'{name:15s} mean {c.mean():.6f}  min {c.min():.6f}  1% {np.percentile(c, 1):.6f}  median {np.median(c):.6f}')
    d = lean - base
    print(f'lean - current: mean {d.mean():+.6f}, worst {d.min():+.6f}, best {d.max():+.6f} over {len(idx)} texts')


if __name__ == '__main__':
    main()
