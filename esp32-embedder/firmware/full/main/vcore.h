#pragma once

// Raise the S3's digital core voltage for 240 MHz to at least min_mv (never raising it above 1.30 V,
// never lowering what ESP-IDF chose; only with a valid eFuse calibration). Call once, early in
// app_main. Not persistent: gone at the next reset.
// Prints what it did; returns the voltage now set, in mV.
int vcore_boost(int min_mv);
