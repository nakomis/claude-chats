"""CHATS-4: does a leaner activation layout keep accuracy? (emulation, before touching the device)

To fit a whole message (up to 512 tokens) in PSRAM, the firmware would keep:
  - x (the residual stream) in fp32, with matmul outputs added straight into it
  - q, k, v (and o, written over q) in fp16
  - the FFN hidden as int8 per (token, block of 16 rows) with its own scale, the
    ffn_down outlier channels kept in fp32, then requantised per token for ffn_down

That is ~11 KB per token instead of ~16. This runs the device arithmetic (emu_bin)
with those roundings added and compares device-vs-Ollama cosine with the
current layout on the same messages.

CHATS-5: a pass carries several messages (embed_lean_seg), as the firmware packs
them: positions restart at each message and attention stays within it; everything
else works per token. The weights are a Model: the real one (read from mxbai.bin
through emu_bin, on first use) or synthetic(), small random weights of the same
form, for tests that need no model files (test_seg.py).

    python lean.py [n_texts]
"""
import json
import sys

import numpy as np

BLOCK = 16                                   # rows per scale, as the firmware
EPS = 1e-12                                  # as mxbai.py
MATS = ['attn_q', 'attn_k', 'attn_v', 'attn_output', 'ffn_up', 'ffn_down']
NORMS = ['attn_output_norm.weight', 'attn_output_norm.bias', 'layer_output_norm.weight', 'layer_output_norm.bias']


class Model:
    """The weights the emulation reads. G: the embeddings (as emu_bin.G); Q: each matrix as emu_bin.Q
    holds it (int8 values as float32, row scales, bias, outlier channels, outlier columns); N: the
    layer norms, by GGUF name."""

    def __init__(self, G, Q, N, layers, heads, d):
        self.G, self.Q, self.N, self.layers, self.heads, self.d = G, Q, N, layers, heads, d


_real = None


def real():
    """mxbai-embed-large as the S3 runs it (mxbai.bin, and Ollama's GGUF for mxbai.py)."""
    global _real
    if _real is None:
        import emu_bin
        import mxbai as M
        _real = Model(emu_bin.G, emu_bin.Q, M.T, M.LAYERS, M.HEADS, M.D)
    return _real


def synthetic(seed=0, layers=2, heads=4, d=64, ff=256, vocab=300, n_pos=512, k=8):
    """Small random weights in mxbai.bin's form: int8 rows with a scale each, K outlier channels per
    matrix. Scaled so activations stay O(1); the vectors mean nothing, the arithmetic is the same."""
    r = np.random.default_rng(seed)
    f = np.float32
    G = {'tok_q': r.integers(-127, 128, (vocab, d)).astype(np.int8), 'tok_s': r.uniform(0.005, 0.02, vocab).astype(f),
         'pos': (0.1 * r.standard_normal((n_pos, d))).astype(f), 'type0': (0.1 * r.standard_normal(d)).astype(f),
         'en_w': (1 + 0.1 * r.standard_normal(d)).astype(f), 'en_b': (0.1 * r.standard_normal(d)).astype(f)}
    Q, N = {}, {}
    for li in range(layers):
        for m in MATS:
            i, o = (ff, d) if m == 'ffn_down' else (d, ff) if m == 'ffn_up' else (d, d)
            Q[f'blk.{li}.{m}'] = (r.integers(-127, 128, (o, i)).astype(f),
                                  (r.uniform(0.5, 1.5, o) / (127 * np.sqrt(i))).astype(f),
                                  (0.02 * r.standard_normal(o)).astype(f),
                                  np.sort(r.choice(i, k, replace=False)).astype(np.int64),
                                  (r.standard_normal((o, k)) / np.sqrt(i)).astype(f))
        for nm in NORMS:
            w = 0.1 * r.standard_normal(d) + (nm.endswith('weight'))
            N[f'blk.{li}.{nm}'] = w.astype(f)
    return Model(G, Q, N, layers, heads, d)


def ln(x, w, b):                             # mxbai.ln
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + EPS) * w + b


def gelu(x):                                 # mxbai.gelu
    return 0.5 * x * (1 + np.tanh(0.7978845608 * (x + 0.044715 * x ** 3)))


def mm(W, x, name):
    """emu_bin._mm, with the weights passed in."""
    wq, ws, b, o, wo = W.Q[name]
    xr = x.copy()
    xr[:, o] = 0
    s = np.abs(xr).max(1, keepdims=True) / 127
    s[s == 0] = 1
    xq = np.round(xr / s).astype(np.float32)
    return (xq @ wq.T) * s * ws[None, :] + x[:, o] @ wo.T + b


def f16(x):
    return x.astype(np.float16).astype(np.float32)


def layer_lean(x, i, W=None, segs=None):
    """One layer; segs: the (first, end) token ranges of the messages in the pass (default: one)."""
    W = W or real()
    p = f'blk.{i}.'
    n = x.shape[0]
    q, k, v = (f16(mm(W, x, p + m)).reshape(n, W.heads, -1).transpose(1, 0, 2) for m in ('attn_q', 'attn_k', 'attn_v'))
    o = []
    for s0, s1 in segs or [(0, n)]:          # each message's queries see only its own keys
        a = q[:, s0:s1] @ k[:, s0:s1].transpose(0, 2, 1) / np.sqrt(q.shape[-1])
        a = np.exp(a - a.max(-1, keepdims=True))
        a /= a.sum(-1, keepdims=True)
        o.append((a @ v[:, s0:s1]).transpose(1, 0, 2).reshape(s1 - s0, W.d))
    o = f16(np.concatenate(o))
    x = ln(x + mm(W, o, p + 'attn_output'), W.N[p + 'attn_output_norm.weight'], W.N[p + 'attn_output_norm.bias'])
    h = gelu(mm(W, x, p + 'ffn_up'))
    # block int8 with its own scale, outlier channels of ffn_down left exact
    oi = W.Q[p + 'ffn_down'][3]
    keep = h[:, oi].copy()
    hb = h.copy()
    hb[:, oi] = 0
    for b0 in range(0, hb.shape[1], BLOCK):
        blk = hb[:, b0:b0 + BLOCK]
        s = np.abs(blk).max(1, keepdims=True) / 127
        s[s == 0] = 1
        hb[:, b0:b0 + BLOCK] = np.round(blk / s) * s
    hb[:, oi] = keep
    return ln(x + mm(W, hb, p + 'ffn_down'), W.N[p + 'layer_output_norm.weight'], W.N[p + 'layer_output_norm.bias'])


def embed_lean_seg(batch, W=None):
    """CLS vectors of several messages (lists of ids, each [CLS] ... [SEP]) from one pass."""
    W = W or real()
    G = W.G
    ids = np.concatenate([np.asarray(b) for b in batch])
    pos = np.concatenate([np.arange(len(b)) for b in batch])          # restart at each message
    first = np.cumsum([0] + [len(b) for b in batch])
    segs = list(zip(first[:-1], first[1:]))
    x = G['tok_q'][ids].astype(np.float32) * G['tok_s'][ids, None] + G['pos'][pos] + G['type0']
    x = ln(x, G['en_w'], G['en_b'])
    for i in range(W.layers):
        x = layer_lean(x, i, W, segs)
    return x[first[:-1]]


def embed_lean(ids, W=None):
    return embed_lean_seg([ids], W)[0]


def main():
    import mxbai as M
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
