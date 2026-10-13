// Known-good output vectors, as CRC32 of the [CLS] row (1024 fp32): tests 0..4 (mxtest.bin), then the
// long texts (longtest.h). Every healthy board produces these bits exactly; a chip short of core
// voltage (vcore.c) or anything else wrong changes them. 0 = not recorded yet (printed, not checked).
// They change whenever the arithmetic does (the kernels, ESP-IDF, GCC or the optimisation level):
// re-record from a known-good board's log with host/selftest_crcs.py, checked on two boards.
#define N_SELFTEST 7
static const uint32_t SELFTEST_CRC[N_SELFTEST] = { 0, 0, 0, 0, 0, 0, 0 };
