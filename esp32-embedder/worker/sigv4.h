// AWS Signature Version 4 (CHATS-5): signs Kata's SQS requests with the temporary credentials the
// IoT credentials provider hands out (creds.h). Header signing only (no presigned URLs, no S3
// chunked uploads), which is all SQS's JSON protocol needs. HMAC-SHA-256 and SHA-256 are mbedTLS's.
//
// Tested against AWS's SigV4 test suite (../test/sigv4/, from botocore): canonical request,
// string to sign and Authorization header, byte for byte.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "kw.h"

typedef struct {
    const char *name, *value;  // as sent; signing lower-cases names and trims and folds values
} sv4_hdr_t;

typedef struct {
    const char *method;        // "POST"
    const char *path;          // "/" or "/123/queue": raw, each byte outside A-Za-z0-9-._~/ is %-encoded
    const char *query;         // "a=1&b=2" (raw or %-encoded), or NULL
    const sv4_hdr_t *hdrs;     // every header to sign: must include host and x-amz-date (and
    int n_hdrs;                //   x-amz-security-token with temporary credentials)
    const uint8_t *body;
    size_t body_len;
} sv4_req_t;

typedef struct {
    const char *access_key, *secret_key;
    const char *region, *service;   // "eu-west-2", "sqs"
} sv4_key_t;

// Signs r at amz_date ("YYYYMMDDTHHMMSSZ", the request's own x-amz-date). Writes the Authorization
// header's value to authz (NUL-terminated). The canonical request and string to sign go to creq and
// sts if they're not NULL (for the tests). 0, or -1 (out of memory, or authz too short).
int sv4_sign(const sv4_req_t *r, const sv4_key_t *k, const char *amz_date, char *authz, size_t authz_len,
             jbuf_t *creq, jbuf_t *sts);

// SHA-256 of data as lower-case hex (65 bytes with the NUL).
void sv4_sha256_hex(const void *data, size_t n, char out[65]);
