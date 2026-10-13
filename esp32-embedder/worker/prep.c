// The canonical text (prep.h).
#include "prep.h"

#include <string.h>

int prep_json_nuls(const char *json, size_t n, jbuf_t *out)
{
    int nuls = 0;
    bool in_str = false;
    size_t run = 0;
    for (size_t i = 0; i < n; i++) {
        char c = json[i];
        if (!in_str) {
            if (c == '"') in_str = true;
            continue;
        }
        if (c == '"') { in_str = false; continue; }
        if (c != '\\' || i + 1 >= n) continue;
        // an escape: \u0000 is the one to rewrite; anything else (\\, \", é...) is skipped whole
        if (json[i + 1] == 'u' && i + 5 < n && !memcmp(json + i + 2, "0000", 4)) {
            jb_put(out, json + run, i - run);
            jb_put(out, "\\\\0", 3);
            nuls++;
            i += 5;
            run = i + 1;
        } else {
            i++;                              // the escaped character
        }
    }
    jb_put(out, json + run, n - run);
    return out->oom ? -1 : nuls;
}

size_t prep_truncate(const char *s, size_t n, size_t max_chars, bool *truncated)
{
    size_t chars = 0;
    for (size_t i = 0; i < n; i++) {
        if (((unsigned char)s[i] & 0xc0) == 0x80) continue;   // a continuation byte
        if (chars == max_chars) {
            if (truncated) *truncated = true;
            return i;
        }
        chars++;
    }
    if (truncated) *truncated = false;
    return n;
}
