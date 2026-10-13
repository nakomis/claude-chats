// Host test for tok.c on a tiny made-up vocab (CHATS-10): enough to run in CI, which has neither
// mxbai.bin's real vocab nor llama-server. The real check, id for id against llama-server on
// thousands of messages, is host/test_tok.py.
//
//     cc -std=c99 -Wall -Wextra -Werror -o test_tok test_tok.c tok.c && ./test_tok
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tok.h"

enum { HELLO = 1000, WORLD, S, COMMA, CAFE, BANG, N };

static int fail;

static void check(const tok_vocab *v, const char *text, int max_ids, const int32_t *want, int n_want, int want_total)
{
    int32_t ids[64];
    int total = -1;
    int n = tok_encode(v, text, strlen(text), ids, max_ids, &total);
    int ok = n == n_want && total == want_total && !memcmp(ids, want, n_want * sizeof *ids);
    printf("%s %-28s ->", ok ? "ok  " : "FAIL", text);
    for (int i = 0; i < n; i++) printf(" %ld", (long)ids[i]);
    printf("  (total %d)\n", total);
    fail += !ok;
}

int main(void)
{
    // HF form, as in mxbai.bin: "word" starts a word, "##piece" continues one, "[X]" is special.
    static const char *words[N];
    for (int i = 0; i < N; i++) words[i] = "[unused]";
    words[0] = "[PAD]";
    words[TOK_UNK] = "[UNK]";
    words[TOK_CLS] = "[CLS]";
    words[TOK_SEP] = "[SEP]";
    words[103] = "[MASK]";
    words[HELLO] = "hello";
    words[WORLD] = "world";
    words[S] = "##s";
    words[COMMA] = ",";
    words[CAFE] = "cafe";
    words[BANG] = "!";
    size_t len = 0;
    for (int i = 0; i < N; i++) len += strlen(words[i]) + 1;
    char *hf = malloc(len), *p = hf;
    for (int i = 0; i < N; i++) p = memcpy(p, words[i], strlen(words[i]) + 1), p += strlen(words[i]) + 1;

    tok_vocab v;
    if (tok_init(&v, hf, len, N, malloc, free)) { printf("tok_init failed\n"); return 1; }
    if (tok_lookup(&v, "\xe2\x96\x81hello", 8) != HELLO || tok_lookup(&v, "s", 1) != S) {
        printf("FAIL lookup\n");
        fail++;
    }

    // Lower-casing, punctuation split, a continuation piece.
    check(&v, "Hello, worlds!", 64, (const int32_t[]){ TOK_CLS, HELLO, COMMA, WORLD, S, BANG, TOK_SEP }, 7, 7);
    // NFD with the accent dropped.
    check(&v, "Caf\xc3\xa9 hello", 64, (const int32_t[]){ TOK_CLS, CAFE, HELLO, TOK_SEP }, 4, 4);
    // A literal special in the text is the special token.
    check(&v, "hello [SEP] world", 64, (const int32_t[]){ TOK_CLS, HELLO, TOK_SEP, WORLD, TOK_SEP }, 5, 5);
    // No match at all.
    check(&v, "xyzzy", 64, (const int32_t[]){ TOK_CLS, TOK_UNK, TOK_SEP }, 3, 3);
    // Over the limit: [CLS] + the first max_ids - 2 + [SEP], and the untruncated total.
    check(&v, "hello world hello world", 4, (const int32_t[]){ TOK_CLS, HELLO, WORLD, TOK_SEP }, 4, 6);
    // Empty.
    check(&v, "", 64, (const int32_t[]){ TOK_CLS, TOK_SEP }, 2, 2);

    tok_free(&v);
    free(hf);
    printf(fail ? "%d FAILED\n" : "all passed\n", fail);
    return fail != 0;
}
