"""CHATS-5: several messages in one pass give each the vector it gets on its own, exactly.

Packs batches of messages as the firmware does (lean.embed_lean_seg: positions
restart at each message, attention stays within it) and checks every message's
vector against embedding it alone (lean.embed_lean). The arithmetic is the same,
so the bits must be. As a check that the test can fail, the batch run as one
sequence (the pass before CHATS-5) must not match.

    python test_seg.py           # small random weights: no model files needed
    python test_seg.py --real    # mxbai-embed-large itself (mxbai.bin, and Ollama's GGUF)
"""
import sys

import numpy as np

import lean

CLS, SEP = 101, 102

# the texts firmware/full tests with (full.c), and a few more
TEXTS = [
    'The cat sat on the radiator all afternoon.',
    'Shall I carry on with the kernel?',
    'git push failed: permission denied (publickey)',
    'login',
    'Martin asked whether a cheap ESP32-S3 could take over the embedding job that Cal does today. '
    'The plan streams the weights of mxbai-embed-large from an SD card, one matrix at a time.',
    '^^',
]


def check(W, batches, label):
    bad = 0
    for batch in batches:
        got = lean.embed_lean_seg(batch, W)
        alone = np.array([lean.embed_lean(ids, W) for ids in batch])
        same = [np.array_equal(g, a) for g, a in zip(got, alone)]
        # control: the whole batch as one sequence, as the firmware would have run it before CHATS-5
        joined = lean.embed_lean(np.concatenate(batch), W)
        control = not np.array_equal(joined, alone[0]) or len(batch) == 1
        bad += not all(same) or not control
        lens = '+'.join(str(len(b)) for b in batch)
        print(f'{label}: {len(batch):2d} messages, {lens} = {sum(map(len, batch))} tokens: '
              f'{sum(same)}/{len(batch)} the same bits as alone; as one sequence, message 0 '
              f'{"differs" if control else "DOES NOT DIFFER"} (max |diff| {np.abs(joined - alone[0]).max():.2e})')
    return bad


def synthetic():
    bad = 0
    for seed in range(3):
        W = lean.synthetic(seed)
        r = np.random.default_rng(100 + seed)
        vocab = W.G['tok_q'].shape[0]

        def msg(n):
            return [CLS] + list(r.integers(103, vocab, n - 2)) + [SEP]
        batches = [[msg(n) for n in (13, 10, 13, 115, 140)], [msg(2), msg(2), msg(3)], [msg(300), msg(212)]]
        for _ in range(3):
            lens, total = [], 0
            while len(lens) < 12:
                n = int(r.integers(2, 80))
                if total + n > 512:
                    break
                lens.append(n)
                total += n
            batches.append([msg(n) for n in lens])
        bad += check(W, batches, f'synthetic {seed}')
    return bad


def real():
    import mxbai as M
    W = lean.real()
    ids = [M.TOK.encode(t) for t in TEXTS]
    return check(W, [ids[:3], ids, ids[3:] + ids[:1]], 'mxbai')


if __name__ == '__main__':
    bad = real() if '--real' in sys.argv else synthetic()
    print('all passed' if not bad else f'{bad} batches FAILED')
    sys.exit(bad != 0)
