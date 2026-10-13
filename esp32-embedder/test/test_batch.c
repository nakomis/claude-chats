// CHATS-5: the batching planner (worker/batch.c) on the host.
//   - hand-worked queues: the oldest always goes, later ones fill in order, a misfit is passed over,
//     the soft target stops a pass once reached
//   - random queues: every pick obeys the policy, written out again here the long way
//   - random queues drained pass by pass: every message goes, none waits more passes than there
//     were messages ahead of it
//
//     make test_batch && ./test_batch
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "batch.h"

static int fails;

static void expect(const char *what, const int *len, int n, int cap, int target, const int *want, int nw)
{
    int pick[64], np = batch_plan(len, n, cap, target, pick);
    int ok = np == nw && !memcmp(pick, want, nw * sizeof *want);
    fails += !ok;
    printf("%-44s ->", what);
    for (int i = 0; i < np; i++) printf(" %d", pick[i]);
    printf("%s\n", ok ? "" : "  FAIL");
}

// The policy, the long way: walk the queue keeping the running total; message i goes if it is the
// oldest, or if the total is still under the target and it fits under the cap.
static int obeys(const int *len, int n, int cap, int target, const int *pick, int np)
{
    if (n == 0) return np == 0;
    if (np < 1 || pick[0] != 0) return 0;
    int total = 0, p = 0;
    for (int i = 0; i < n; i++) {
        int go = i == 0 || (total < target && total < cap && total + len[i] <= cap);
        int took = p < np && pick[p] == i;
        if (go != took) return 0;
        if (took) { total += len[i]; p++; }
    }
    return p == np && (np == 1 || total <= cap);
}

static unsigned rng = 1;
static int rnd(int n) { rng = rng * 1103515245u + 12345u; return (int)((rng >> 8) % (unsigned)n); }

int main(void)
{
    // hand-worked
    { int l[] = { 13 }, w[] = { 0 }; expect("one message", l, 1, 512, 512, w, 1); }
    { expect("empty queue", NULL, 0, 512, 512, NULL, 0); }
    { int l[] = { 13, 10, 13, 115, 140 }, w[] = { 0, 1, 2, 3, 4 }; expect("the five test texts: all fit", l, 5, 512, 512, w, 5); }
    { int l[] = { 300, 250, 100, 50 }, w[] = { 0, 2, 3 }; expect("300 250 100 50: 250 passed over", l, 4, 512, 512, w, 3); }
    { int l[] = { 100, 100, 100, 100 }, w[] = { 0, 1, 2 }; expect("4 x 100, target 250: the crossing one goes", l, 4, 512, 250, w, 3); }
    { int l[] = { 256, 256, 10 }, w[] = { 0 }; expect("256 256 10, target 256: oldest reaches it", l, 3, 512, 256, w, 1); }
    { int l[] = { 512, 2 }, w[] = { 0 }; expect("a full 512 goes alone", l, 2, 512, 512, w, 1); }
    { int l[] = { 510, 2, 2 }, w[] = { 0, 1 }; expect("510 then 2 then 2: exactly 512", l, 3, 512, 512, w, 2); }
    { int l[] = { 2, 511, 400, 300, 110 }, w[] = { 0, 2, 4 }; expect("2 511 400 300 110", l, 5, 512, 512, w, 3); }

    // random queues against the policy written out
    int bad = 0, trials = 20000;
    for (int t = 0; t < trials; t++) {
        int n = rnd(40), len[40], pick[40];
        int cap = t % 3 ? 512 : 64 + rnd(449), target = t % 2 ? cap : 1 + rnd(cap + 50);
        for (int i = 0; i < n; i++) len[i] = 2 + (t % 4 ? rnd(cap - 1) : rnd(40));
        int np = batch_plan(len, n, cap, target, pick);
        bad += !obeys(len, n, cap, target, pick, np);
    }
    fails += bad != 0;
    printf("%d random queues: %d break the policy\n", trials, bad);

    // drain random queues: everything goes, and nothing waits longer than its place in the queue
    int late = 0, lost = 0, passes = 0, msgs = 0;
    for (int t = 0; t < 2000; t++) {
        int n = 1 + rnd(60), len[60], id[60], pass_of[60], qn = n, pick[60];
        for (int i = 0; i < n; i++) { len[i] = 2 + (t % 2 ? rnd(511) : rnd(60)); id[i] = i; pass_of[i] = -1; }
        int qlen[60];
        memcpy(qlen, len, sizeof len);
        for (int p = 0; qn; p++, passes++) {
            int np = batch_plan(qlen, qn, 512, t % 3 ? 512 : 200, pick), k = 0;
            for (int i = 0, j = 0; i < qn; i++) {
                if (j < np && pick[j] == i) { pass_of[id[i]] = p; j++; continue; }
                id[k] = id[i]; qlen[k] = qlen[i]; k++;
            }
            qn = k;
        }
        for (int i = 0; i < n; i++) {
            lost += pass_of[i] < 0;
            late += pass_of[i] > i;
        }
        msgs += n;
    }
    fails += late || lost;
    printf("2000 queues drained: %d messages in %d passes; %d lost, %d waited longer than their place\n",
           msgs, passes, lost, late);
    printf("%s\n", fails ? "FAILED" : "all passed");
    return fails != 0;
}
