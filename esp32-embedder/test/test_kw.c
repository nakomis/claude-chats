// CHATS-5: the worker's shared helpers (worker/kw.c): base64 (RFC 4648's vectors, and age's
// strictness), JSON string escaping, the buffer.
#include "kw.h"
#include "testutil.h"

#include "cJSON.h"

int main(void)
{
    // RFC 4648 section 10
    static const char *const RFC[][2] = { { "", "" }, { "f", "Zg==" }, { "fo", "Zm8=" }, { "foo", "Zm9v" },
                                          { "foob", "Zm9vYg==" }, { "fooba", "Zm9vYmE=" }, { "foobar", "Zm9vYmFy" } };
    for (int i = 0; i < 7; i++) {
        char enc[16];
        uint8_t dec[16];
        b64_encode((const uint8_t *)RFC[i][0], strlen(RFC[i][0]), enc);
        CHECK(!strcmp(enc, RFC[i][1]), "encode %s -> %s", RFC[i][0], enc);
        long n = b64_decode(RFC[i][1], strlen(RFC[i][1]), dec, B64_PADDED);
        CHECK(n == (long)strlen(RFC[i][0]) && !memcmp(dec, RFC[i][0], (size_t)n), "decode %s", RFC[i][1]);
        // age's form: the same without the padding
        size_t raw = strcspn(RFC[i][1], "=");
        n = b64_decode(RFC[i][1], raw, dec, B64_RAW_STRICT);
        CHECK(n == (long)strlen(RFC[i][0]), "raw decode %.*s", (int)raw, RFC[i][1]);
    }
    uint8_t d[16];
    CHECK(b64_decode("Zg=", 3, d, B64_PADDED) == -1, "short padding");
    CHECK(b64_decode("Zg", 2, d, B64_PADDED) == -1, "missing padding");
    CHECK(b64_decode("Zg==", 4, d, B64_RAW_STRICT) == -1, "age: padding refused");
    CHECK(b64_decode("Zh", 2, d, B64_RAW_STRICT) == -1, "age: non-canonical trailing bits refused");
    CHECK(b64_decode("Zh==", 4, d, B64_PADDED) == -1, "non-canonical refused");
    CHECK(b64_decode("Z===", 4, d, B64_PADDED) == -1 && b64_decode("Z", 1, d, B64_RAW_STRICT) == -1, "one character");
    CHECK(b64_decode("Zm9v\n", 5, d, B64_RAW_STRICT) == -1 && b64_decode("Zm-v", 4, d, B64_PADDED) == -1, "bad characters");
    CHECK(b64_decode("Zm=v", 4, d, B64_PADDED) == -1, "padding inside");

    // JSON escaping round-trips through cJSON, every byte from 1 to 255 (UTF-8 aside, raw bytes pass)
    char all[128];
    for (int i = 1; i < 128; i++) all[i - 1] = (char)i;
    all[127] = 0;
    jbuf_t b;
    jb_init(&b);
    jb_puts(&b, "{\"s\":");
    jb_json_str(&b, all, 127);
    jb_puts(&b, "}");
    cJSON *j = cJSON_Parse(b.p);
    const char *s = cJSON_GetStringValue(cJSON_GetObjectItem(j, "s"));
    CHECK(s && !strcmp(s, all), "escaping round-trip: %s", b.p);
    cJSON_Delete(j);
    // printf, take, reset
    jb_reset(&b);
    jb_printf(&b, "%d-%s", 42, "x");
    for (int i = 0; i < 2000; i++) jb_putc(&b, 'y');
    size_t n;
    char *t = jb_take(&b, &n);
    CHECK(n == 2004 && !strncmp(t, "42-xyy", 6) && t[n] == 0 && !b.p, "take");
    kw_free(t);
    return report("kw");
}
