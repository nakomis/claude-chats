// CHATS-5: the firmware's attention (kernels.inc) on the host, ESP-IDF stubbed out (stub/).
//   1. One segment: att_heads gives the same bits as the attention before CHATS-5 (att_old.inc),
//      at lengths either side of the QB and KC block edges.
//   2. Several segments: each segment's output has the same bits as a pass of that segment alone,
//      and matches a naive double-precision softmax(q k^T / 8) v over its own keys only (and, as a
//      check that the test can fail, not the same over every key of the pass).
//
//     make test_att && ./test_att
#include "esp_host.h"
#include "kernels.inc"
#include "att_old.inc"

static uint32_t rng = 12345;
static uint32_t rnd_u(void) { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng; }
static float rnd_n(void)                      // roughly N(0, 1)
{
    float s = 0;
    for (int i = 0; i < 12; i++) s += (rnd_u() >> 8) / 16777216.0f;
    return s - 6;
}

static float scr[ATT_SCR / sizeof(float)];

// as attention() does it, one half of the heads after the other
static void run(void (*f)(void *), uint16_t *q, const uint16_t *k, const uint16_t *v)
{
    att_t a = { q, k, v, scr, 0, HEADS / 2 }, b = { q, k, v, scr, HEADS / 2, HEADS };
    f(&a);
    f(&b);
}

// max |o - naive| / max(1, |naive|) over tokens t0 .. t1 - 1, the naive attention taking keys
// lo .. hi - 1. o is fp16, so half an ulp is 4.9e-4 of it.
static double err_naive(const uint16_t *o, const uint16_t *q, const uint16_t *k, const uint16_t *v,
                        int t0, int t1, int lo, int hi)
{
    static double p[MAXTOK];
    double worst = 0;
    for (int h = 0; h < HEADS; h++)
        for (int i = t0; i < t1; i++) {
            double m = -1e300, sum = 0;
            for (int j = lo; j < hi; j++) {
                double s = 0;
                for (int d = 0; d < HD; d++) s += (double)(h2f(q[(size_t)i * D + h * HD + d]) * 0.125f) * h2f(k[(size_t)j * D + h * HD + d]);
                p[j] = s;
                m = s > m ? s : m;
            }
            for (int j = lo; j < hi; j++) { p[j] = exp(p[j] - m); sum += p[j]; }
            for (int d = 0; d < HD; d++) {
                double r = 0;
                for (int j = lo; j < hi; j++) r += p[j] * h2f(v[(size_t)j * D + h * HD + d]);
                double want = r / sum, e = fabs(want - h2f(o[(size_t)i * D + h * HD + d])) / fmax(1, fabs(want));
                worst = e > worst ? e : worst;
            }
        }
    return worst;
}

int main(void)
{
    size_t bytes = (size_t)MAXTOK * D * sizeof(uint16_t);
    uint16_t *q = malloc(bytes), *k = malloc(bytes), *v = malloc(bytes), *o1 = malloc(bytes), *o2 = malloc(bytes);
    static uint16_t segs[MAXSEG + 1], alone[2];
    for (int i = 0; i < MAXTOK * D; i++) { q[i] = f2h(2 * rnd_n()); k[i] = f2h(rnd_n()); v[i] = f2h(rnd_n()); }
    int fails = 0;

    static const int lens[] = { 1, 2, 3, 4, 13, 31, 32, 33, 63, 64, 65, 100, 140, 257, 511, 512 };
    for (size_t c = 0; c < sizeof lens / sizeof *lens; c++) {
        int n = lens[c];
        memcpy(o1, q, (size_t)n * D * 2);
        memcpy(o2, q, (size_t)n * D * 2);
        n_tok = n; n_seg = 1; seg0 = alone; alone[0] = 0; alone[1] = n;
        run(att_heads, o1, k, v);
        run(att_heads_old, o2, k, v);
        int same = !memcmp(o1, o2, (size_t)n * D * 2);
        fails += !same;
        printf("one segment, %3d tokens: %s\n", n, same ? "same bits as before CHATS-5" : "DIFFERENT FROM BEFORE CHATS-5");
    }

    static const int fixed[][8] = { { 13, 10, 13, 115, 140 }, { 1, 1, 1, 1 }, { 2, 64, 65, 3 }, { 32, 32, 32 },
                                    { 200, 312 }, { 511, 1 }, { 1, 511 }, { 64, 64, 64, 64, 64, 64, 64, 64 } };
    int nfixed = sizeof fixed / sizeof *fixed;
    for (int c = 0; c < nfixed + 40; c++) {
        int len[MAXSEG], ns = 0, n = 0;
        if (c < nfixed) {
            for (; ns < 8 && fixed[c][ns]; ns++) n += len[ns] = fixed[c][ns];
        } else {                              // random: 2 to 40 segments, up to 512 tokens
            int want = 2 + rnd_u() % 39;
            while (ns < want) {
                int l = 2 + rnd_u() % (c % 2 ? 150 : 20);
                if (n + l > MAXTOK) break;
                n += len[ns++] = l;
            }
            if (ns < 2) { c--; continue; }
        }
        segs[0] = 0;
        for (int s = 0; s < ns; s++) segs[s + 1] = segs[s] + len[s];
        memcpy(o1, q, (size_t)n * D * 2);
        n_tok = n; n_seg = ns; seg0 = segs;
        run(att_heads, o1, k, v);
        int same = 1;
        double err = 0, err_all = 0;
        for (int s = 0; s < ns; s++) {
            int t0 = segs[s], t1 = segs[s + 1];
            memcpy(o2, q + (size_t)t0 * D, (size_t)len[s] * D * 2);
            n_tok = len[s]; n_seg = 1; seg0 = alone; alone[0] = 0; alone[1] = len[s];
            run(att_heads, o2, k + (size_t)t0 * D, v + (size_t)t0 * D);
            same &= !memcmp(o1 + (size_t)t0 * D, o2, (size_t)len[s] * D * 2);
            double e = err_naive(o1, q, k, v, t0, t1, t0, t1), ea = err_naive(o1, q, k, v, t0, t1, 0, n);
            err = e > err ? e : err;
            err_all = ea > err_all ? ea : err_all;
        }
        int ok = same && err < 1e-3 && err_all > 1e-2;
        fails += !ok;
        printf("%2d segments, %3d tokens: %s each alone; max error %.1e vs naive within segments, %.1e vs naive "
               "over the whole pass%s\n", ns, n, same ? "same bits as" : "DIFFERENT FROM", err, err_all, ok ? "" : "  FAIL");
    }
    printf("%s\n", fails ? "FAILED" : "all passed");
    return fails != 0;
}
