"""The C tokeniser (../tok) from Python, via ctypes.

    import tok; tok.from_bin().encode('Hello world')   # -> [101, 7592, 2088, 102]

mxbai.py builds one from the GGUF (tok.from_gguf_tokens); the tests use the
vocab section of mxbai.bin, which is what the S3 reads.

Builds ../tok/libtok.so with the system cc on first use (and whenever the C is
newer than the library).
"""
import ctypes
import os
import struct
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / 'tok'
LIB = SRC / 'libtok.so'
BIN = os.environ.get('MXBAI_BIN') or str(HERE / 'mxbai.bin')
MAX_IDS = 512


def _build():
    srcs = [SRC / 'tok.c', SRC / 'tok.h', SRC / 'tok_tables.h']
    if not LIB.exists() or LIB.stat().st_mtime < max(s.stat().st_mtime for s in srcs):
        subprocess.run(['cc', '-O2', '-std=c99', '-Wall', '-Wextra', '-shared', '-fPIC',
                        '-o', str(LIB), str(SRC / 'tok.c')], check=True)
    return ctypes.CDLL(str(LIB))


class _Vocab(ctypes.Structure):
    _fields_ = [('buf', ctypes.c_void_p), ('off', ctypes.c_void_p), ('hash', ctypes.c_void_p),
                ('hmask', ctypes.c_uint32), ('n', ctypes.c_int), ('max_len', ctypes.c_int),
                ('alloc', ctypes.c_void_p), ('free', ctypes.c_void_p)]


_lib = _build()
_libc = ctypes.CDLL(None)
_lib.tok_init.argtypes = [ctypes.POINTER(_Vocab), ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int,
                          ctypes.c_void_p, ctypes.c_void_p]
_lib.tok_encode.argtypes = [ctypes.POINTER(_Vocab), ctypes.c_char_p, ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_int32), ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_lib.tok_lookup.argtypes = [ctypes.POINTER(_Vocab), ctypes.c_char_p, ctypes.c_size_t]
_lib.tok_free.argtypes = [ctypes.POINTER(_Vocab)]


class Tokenizer:
    """tok.c over a vocab section: NUL-separated HF-form strings, as in mxbai.bin."""

    def __init__(self, vocab, n):
        self._v = _Vocab()
        if _lib.tok_init(ctypes.byref(self._v), vocab, len(vocab), n,
                         ctypes.cast(_libc.malloc, ctypes.c_void_p), ctypes.cast(_libc.free, ctypes.c_void_p)):
            raise RuntimeError('tok_init failed')

    def __del__(self):
        _lib.tok_free(ctypes.byref(self._v))

    def lookup(self, s):
        """Id of a GGUF-form string, or -1."""
        b = s.encode()
        return _lib.tok_lookup(ctypes.byref(self._v), b, len(b))

    def encode(self, text, max_ids=MAX_IDS, total=False):
        """Token ids for text (str or UTF-8 bytes), [CLS] ... [SEP], truncated as Ollama does."""
        b = text.encode('utf-8', 'surrogatepass') if isinstance(text, str) else bytes(text)
        ids = (ctypes.c_int32 * max_ids)()
        t = ctypes.c_int()
        n = _lib.tok_encode(ctypes.byref(self._v), b, len(b), ids, max_ids, ctypes.byref(t))
        if n < 0:
            raise ValueError(f'tok_encode failed ({n})')
        return (list(ids[:n]), t.value) if total else list(ids[:n])


def hf_form(gguf_tokens):
    """GGUF strings ('\u2581word', bare 'piece') to the HF form mxbai.bin stores ('word', '##piece')."""
    out = []
    for t in gguf_tokens:
        if t.startswith('\u2581'):
            t = t[1:]
        elif not (t.startswith('[') and t.endswith(']')):
            t = '##' + t
        out.append(t)
    return out


def from_gguf_tokens(gguf_tokens):
    gguf_tokens = list(gguf_tokens)
    vocab = hf_form(gguf_tokens)
    t = Tokenizer(b'\0'.join(v.encode() for v in vocab) + b'\0', len(vocab))
    # the HF form only round-trips by convention ("[..]" bare, "##" continues): check it
    bad = [i for i, g in enumerate(gguf_tokens) if t.lookup(g) != i]
    assert not bad, f'vocab does not round-trip, e.g. id {bad[0]} {gguf_tokens[bad[0]]!r}'
    return t


def from_bin(path=BIN):
    """The vocab section of mxbai.bin (global section 6), as the S3 reads it off the card."""
    with open(path, 'rb') as f:
        hdr = f.read(512)
        n_vocab = struct.unpack_from('<4sIIIIIII', hdr)[4]
        goff = struct.unpack_from('<7I', hdr, 32)
        f.seek(goff[6])
        return Tokenizer(f.read(n_vocab * 32), n_vocab)
