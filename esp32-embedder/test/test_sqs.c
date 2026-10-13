// CHATS-5: the SQS client (worker/sqs.c) against the fake SQS (fake_sqs.c), which checks every
// request's SigV4 signature by signing it again.
#include "fake_sqs.h"
#include "kw.h"
#include "sqs.h"
#include "testutil.h"

#define INGEST "https://sqs.eu-west-2.amazonaws.com/000000000000/conversation-memory-test"
#define RESULTS "https://sqs.eu-west-2.amazonaws.com/000000000000/conversation-memory-results-test"

int main(void)
{
    aws_creds_t creds = { "ASIATESTONLY", "secretTESTONLY", NULL, 2000000000 };
    creds.token = kw_strndup("tokenTESTONLY", 13);
    fake_sqs_t f;
    fq_init(&f, &creds, 1760370000);
    sqs_t s;
    CHECK(sqs_init(&s, "https://example.com/1/q", &creds, fq_now, fq_post, &f) == -1, "a non-SQS URL is refused");
    CHECK(sqs_init(&s, INGEST, &creds, fq_now, fq_post, &f) == 0, "init");
    CHECK(!strcmp(s.host, "sqs.eu-west-2.amazonaws.com") && !strcmp(s.region, "eu-west-2"), "%s %s", s.host, s.region);

    // empty queue: a long poll returns nothing, after waiting
    sqs_msg_t *m;
    int n;
    int64_t t0 = f.now;
    CHECK(sqs_receive(&s, INGEST, 3, 20, 900, &m, &n) == 0 && n == 0 && !m, "empty receive");
    CHECK(f.now == t0 + 20, "the fake long poll waits");
    CHECK(f.bad_signatures == 0, "signature");

    // bodies with every kind of character JSON has to escape survive the round trip
    const char *b1 = "{\"v\":1,\"message_uuid\":\"u-1\",\"ciphertext\":\"YWJj\"}";
    const char *b2 = "quotes \" and \\ backslashes, a tab\t, a newline\n, caf\xc3\xa9 \xe2\x98\x95 and \x01";
    fq_send(&f, INGEST, b1);
    fq_send(&f, INGEST, b2);
    fq_send(&f, INGEST, "third");
    CHECK(sqs_receive(&s, INGEST, 2, 0, 900, &m, &n) == 0 && n == 2, "receive 2: %d (%s)", n, s.last_error);
    CHECK(n == 2 && !strcmp(m[0].body, b1) && m[0].body_len == strlen(b1), "body 1");
    CHECK(n == 2 && !strcmp(m[1].body, b2), "body 2: [%s]", n == 2 ? m[1].body : "");
    CHECK(n == 2 && m[0].receive_count == 1 && m[0].receipt && m[0].message_id, "attributes");
    fq_queue_t *q = fq_queue(&f, INGEST);
    CHECK(fq_visible(&f, q) == 1, "two in flight, one visible");

    // visibility: release one (0), extend the other
    const char *rh[2] = { m[0].receipt, m[1].receipt };
    bool ok[10];
    CHECK(sqs_visibility_batch(&s, INGEST, rh, 1, 0, ok) == 0 && ok[0], "release");
    CHECK(fq_visible(&f, q) == 2, "released");
    // receive it again: count 2, a new receipt; the old one is stale
    sqs_msg_t *m2;
    int n2;
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m2, &n2) == 0 && n2 == 1 && m2[0].receive_count == 2, "redelivered");
    CHECK(n2 == 1 && strcmp(m2[0].receipt, m[0].receipt) != 0, "a new receipt handle");
    // a stale handle can't change visibility; deleting with it "succeeds" but doesn't delete (as SQS)
    CHECK(sqs_visibility_batch(&s, INGEST, rh, 1, 900, ok) == 0 && !ok[0], "stale handle refused");
    CHECK(sqs_delete_batch(&s, INGEST, rh, 1, ok) == 0 && ok[0] && fq_live(q) == 3, "stale delete deletes nothing");
    const char *fresh[2] = { m2[0].receipt, m[1].receipt };
    CHECK(sqs_delete_batch(&s, INGEST, fresh, 2, ok) == 0 && ok[0] && ok[1] && fq_live(q) == 1, "delete 2");
    for (int i = 0; i < n; i++) sqs_msg_free(&m[i]);
    kw_free(m);
    sqs_msg_free(&m2[0]);
    kw_free(m2);

    // send a batch to the results queue
    const char *bodies[3] = { b1, b2, "x" };
    size_t lens[3] = { strlen(b1), strlen(b2), 1 };
    CHECK(sqs_send_batch(&s, RESULTS, bodies, lens, 3, ok) == 0 && ok[0] && ok[1] && ok[2], "send 3: %s", s.last_error);
    fq_queue_t *rq = fq_queue(&f, RESULTS);
    CHECK(rq->n == 3 && !strcmp(rq->msgs[1].body, b2), "sent bodies arrive intact");
    // one refused entry: only it is not ok
    f.refuse_body_containing = "backslashes";
    CHECK(sqs_send_batch(&s, RESULTS, bodies, lens, 3, ok) == 0 && ok[0] && !ok[1] && ok[2], "partial failure");
    f.refuse_body_containing = NULL;
    // too much for one batch is refused before it's sent
    char *big = malloc(200001);
    memset(big, 'A', 200000);
    big[200000] = 0;
    const char *two_big[2] = { big, big };
    size_t big_lens[2] = { 200000, 200000 };
    int sends = f.calls[1];
    CHECK(sqs_send_batch(&s, RESULTS, two_big, big_lens, 2, ok) == -1 && !ok[0] && !ok[1] && f.calls[1] == sends,
          "an over-256 KiB batch should be refused locally");
    CHECK(sqs_send_batch(&s, RESULTS, two_big, big_lens, 1, ok) == 0 && ok[0], "one 200 KB message alone");
    free(big);

    // faults: no response, HTTP 500 (with the error decoded), and a bad MD5 on receive
    f.fail_next = 1;
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m, &n) == -1 && s.last_status == -1, "no response");
    f.http_500_next = 1;
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m, &n) == -1 && s.last_status == 500 && strstr(s.last_error, "InternalError"),
          "500: %s", s.last_error);
    f.corrupt_md5 = true;
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m, &n) == 0 && n == 0, "a body failing its MD5 is dropped");
    f.corrupt_md5 = false;
    // an SQS that sends more than asked: the client keeps only what it asked for
    for (int i = 0; i < 4; i++) fq_send(&f, INGEST, "extra");
    f.over_deliver = 3;
    CHECK(sqs_receive(&s, INGEST, 2, 0, 900, &m, &n) == 0 && n == 2, "over-delivery capped at max: %d", n);
    for (int i = 0; i < n; i++) sqs_msg_free(&m[i]);
    kw_free(m);
    f.over_deliver = 0;
    f.now += 1000;                            // the extras come back
    CHECK(sqs_receive(&s, INGEST, 10, 0, 900, &m, &n) == 0 && n == 5, "the rest come back: %d", n);
    for (int i = 0; i < n; i++) sqs_msg_free(&m[i]);
    kw_free(m);
    // wrong credentials: SQS (the fake) refuses the signature
    aws_creds_t wrong = creds;
    wrong.secret_key[0] ^= 1;
    s.creds = &wrong;
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m, &n) == -1 && s.last_status == 403 &&
          strstr(s.last_error, "SignatureDoesNotMatch"), "403: %s", s.last_error);
    s.creds = &creds;
    f.bad_signatures = 0;
    f.now += 1000;                            // the dropped message is visible again
    CHECK(sqs_receive(&s, INGEST, 1, 0, 900, &m, &n) == 0 && n == 1, "the dropped message comes back");
    if (n) { sqs_msg_free(&m[0]); kw_free(m); }
    CHECK(f.bad_signatures == 0, "every request was correctly signed");

    sqs_free(&s);
    fq_free(&f);
    creds_clear(&creds);
    return report("sqs");
}
