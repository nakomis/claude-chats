// CHATS-16: the portable half of Kata's LAN status endpoint (firmware/full/main/statusd.c). Leia's
// nginx terminates TLS for kata.home.nakomis.com and asks the board over plain HTTP; this is the
// little HTTP/1.x it needs: route a request head, write a response head, and decide who may ask.
// Portable C11, no ESP-IDF, so the host tests (../test/test_httpreq.c) run the same code.
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HTTPREQ_MAX_HEAD 1024           // a request head this long without its blank line is refused
#define HTTPREQ_MAX_ALLOW 4             // addresses in prov's status_allow

typedef enum {
    HTTPREQ_INCOMPLETE,                 // no blank line yet: read more
    HTTPREQ_JSON,                       // GET /json (any query string): 200
    HTTPREQ_NOT_FOUND,                  // GET anything else: 404
    HTTPREQ_BAD_METHOD,                 // a method other than GET: 405
    HTTPREQ_BAD,                        // malformed, or no blank line within HTTPREQ_MAX_HEAD: 400
} httpreq_route_t;

// Routes the request head in buf[0 .. n). Only the request line is looked at; headers are skipped.
httpreq_route_t httpreq_route(const char *buf, size_t n);
// The status code for a route (200, 404, 405 or 400); 0 for HTTPREQ_INCOMPLETE.
int httpreq_status(httpreq_route_t r);

// Writes a whole response head into out (always NUL-terminated): status line, Content-Type,
// Content-Length, Cache-Control: no-store, Connection: close, Allow: GET on a 405, and Retry-After if
// retry_after_s > 0. Returns its length, or 0 if it didn't fit in cap.
size_t httpreq_head(char *out, size_t cap, int status, const char *content_type, size_t content_length,
                    int retry_after_s);

// "a.b.c.d" (exactly) -> address as a host-order integer. False if malformed.
bool httpreq_parse_ip(const char *s, size_t n, uint32_t *ip);
// prov's status_allow: addresses separated by commas and/or spaces. Returns how many (up to max), or
// -1 if any is malformed, or there are more than max (statusd then refuses everyone, and says why:
// a list that didn't parse mustn't widen to the whole subnet).
int httpreq_parse_allow(const char *s, uint32_t *out, int max);
// Who may ask (host-order addresses). With an allow list (n_allow > 0) only those addresses; without
// one, any peer on the board's own subnet (self & mask). Never a peer when the board has no address.
bool httpreq_peer_ok(uint32_t peer, uint32_t self, uint32_t mask, const uint32_t *allow, int n_allow);
