// CHATS-5: the canonical text (worker/prep.c), against Cal's own tests: home-infra
// cal/embedding-consumer's sanitise_nuls tests (consumer.rs) and ollama::truncate's (ollama.rs),
// restated here through the JSON a job arrives as, since that's where Kata does step 1.
#include "kw.h"
#include "prep.h"
#include "testutil.h"

#include "cJSON.h"

// The job's JSON with NULs rewritten, parsed, then the content cut to max chars.
static char *canonical(const char *json, size_t max, int *nuls, bool *truncated)
{
    jbuf_t b;
    jb_init(&b);
    *nuls = prep_json_nuls(json, strlen(json), &b);
    cJSON *j = cJSON_Parse(b.p);
    jb_free(&b);
    const char *c = cJSON_GetStringValue(cJSON_GetObjectItem(j, "content"));
    char *out = c ? kw_strndup(c, prep_truncate(c, strlen(c), max, truncated)) : NULL;
    cJSON_Delete(j);
    return out;
}

static void nul_case(const char *json, const char *want, int want_nuls)
{
    int nuls;
    bool tr;
    char *got = canonical(json, PREP_MAX_CHARS, &nuls, &tr);
    CHECK(got && !strcmp(got, want) && nuls == want_nuls, "%s -> [%s] (%d NULs), want [%s] (%d)", json, got, nuls, want,
          want_nuls);
    kw_free(got);
}

static void trunc_case(const char *s, size_t max, const char *want, bool want_tr)
{
    bool tr;
    size_t n = prep_truncate(s, strlen(s), max, &tr);
    CHECK(n == strlen(want) && !memcmp(s, want, n) && tr == want_tr, "truncate(%s, %zu) -> [%.*s], want [%s]", s, max,
          (int)n, s, want);
}

int main(void)
{
    // sanitise_nuls (consumer.rs)
    nul_case("{\"content\":\"before\\u0000after\"}", "before\\0after", 1);           // nul_bytes_become_a_literal_backslash_zero
    nul_case("{\"content\":\"\\u0000\\u0000\\u0000\"}", "\\0\\0\\0", 3);            // every_nul_is_replaced_not_just_the_first
    nul_case("{\"content\":\"an ordinary tool result\"}", "an ordinary tool result", 0);
    nul_case("{\"content\":\"printf '\\\\0'\"}", "printf '\\0'", 0);                // a_literal_backslash_zero_..._is_not_doubled
    // and what's particular to doing it on the JSON
    nul_case("{\"content\":\"a\\\\u0000b\"}", "a\\u0000b", 0);                      // an escaped backslash, then "u0000": text
    nul_case("{\"content\":\"\\\\\\u0000\"}", "\\\\0", 1);                          // \\ then a real NUL
    nul_case("{\"content\":\"caf\\u00e9 \\u0000\\\"q\\\"\"}", "caf\xc3\xa9 \\0\"q\"", 1);
    nul_case("{\"other\":\"\\u0000\",\"content\":\"x\"}", "x", 1);                 // other fields are rewritten too (harmless)

    // ollama::truncate (ollama.rs)
    trunc_case("hello", 800, "hello", false);                                        // leaves_short_input_alone
    trunc_case("abcdef", 3, "abc", true);                                            // truncates_to_the_char_count
    trunc_case("abc", 3, "abc", false);                                              // boundary_is_exact_not_off_by_one
    trunc_case("abcd", 3, "abc", true);
    trunc_case("h\xc3\xa9llo", 2, "h\xc3\xa9", true);                                // never_splits_a_multibyte_codepoint
    const char *s = "h\xc3\xa9llo w\xc3\xb6rld";
    for (size_t n = 0; n <= 13; n++) {
        bool tr;
        size_t k = prep_truncate(s, strlen(s), n, &tr);
        CHECK(k <= strlen(s) && (k == strlen(s) || (s[k] & 0xc0) != 0x80), "split a character at %zu chars", n);
    }
    trunc_case("\xf0\x9f\x90\x88\xf0\x9f\x90\x88\xf0\x9f\x90\x88", 2, "\xf0\x9f\x90\x88\xf0\x9f\x90\x88", true);  // emoji
    trunc_case("", 800, "", false);
    trunc_case("e\xcc\x81\xcc\x81x", 2, "e\xcc\x81", true);                          // combining marks are characters too

    // 800 characters, not bytes: 1000 two-byte characters cut at 1600 bytes
    jbuf_t b;
    jb_init(&b);
    for (int i = 0; i < 1000; i++) jb_puts(&b, "\xc3\xa9");
    bool tr;
    CHECK(prep_truncate(b.p, b.n, PREP_MAX_CHARS, &tr) == 1600 && tr, "800 two-byte characters");
    CHECK(prep_truncate(b.p, 1600, PREP_MAX_CHARS, &tr) == 1600 && !tr, "exactly 800");
    jb_free(&b);
    return report("prep");
}
