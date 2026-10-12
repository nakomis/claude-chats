"""mxbai-embed-large (BERT-large) forward pass in numpy, from the Ollama GGUF.

fp32 reference, plus an emulation of the int8 scheme the ESP32-S3 would use:
weights symmetric int8 per output row, activations symmetric int8 per token
before every matmul, int32 accumulate. Everything else (softmax, layernorm,
GELU, residuals) in fp32.
"""
import json
import os
from pathlib import Path

import numpy as np
from gguf import GGUFReader
from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors



def _ollama_gguf(model='mxbai-embed-large', tag='latest'):
    """Path of the model blob Ollama pulled (`ollama pull mxbai-embed-large`)."""
    models = Path(os.environ.get('OLLAMA_MODELS', Path.home() / '.ollama' / 'models'))
    manifest = json.loads((models / 'manifests' / 'registry.ollama.ai' / 'library' / model / tag).read_text())
    digest = next(l['digest'] for l in manifest['layers'] if l['mediaType'] == 'application/vnd.ollama.image.model')
    return str(models / 'blobs' / digest.replace(':', '-'))


GGUF = os.environ.get('MXBAI_GGUF') or _ollama_gguf()
EPS = 1e-12
LAYERS, HEADS, D = 24, 16, 1024

_r = GGUFReader(GGUF)
T = {t.name: np.asarray(t.data, dtype=np.float32) for t in _r.tensors}


def _tokenizer():
    f = _r.fields['tokenizer.ggml.tokens']
    # llama.cpp stores BERT WordPiece as '\u2581word' (word start) and bare
    # 'piece' (continuation); turn it back into 'word' / '##piece'
    vocab = {}
    for n, i in enumerate(f.data):
        t = bytes(f.parts[i]).decode()
        if t.startswith('\u2581'):
            t = t[1:]
        elif not (t.startswith('[') and t.endswith(']')):
            t = '##' + t
        vocab.setdefault(t, n)
    tok = Tokenizer(models.WordPiece(vocab, unk_token='[UNK]', max_input_chars_per_word=100))
    tok.normalizer = normalizers.BertNormalizer(lowercase=True)
    tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
    tok.post_processor = processors.BertProcessing(('[SEP]', 102), ('[CLS]', 101))
    tok.enable_truncation(512)
    return tok


TOK = _tokenizer()


def ln(x, w, b):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + EPS) * w + b


def gelu(x):
    return 0.5 * x * (1 + np.tanh(0.7978845608 * (x + 0.044715 * x ** 3)))


# ---- int8 emulation -------------------------------------------------------
def q_rows(w):
    """Symmetric int8 per output row; returns (int8 weights, per-row scale)."""
    s = np.abs(w).max(1, keepdims=True) / 127.0
    s[s == 0] = 1
    return np.clip(np.round(w / s), -127, 127).astype(np.int8), s[:, 0]


_QCACHE = {}


def mm(x, name, int8):
    w, b = T[name + '.weight'], T[name + '.bias']
    if not int8:
        return x @ w.T + b
    if name not in _QCACHE:
        _QCACHE[name] = q_rows(w)
    wq, ws = _QCACHE[name]
    xs = np.abs(x).max(1, keepdims=True) / 127.0          # per token
    xs[xs == 0] = 1
    xq = np.clip(np.round(x / xs), -127, 127).astype(np.int32)
    acc = xq @ wq.astype(np.int32).T                      # int32 accumulate
    return acc * xs * ws[None, :] + b


def embed_input(text):
    ids = TOK.encode(text).ids
    n = len(ids)
    x = T['token_embd.weight'][ids] + T['position_embd.weight'][:n] + T['token_types.weight'][0]
    return ln(x, T['token_embd_norm.weight'], T['token_embd_norm.bias'])


def layer(x, i, int8=False):
    p = f'blk.{i}.'
    n = x.shape[0]
    q = mm(x, p + 'attn_q', int8).reshape(n, HEADS, -1).transpose(1, 0, 2)
    k = mm(x, p + 'attn_k', int8).reshape(n, HEADS, -1).transpose(1, 0, 2)
    v = mm(x, p + 'attn_v', int8).reshape(n, HEADS, -1).transpose(1, 0, 2)
    a = q @ k.transpose(0, 2, 1) / np.sqrt(q.shape[-1])
    a = np.exp(a - a.max(-1, keepdims=True))
    a /= a.sum(-1, keepdims=True)
    o = (a @ v).transpose(1, 0, 2).reshape(n, D)
    x = ln(x + mm(o, p + 'attn_output', int8), T[p + 'attn_output_norm.weight'], T[p + 'attn_output_norm.bias'])
    h = gelu(mm(x, p + 'ffn_up', int8))
    return ln(x + mm(h, p + 'ffn_down', int8), T[p + 'layer_output_norm.weight'], T[p + 'layer_output_norm.bias'])


def embed(text, int8=False):
    x = embed_input(text)
    for i in range(LAYERS):
        x = layer(x, i, int8)
    return x[0]                                           # CLS pooling


def cos(a, b):
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    return float(a @ b / np.linalg.norm(a) / np.linalg.norm(b))
