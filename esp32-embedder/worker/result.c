// convmem_wire::KataResult v1 (result.h).
#include "result.h"

#include <string.h>

static void str_or_null(jbuf_t *b, const char *s)
{
    if (s) jb_json_str(b, s, strlen(s));
    else jb_puts(b, "null");
}

int result_encode(const kata_result_t *r, jbuf_t *b)
{
    jb_printf(b, "{\"v\":%d,\"kind\":\"%s\",\"message_uuid\":", RESULT_VERSION, r->embedded ? "embedded" : "unembedded");
    str_or_null(b, r->message_uuid);
    jb_puts(b, ",\"envelope\":");
    jb_put(b, r->envelope, r->envelope_len);
    jb_puts(b, ",\"embedder\":");
    str_or_null(b, r->embedded ? r->embedder : NULL);
    jb_puts(b, ",\"vector\":");
    if (r->embedded) {
        // little-endian f32, whatever the host's order (both the S3 and the tests' are little-endian)
        uint8_t *raw = kw_alloc(RESULT_DIMS * 4);     // off the (small) worker stack
        if (!raw) return -1;
        for (int i = 0; i < RESULT_DIMS; i++) {
            uint32_t u;
            memcpy(&u, &r->vector[i], 4);
            raw[4 * i] = (uint8_t)u; raw[4 * i + 1] = (uint8_t)(u >> 8);
            raw[4 * i + 2] = (uint8_t)(u >> 16); raw[4 * i + 3] = (uint8_t)(u >> 24);
        }
        jb_putc(b, '"');
        jb_b64(b, raw, RESULT_DIMS * 4);
        kw_free(raw);
        jb_putc(b, '"');
        jb_printf(b, ",\"tokens\":%d,\"truncated_chars\":%s,\"reason\":null", r->tokens, r->truncated_chars ? "true" : "false");
    } else {
        jb_puts(b, "null,\"tokens\":null,\"truncated_chars\":null,\"reason\":");
        str_or_null(b, r->reason);
    }
    jb_puts(b, ",\"board\":");
    str_or_null(b, r->board);
    jb_puts(b, ",\"fw\":");
    str_or_null(b, r->fw);
    if (r->has_pass) jb_printf(b, ",\"pass\":{\"n_seg\":%d,\"tokens\":%d,\"ms\":%lu}", r->pass_n_seg, r->pass_tokens,
                               (unsigned long)r->pass_ms);
    else jb_puts(b, ",\"pass\":null");
    jb_puts(b, ",\"ingest\":{\"message_id\":");
    str_or_null(b, r->message_id);
    if (r->receive_count > 0) jb_printf(b, ",\"receive_count\":%d}}", r->receive_count);
    else jb_puts(b, ",\"receive_count\":null}}");
    return b->oom ? -1 : 0;
}
