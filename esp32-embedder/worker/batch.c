// CHATS-5: the batching policy (batch.h). From draft PR #21.
#include "batch.h"

int batch_plan(const int *len, int n, int cap, int target, int *pick)
{
    if (n <= 0) return 0;
    int np = 0, total = len[0];
    pick[np++] = 0;                           // the oldest, always
    for (int i = 1; i < n && total < target && total < cap; i++)
        if (len[i] <= cap - total) { pick[np++] = i; total += len[i]; }
    return np;
}
