// CHATS-20: the metrics line (worker/metrics_line.c), InfluxDB line protocol for VictoriaMetrics.
// tinycrypt's test_metrics_line.py (CRYPT-28), as C cases.
#include <stdint.h>
#include <string.h>

#include "metrics_line.h"
#include "testutil.h"

#define LINE(buf, cap, host, ver, fields) \
    metrics_line(buf, cap, "kata", host, ver, fields, sizeof(fields) / sizeof((fields)[0]))

int main(void)
{
    char out[512];

    metrics_field_t f1[] = {{"heap_free", 123456}, {"rssi", -61}, {"worker_running", 1}};
    int n = LINE(out, sizeof out, "kata-prod-kata", "0.3.1", f1);
    CHECK(n > 0 && !strcmp(out, "kata,host_name=kata-prod-kata,version=0.3.1 heap_free=123456i,rssi=-61i,worker_running=1i\n"),
          "got %s", out);
    CHECK(n == (int)strlen(out), "length %d", n);

    metrics_field_t f2[] = {{"uptime_s", 5}};
    n = LINE(out, sizeof out, "kata", NULL, f2);
    CHECK(n > 0 && !strcmp(out, "kata,host_name=kata uptime_s=5i\n"), "version is optional: %s", out);

    // comma, space, equals and backslash are syntax in a tag value
    metrics_field_t f3[] = {{"x", 1}};
    n = LINE(out, sizeof out, "a,b c=d\\e", NULL, f3);
    CHECK(n > 0 && !strcmp(out, "kata,host_name=a\\,b\\ c\\=d\\\\e x=1i\n"), "escaping: %s", out);

    CHECK(LINE(out, sizeof out, "", NULL, f3) == -1, "empty host");
    CHECK(LINE(out, sizeof out, "new\nline", NULL, f3) == -1, "newline in a tag");
    CHECK(LINE(out, sizeof out, NULL, NULL, f3) == -1, "no host");
    CHECK(LINE(out, sizeof out, "k", "", f3) == -1, "empty version");

    const char *bad[] = {"Heap", "heap free", "heap,free", "heap-free", "", NULL};
    for (size_t i = 0; i < sizeof bad / sizeof *bad; i++) {
        metrics_field_t f[] = {{bad[i], 1}};
        CHECK(LINE(out, sizeof out, "k", NULL, f) == -1, "field name %s", bad[i] ? bad[i] : "(null)");
    }
    CHECK(metrics_line(out, sizeof out, "kata", "k", NULL, NULL, 0) == -1, "no fields");
    metrics_field_t f4[] = {{"x", 1}};
    CHECK(metrics_line(out, sizeof out, "Kata", "k", NULL, f4, 1) == -1, "measurement must be plain");

    // a line that doesn't fit is refused, not truncated: exactly len + 1 fits, len doesn't
    metrics_field_t f5[] = {{"heap_free", 123456}};
    n = LINE(out, sizeof out, "kata-prod-kata", "0.3.1", f5);
    char tight[512];
    CHECK(LINE(tight, (size_t)n + 1, "kata-prod-kata", "0.3.1", f5) == n, "exact fit");
    CHECK(LINE(tight, (size_t)n, "kata-prod-kata", "0.3.1", f5) == -1, "one short");
    CHECK(LINE(NULL, 10, "k", NULL, f5) == -1 && LINE(out, 0, "k", NULL, f5) == -1, "no buffer");

    metrics_field_t f6[] = {{"a", INT64_MIN}, {"b", INT64_MAX}};
    n = LINE(out, sizeof out, "k", NULL, f6);
    CHECK(n > 0 && !strcmp(out, "kata,host_name=k a=-9223372036854775808i,b=9223372036854775807i\n"), "extremes: %s", out);

    printf("metrics_line: %d checks, %d failed\n", checks, fails);
    return fails != 0;
}
