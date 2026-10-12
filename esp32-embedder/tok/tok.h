// WordPiece tokeniser for mxbai-embed-large that gives the same ids as Ollama.
//
// Ollama embeds through llama.cpp's llama-server, so this is a port of
// llama.cpp's WPM tokeniser (src/llama-vocab.cpp, llm_tokenizer_wpm_session,
// tag b11351), not HuggingFace's. The differences that matter:
//   - literal "[PAD]" "[UNK]" "[CLS]" "[SEP]" "[MASK]" in the text become those
//     special tokens (llama-server tokenises with parse_special)
//   - llama.cpp's own Unicode tables: single-codepoint NFD, combining marks and
//     controls dropped, its lowercase map; split on punctuation, ASCII symbols
//     and CJK
//   - greedy longest match over UTF-8 bytes against the GGUF strings ("▁word"
//     starts a word, bare "piece" continues one), with no 100-char [UNK] cap
//   - over 512 tokens Ollama keeps [CLS] + the first 510 + [SEP]
//
// Portable C99; the caller supplies the allocator (PSRAM on the S3).
#pragma once
#include <stddef.h>
#include <stdint.h>

#define TOK_CLS 101
#define TOK_SEP 102
#define TOK_UNK 100

typedef struct {
    char *buf;          // GGUF-form strings, NUL-separated, id order
    uint32_t *off;      // offset of each id's string in buf
    uint16_t *hash;     // open addressing, id + 1 (0 = empty)
    uint32_t hmask;
    int n, max_len;     // vocab size, longest string in bytes
    void *(*alloc)(size_t);
    void (*free)(void *);
} tok_vocab;

// Build from the vocab section of mxbai.bin: n NUL-separated strings in HF form
// ("word", "##piece", "[CLS]"), which map back to the GGUF strings losslessly.
// Returns 0, or -1 if out of memory or the section is short.
int tok_init(tok_vocab *v, const char *hf, size_t len, int n,
             void *(*alloc)(size_t), void (*free)(void *));
void tok_free(tok_vocab *v);

// Id of a GGUF-form string, or -1.
int tok_lookup(const tok_vocab *v, const char *s, size_t len);

// Tokenise len bytes of UTF-8 (need not be valid) into [CLS] ... [SEP], at most
// max_ids (512 for mxbai, which must be >= 2). Over the limit it keeps [CLS] +
// the first max_ids - 2 + [SEP], as Ollama does. Returns the number of ids
// written; *total (if not NULL) gets the untruncated count. Returns -1 if out of
// memory, or -2 for a 4-byte sequence above U+10FFFF, where llama.cpp throws
// (unreachable through Ollama, whose JSON input is always valid UTF-8).
int tok_encode(const tok_vocab *v, const char *text, size_t len,
               int32_t *ids, int max_ids, int *total);
