"""CHATS-8: does choosing the outlier channels on real messages (or more of them) fix the tail?

prep_full.py picks K = 8 outlier input channels per matmul from max |x| over 10
hand-written texts. Here: pick them from real messages instead, for several K,
and measure device-vs-Ollama cosine on held-out messages. Only the outlier set
(and its fp32 weight columns) changes; the int8 weights and scales in mxbai.bin
stay as they are.

    python recal.py [n_calib] [K ...]       # default: 300 calibration texts, K = 8 16 32

Calibration texts come from the first half of corpus.json, evaluation from the
second half, against the Ollama vectors tail.py saved in tail.npz.
"""
import json
import sys
import time

import numpy as np

import emu_bin
import mxbai as M


def calibrate(texts):
    """Per matmul: max |x| per input channel over the texts (fp32 pass)."""
    stats = {}
    orig = M.mm

    def rec(x, name, int8):
        a = np.abs(x).max(0)
        stats[name] = np.maximum(stats[name], a) if name in stats else a
        return orig(x, name, False)

    M.mm = rec
    try:
        for t in texts:
            M.embed(t)
    finally:
        M.mm = orig
    return stats


def with_outliers(stats, k):
    """emu_bin.Q with the top-k channels per matmul as the outliers."""
    q = {}
    for name, (wq, ws, b, _, _) in emu_bin.Q.items():
        o = np.sort(np.argsort(-stats[name])[:k])
        q[name] = (wq, ws, b, o, wq[:, o] * ws[:, None])
    return q


def evaluate(q, texts, ref):
    saved = emu_bin.Q
    emu_bin.Q = q
    try:
        return np.array([M.cos(emu_bin.embed(M.TOK.encode(t)), r) for t, r in zip(texts, ref)])
    finally:
        emu_bin.Q = saved


def report(name, c):
    print(f'{name:28s} mean {c.mean():.6f}  min {c.min():.6f}  0.1% {np.percentile(c, 0.1):.6f}  '
          f'1% {np.percentile(c, 1):.6f}  <0.998: {(c < 0.998).sum()}', flush=True)


def main():
    n_cal = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    ks = [int(a) for a in sys.argv[2:]] or [8, 16, 32]
    texts = [t[:800] for t in json.load(open('corpus.json'))]
    tail = np.load('tail.npz')
    half = len(texts) // 2
    rng = np.random.default_rng(0)
    cal = [texts[i] for i in rng.choice(half, n_cal, replace=False)]
    ev, ev_ref = texts[half:], tail['ref'][half:]

    report('as packed (10 texts, K=8)', tail['cos'][half:])
    t0 = time.time()
    stats = calibrate(cal)
    print(f'calibrated on {n_cal} real texts in {time.time() - t0:.0f} s', flush=True)
    out = {}
    for k in ks:
        c = evaluate(with_outliers(stats, k), ev, ev_ref)
        out[k] = c
        report(f'{n_cal} real texts, K={k}', c)
    np.savez('recal.npz', **{f'k{k}': c for k, c in out.items()})


if __name__ == '__main__':
    main()
