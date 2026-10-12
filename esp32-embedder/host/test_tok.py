"""Check the C tokeniser against llama-server (what Ollama runs), id for id.

    python test_tok.py              # corpus_ids.json (from ollama_ids.py) + stress + long
    python test_tok.py --stress     # stress only (needs llama-server on :18555)

Stress cases go to a live llama-server (see ollama_ids.py for how to start one):
hand-picked edge cases plus random strings drawn from awkward Unicode blocks.
Invalid UTF-8 is not covered: JSON can't carry it, so it can't reach Ollama.

Long inputs (over 512 tokens) go to Ollama itself, because Ollama truncates
by detokenising and re-embedding text, which /tokenize can't show: the fp32
numpy vector from tok.c's ids must match Ollama's to 0.99999, where being one
token off gives ~0.9999.
"""
import json
import random
import sys
import urllib.request

import tok

TOK = tok.from_bin()                                  # the vocab the S3 reads

URL = 'http://127.0.0.1:18555/tokenize'

EDGE = [
    '', ' ', 'Hello world', 'a [SEP] b', '[CLS][SEP][PAD][UNK][MASK]', '[cls] [Sep]', 'x[MASK]y',
    '[SEP', 'SEP]', '[[SEP]]', 'café naïve Ünïcode ÅNGSTRÖM', 'İstanbul ǅ ß ẞ ﬁ ﬀ', 'Ⅻ ① ㎏ ℡',
    '中文字符测试，你好！', '日本語のテキスト', '한국어 텍스트', 'emoji 😀👍🏽🇬🇧 ok', 'zero\u200bwidth\u200djoin',
    'tab\there\nnew\r\nline\xa0nbsp\u3000ideo', 'ctrl\x00\x01\x7f\x85\u2028\u2029', 'x' * 300,
    'supercalifragilisticexpialidocious' * 5, '▁the ▁ ▁▁', '##ing ## #', 'a\u0301e\u0301 \u0300',
    'Ελληνικά Кириллица עברית العربية हिन\u094dदी ไทย', '$100 £5 €3 ¥9 ©®™ ±×÷ <=> ^_^ `~|\\',
    'https://example.com/path?q=1&r=2#frag', 'snake_case camelCase kebab-case', '...!!!???',
    '�\ufeff\ufe0f', 'a' + '\u0300' * 50, '𝔘𝔫𝔦𝔠𝔬𝔡𝔢 𝐁𝐨𝐥𝐝', '\U0002b820\U0002b920\U0002ceaf',
]

BLOCKS = [(0x20, 0x7f), (0xa0, 0x250), (0x300, 0x370), (0x370, 0x530), (0x590, 0x700), (0x900, 0x980),
          (0xe00, 0xe80), (0x1e00, 0x2000), (0x2000, 0x2070), (0x2100, 0x2200), (0x2460, 0x2500),
          (0x3000, 0x3100), (0x4e00, 0x4f00), (0xac00, 0xad00), (0xfb00, 0xfb50), (0xfe00, 0xfe70),
          (0xff00, 0xfff0), (0x1d400, 0x1d500), (0x1f300, 0x1f700), (0x20000, 0x20100), (0x2b800, 0x2b950),
          (0xe0000, 0xe0080), (0xd800, 0xe000)]


def server(b):
    req = urllib.request.Request(URL, json.dumps({'content': b.decode('utf-8', 'surrogatepass') if isinstance(b, bytes) else b,
                                                  'add_special': True}).encode('utf-8', 'surrogatepass'),
                                 {'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))['tokens']


def ollama_truncate(ids, n=512):
    return ids if len(ids) <= n else ids[:n - 1] + [ids[-1]]


def check(name, text, want):
    got = TOK.encode(text)
    want = ollama_truncate(want)
    if got != want:
        i = next((k for k in range(min(len(got), len(want))) if got[k] != want[k]), min(len(got), len(want)))
        print(f'MISMATCH {name}: len {len(got)} vs {len(want)}, first diff at {i}: {got[i:i + 5]} vs {want[i:i + 5]}')
        return False
    return True


def stress(n_random=3000):
    rng = random.Random(1)
    bad = 0
    for k, t in enumerate(EDGE):
        bad += not check(f'edge {k} {t[:20]!r}', t, server(t))
    for k in range(n_random):
        parts = []
        for _ in range(rng.randint(1, 40)):
            a, b = rng.choice(BLOCKS)
            cps = [rng.randrange(a, b) for _ in range(rng.randint(1, 8))]
            if a == 0xd800:
                cps = [0x61]                            # JSON can't carry lone surrogates
            parts.append(''.join(map(chr, cps)) + rng.choice(['', ' ', '  ', '\n', '[SEP]', '.']))
        t = ''.join(parts)
        bad += not check(f'random {k}', t, server(t))
    print(f'stress: {len(EDGE)} edge + {n_random} random, {bad} mismatches')
    return bad


def long():
    import numpy as np
    import mxbai as M
    bad = 0
    for text in (' '.join(f'word{i} alpha beta' for i in range(200)),
                 ' '.join(f'\u00dcn\u00efcode-{i} na\u00efve, caf\u00e9!' for i in range(150))):
        ids, total = TOK.encode(text, total=True)
        req = urllib.request.Request('http://localhost:11434/api/embed',
                                     json.dumps({'model': 'mxbai-embed-large', 'input': text}).encode())
        o = np.array(json.load(urllib.request.urlopen(req))['embeddings'][0])
        n = len(ids)
        x = M.T['token_embd.weight'][ids] + M.T['position_embd.weight'][:n] + M.T['token_types.weight'][0]
        x = M.ln(x, M.T['token_embd_norm.weight'], M.T['token_embd_norm.bias'])
        for i in range(M.LAYERS):
            x = M.layer(x, i)
        c = M.cos(x[0], o)
        print(f'long: {total} tokens -> {n}, cos vs Ollama {c:.6f}')
        bad += c < 0.99999
    return bad


def corpus():
    d = json.load(open('corpus_ids.json'))
    bad = 0
    for k, r in enumerate(d):
        bad += not check(f'corpus {k} full', r['text'], r['ids'])
        bad += not check(f'corpus {k} 800', r['text'][:800], r['ids800'])
    print(f'corpus: {len(d)} texts x 2 lengths, {bad} mismatches')
    return bad


if __name__ == '__main__':
    bad = (0 if '--stress' in sys.argv else corpus() + long()) + stress()
    sys.exit(1 if bad else 0)
