// WordPiece tokeniser matching Ollama (llama.cpp WPM); see tok.h.
#include "tok.h"

#include <string.h>

#include "tok_tables.h"

static const char *const SPECIAL[] = {"[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"};
#define N_SPECIAL (sizeof SPECIAL / sizeof *SPECIAL)

// ---- vocab ----------------------------------------------------------------
static uint32_t fnv1a(const char *s, size_t len)
{
    uint32_t h = 2166136261u;
    while (len--)
        h = (h ^ (uint8_t)*s++) * 16777619u;
    return h;
}

int tok_lookup(const tok_vocab *v, const char *s, size_t len)
{
    for (uint32_t h = fnv1a(s, len) & v->hmask;; h = (h + 1) & v->hmask) {
        int e = v->hash[h];
        if (!e)
            return -1;
        const char *t = v->buf + v->off[e - 1];
        if (!strncmp(t, s, len) && !t[len])
            return e - 1;
    }
}

int tok_init(tok_vocab *v, const char *hf, size_t len, int n,
             void *(*alloc)(size_t), void (*free)(void *))
{
    memset(v, 0, sizeof *v);
    v->alloc = alloc;
    v->free = free;
    if (n <= 0 || n >= 0xFFFF)
        return -1;
    uint32_t hsize = 1;
    while (hsize < 2u * (uint32_t)n)
        hsize <<= 1;
    v->buf = alloc(len + 3 * (size_t)n);
    v->off = alloc(n * sizeof *v->off);
    v->hash = alloc(hsize * sizeof *v->hash);
    if (!v->buf || !v->off || !v->hash) {
        tok_free(v);
        return -1;
    }
    memset(v->hash, 0, hsize * sizeof *v->hash);
    v->hmask = hsize - 1;

    // HF form back to GGUF: "##piece" -> "piece", "[X]" stays, "word" -> "▁word"
    const char *p = hf, *end = hf + len;
    size_t o = 0;
    for (int i = 0; i < n; i++) {
        const char *z = memchr(p, 0, end - p);
        if (!z) {
            tok_free(v);
            return -1;
        }
        size_t l = z - p;
        v->off[i] = o;
        if (l >= 2 && p[0] == '#' && p[1] == '#') {
            p += 2;
            l -= 2;
        } else if (!(l >= 2 && p[0] == '[' && p[l - 1] == ']')) {
            memcpy(v->buf + o, "\xe2\x96\x81", 3);
            o += 3;
        }
        memcpy(v->buf + o, p, l);
        o += l;
        v->buf[o++] = 0;
        int sl = (int)(o - 1 - v->off[i]);
        if (sl > v->max_len)
            v->max_len = sl;
        // llama.cpp's token_to_id: a later duplicate wins
        uint32_t h = fnv1a(v->buf + v->off[i], sl) & v->hmask;
        while (v->hash[h] && strcmp(v->buf + v->off[v->hash[h] - 1], v->buf + v->off[i]))
            h = (h + 1) & v->hmask;
        v->hash[h] = (uint16_t)(i + 1);
        p = z + 1;
    }
    v->n = n;
    return 0;
}

void tok_free(tok_vocab *v)
{
    if (v->free) {
        v->free(v->buf);
        v->free(v->off);
        v->free(v->hash);
    }
    v->buf = NULL;
    v->off = NULL;
    v->hash = NULL;
}

// ---- Unicode (llama.cpp src/unicode.cpp semantics) -------------------------
static int cpt_class(uint32_t cp)
{
    if (cp >= 0x110000)
        return TOK_KEEP;                                // llama.cpp: undefined flags
    int lo = 0, hi = TOK_N_CLASS - 1;                   // last run starting <= cp
    while (lo < hi) {
        int mid = (lo + hi + 1) / 2;
        if ((tok_class[mid] >> 2) <= cp)
            lo = mid;
        else
            hi = mid - 1;
    }
    return tok_class[lo] & 3;
}

static uint32_t cpt_nfd(uint32_t cp)
{
    int lo = 0, hi = TOK_N_NFD - 1;                     // last range with first <= cp
    while (lo < hi) {
        int mid = (lo + hi + 1) / 2;
        if (tok_nfd[mid][0] <= cp)
            lo = mid;
        else
            hi = mid - 1;
    }
    return (tok_nfd[lo][0] <= cp && cp <= tok_nfd[lo][1]) ? tok_nfd[lo][2] : cp;
}

static uint32_t cpt_lower(uint32_t cp)
{
    int lo = 0, hi = TOK_N_LOWER - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        if (tok_lower[mid][0] == cp)
            return tok_lower[mid][1];
        if (tok_lower[mid][0] < cp)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    return cp;
}

// unicode_cpt_from_utf8, with unicode_cpts_from_utf8's fallback: a bad
// sequence costs one byte and yields U+FFFD. Overlong forms and surrogates are
// accepted, as there.
static uint32_t utf8_next(const uint8_t *s, size_t len, size_t *i)
{
    size_t k = *i;
    uint8_t c = s[k];
    if (!(c & 0x80)) {
        *i = k + 1;
        return c;
    }
    if (c & 0x40) {
        if (!(c & 0x20)) {
            if (k + 1 < len && (s[k + 1] & 0xc0) == 0x80) {
                *i = k + 2;
                return ((c & 0x1f) << 6) | (s[k + 1] & 0x3f);
            }
        } else if (!(c & 0x10)) {
            if (k + 2 < len && (s[k + 1] & 0xc0) == 0x80 && (s[k + 2] & 0xc0) == 0x80) {
                *i = k + 3;
                return ((c & 0x0f) << 12) | ((s[k + 1] & 0x3f) << 6) | (s[k + 2] & 0x3f);
            }
        } else if (!(c & 0x08)) {
            if (k + 3 < len && (s[k + 1] & 0xc0) == 0x80 && (s[k + 2] & 0xc0) == 0x80 && (s[k + 3] & 0xc0) == 0x80) {
                *i = k + 4;
                return ((uint32_t)(c & 0x07) << 18) | ((s[k + 1] & 0x3f) << 12) | ((s[k + 2] & 0x3f) << 6) | (s[k + 3] & 0x3f);
            }
        }
    }
    *i = k + 1;
    return 0xFFFD;
}

static size_t utf8_put(uint32_t cp, char *o)
{
    if (cp < 0x80) {
        o[0] = (char)cp;
        return 1;
    }
    if (cp < 0x800) {
        o[0] = (char)(0xc0 | (cp >> 6));
        o[1] = (char)(0x80 | (cp & 0x3f));
        return 2;
    }
    if (cp < 0x10000) {
        o[0] = (char)(0xe0 | (cp >> 12));
        o[1] = (char)(0x80 | ((cp >> 6) & 0x3f));
        o[2] = (char)(0x80 | (cp & 0x3f));
        return 3;
    }
    o[0] = (char)(0xf0 | (cp >> 18));
    o[1] = (char)(0x80 | ((cp >> 12) & 0x3f));
    o[2] = (char)(0x80 | ((cp >> 6) & 0x3f));
    o[3] = (char)(0x80 | (cp & 0x3f));
    return 4;
}

// ---- tokenise --------------------------------------------------------------
typedef struct {
    const tok_vocab *v;
    int32_t *ids;       // text tokens go to ids[1 ..]
    int cap;            // how many text tokens fit
    int n;              // text tokens so far, including any past cap
    char *word;         // "▁" + current word
    size_t wlen;
} enc;

static void push(enc *e, int id)
{
    if (e->n < e->cap)
        e->ids[1 + e->n] = id;
    e->n++;
}

// llm_tokenizer_wpm_session::tokenize for one word: greedy longest match over
// bytes; if any position fails to match, the whole word becomes [UNK]
static void flush_word(enc *e)
{
    if (e->wlen == 3)
        return;
    const char *w = e->word;
    int n = (int)e->wlen, start = e->n;
    for (int i = 0; i < n; i++) {
        int j = i + e->v->max_len + 1;
        if (j > n)
            j = n;
        for (; j > i; j--) {
            int id = tok_lookup(e->v, w + i, j - i);
            if (id >= 0) {
                push(e, id);
                break;
            }
        }
        if (j == i) {
            e->n = start;
            break;
        }
        i = j - 1;
    }
    if (e->n == start)
        push(e, TOK_UNK);
    e->wlen = 3;
}

// llm_tokenizer_wpm_session::preprocess, streamed: whitespace ends a word,
// punctuation/ASCII symbols/CJK are words on their own
static int fragment(enc *e, const uint8_t *s, size_t len)
{
    for (size_t i = 0; i < len;) {
        uint32_t cp = cpt_nfd(utf8_next(s, len, &i));
        int c = cpt_class(cp);
        if (c == TOK_WS) {
            flush_word(e);
            continue;
        }
        if (c == TOK_DROP)
            continue;
        cp = cpt_lower(cp);
        if (cp > 0x10FFFF)
            return -2;                                  // unicode_cpt_to_utf8 throws
        if (c == TOK_SPLIT) {
            flush_word(e);
            e->wlen += utf8_put(cp, e->word + e->wlen);
            flush_word(e);
        } else {
            e->wlen += utf8_put(cp, e->word + e->wlen);
        }
    }
    flush_word(e);
    return 0;
}

int tok_encode(const tok_vocab *v, const char *text, size_t len,
               int32_t *ids, int max_ids, int *total)
{
    if (max_ids < 2)
        return -1;
    // a codepoint never grows past 4 bytes, and a byte is at least one codepoint
    enc e = {v, ids, max_ids - 2, 0, v->alloc(3 + 4 * len + 1), 3};
    if (!e.word)
        return -1;
    memcpy(e.word, "\xe2\x96\x81", 3);

    // tokenizer_st_partition: literal special-token text becomes that token.
    // The five never overlap, so one left-to-right scan matches llama.cpp's
    // token-by-token splitting.
    const uint8_t *s = (const uint8_t *)text;
    size_t a = 0;
    int r = 0;
    for (size_t i = 0; i < len && !r; i++) {
        if (s[i] != '[')
            continue;
        for (int k = 0; k < (int)N_SPECIAL; k++) {
            size_t l = strlen(SPECIAL[k]);
            if (i + l <= len && !memcmp(s + i, SPECIAL[k], l)) {
                r = fragment(&e, s + a, i - a);
                push(&e, tok_lookup(v, SPECIAL[k], l));
                a = i + l;
                i = a - 1;
                break;
            }
        }
    }
    if (!r)
        r = fragment(&e, s + a, len - a);
    v->free(e.word);
    if (r)
        return r;

    int n = e.n < e.cap ? e.n : e.cap;
    ids[0] = TOK_CLS;
    ids[1 + n] = TOK_SEP;
    if (total)
        *total = e.n + 2;
    return n + 2;
}
