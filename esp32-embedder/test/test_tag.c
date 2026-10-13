// CHATS-5: the embedder tag follows the self-test's CRC table (firmware/full/main/selftest.h).
// When the table is re-recorded (the arithmetic changed), the tag's -vN must go up and
// EMBEDDER_TAG_TABLE_CRC be set to the new table's CRC; this fails until both are done.
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "selftest.h"

static uint32_t crc32(const uint8_t *p, size_t n)
{
    uint32_t c = 0xffffffffu;
    for (size_t i = 0; i < n; i++) {
        c ^= p[i];
        for (int k = 0; k < 8; k++) c = c >> 1 ^ (0xedb88320u & -(c & 1));
    }
    return ~c;
}

int main(void)
{
    uint8_t le[N_SELFTEST * 4];
    for (int i = 0; i < N_SELFTEST; i++)
        for (int b = 0; b < 4; b++) le[4 * i + b] = (uint8_t)(SELFTEST_CRC[i] >> (8 * b));
    uint32_t c = crc32(le, sizeof le);
    int zeros = 0;
    for (int i = 0; i < N_SELFTEST; i++) zeros += !SELFTEST_CRC[i];
    int ok = c == EMBEDDER_TAG_TABLE_CRC && !strncmp(EMBEDDER_TAG, "esp32s3-mxbai-int8-v", 20) && !zeros;
    printf("tag %s, table CRC %08x (pinned %08x), %d unrecorded: %s\n", EMBEDDER_TAG, (unsigned)c,
           (unsigned)EMBEDDER_TAG_TABLE_CRC, zeros, ok ? "ok" : "FAILED");
    if (c != EMBEDDER_TAG_TABLE_CRC)
        printf("selftest.h's CRC table changed: bump EMBEDDER_TAG's -vN and set EMBEDDER_TAG_TABLE_CRC to 0x%08xu\n", (unsigned)c);
    if (zeros) printf("a published build needs every self-test CRC recorded (the tag would name unverified arithmetic)\n");
    return !ok;
}
