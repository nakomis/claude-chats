"""CHATS-8 tail quality: the device's vectors against Ollama's over real messages.

For each corpus text, cut to 800 characters as conversation-memory does:
tokenise (tok.c), embed with the device's arithmetic (emu_bin, read from
mxbai.bin), fetch Ollama's vector, and record the cosine. Reports the minimum
and low percentiles, not just the mean, and the worst texts' shapes (lengths,
not content: the corpus is private).

    python tail.py [corpus.json] [out.npz]
"""
import json
import sys
import time
import urllib.request

import numpy as np

import emu_bin
import mxbai as M

OLLAMA = 'http://localhost:11434/api/embed'


def ollama(texts):
    req = urllib.request.Request(OLLAMA, json.dumps({'model': 'mxbai-embed-large', 'input': texts}).encode())
    return np.array(json.load(urllib.request.urlopen(req))['embeddings'], np.float32)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else 'corpus.json'
    dst = sys.argv[2] if len(sys.argv) > 2 else 'tail.npz'
    texts = [t[:800] for t in json.load(open(src))]
    n = len(texts)
    cos, ntok, dev, ref = np.zeros(n), np.zeros(n, int), np.zeros((n, 1024), np.float32), np.zeros((n, 1024), np.float32)
    t0 = time.time()
    for i in range(0, n, 32):
        ref[i:i + 32] = ollama(texts[i:i + 32])
    print(f'Ollama: {n} vectors in {time.time() - t0:.0f} s', flush=True)
    t0 = time.time()
    for i, t in enumerate(texts):
        ids = M.TOK.encode(t)
        ntok[i] = len(ids)
        dev[i] = emu_bin.embed(ids)
        cos[i] = M.cos(dev[i], ref[i])
        if i % 100 == 99:
            print(f'{i + 1}/{n}  {time.time() - t0:.0f} s  min so far {cos[:i + 1].min():.6f}', flush=True)
    np.savez(dst, cos=cos, ntok=ntok, dev=dev, ref=ref)

    print(f'\n{n} texts, {ntok.min()}-{ntok.max()} tokens (median {int(np.median(ntok))})')
    print(f'cos device vs Ollama: mean {cos.mean():.6f}  min {cos.min():.6f}')
    for p in (0.1, 1, 5, 50):
        print(f'  {p:>4}th percentile {np.percentile(cos, p):.6f}')
    print('worst 10 (index, tokens, non-ASCII chars, cos):')
    for i in np.argsort(cos)[:10]:
        print(f'  {i:5d} {ntok[i]:4d} {sum(ord(c) > 127 for c in texts[i]):4d}  {cos[i]:.6f}')


if __name__ == '__main__':
    main()
