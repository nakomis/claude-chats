#pragma once

// Raise the S3's digital core voltage for 240 MHz to at least min_mv (never above 1.30 V, never
// below what ESP-IDF chose). Call once, early in app_main. Not persistent: gone at the next reset.
// Prints what it did; returns the voltage now set, in mV.
int vcore_boost(int min_mv);
