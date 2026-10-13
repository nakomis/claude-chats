"""The device's arithmetic on the Mac, read from mxbai.bin itself.

prep_full.py's emulation uses the quantised weights it has just made; this one
reads them back from the packed file the S3 streams, so it covers exactly what
is on the card (outlier choice, scales, packing). Matmuls run in float32 on the
int8 values, which is within ~1e-7 of the device's int32 accumulate.

    import emu_bin; v = emu_bin.embed(ids)
"""
import struct

import numpy as np

import mxbai as M
import tok

MATS = ['attn_q', 'attn_k', 'attn_v', 'attn_output', 'ffn_up', 'ffn_down']


def _load(path=tok.BIN):
    raw = np.memmap(path, np.uint8, 'r')
    magic, _, L, K, V, D, FF, P = struct.unpack_from('<4sIIIIIII', raw, 0)
    assert magic == b'MXB1'
    goff = struct.unpack_from('<7I', raw, 32)
    loff = struct.unpack_from(f'<{L}I', raw, 60)

    def arr(off, dtype, n):
        return np.frombuffer(raw, dtype, n, off)

    g = {'tok_q': arr(goff[0], np.int8, V * D).reshape(V, D), 'tok_s': arr(goff[1], np.float32, V),
         'pos': arr(goff[2], np.float32, P * D).reshape(P, D), 'type0': arr(goff[3], np.float32, D),
         'en_w': arr(goff[4], np.float32, D), 'en_b': arr(goff[5], np.float32, D)}
    q = {}
    for li in range(L):
        tab = struct.unpack_from('<34I', raw, loff[li])
        for mi, m in enumerate(MATS):
            i, o = (FF, D) if m == 'ffn_down' else (D, FF) if m == 'ffn_up' else (D, D)
            s = [loff[li] + t for t in tab[mi * 5:mi * 5 + 5]]
            q[f'blk.{li}.{m}'] = (arr(s[0], np.int8, o * i).reshape(o, i).astype(np.float32),
                                  arr(s[1], np.float32, o), arr(s[2], np.float32, o),
                                  arr(s[3], np.uint16, K).astype(np.int64),
                                  arr(s[4], np.float32, o * K).reshape(o, K))
        # the layer norms: the file's copies, so the whole layer comes off the card
        for k, nm in enumerate(('attn_output_norm.weight', 'attn_output_norm.bias',
                                'layer_output_norm.weight', 'layer_output_norm.bias')):
            M.T[f'blk.{li}.{nm}'] = arr(loff[li] + tab[30 + k], np.float32, D)
    return g, q


G, Q = _load()


def _mm(x, name, int8):
    wq, ws, b, o, wo = Q[name]
    xr = x.copy()
    xr[:, o] = 0
    s = np.abs(xr).max(1, keepdims=True) / 127
    s[s == 0] = 1
    xq = np.round(xr / s).astype(np.float32)
    return (xq @ wq.T) * s * ws[None, :] + x[:, o] @ wo.T + b


def embed(ids):
    """CLS vector for token ids, as the S3 computes it."""
    ids = np.asarray(ids)
    n = len(ids)
    x = G['tok_q'][ids].astype(np.float32) * G['tok_s'][ids, None] + G['pos'][:n] + G['type0']
    x = M.ln(x, G['en_w'], G['en_b'])
    orig, M.mm = M.mm, _mm
    try:
        for i in range(M.LAYERS):
            x = M.layer(x, i, int8=True)
    finally:
        M.mm = orig
    return x[0].astype(np.float32)
