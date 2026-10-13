// Shared by the worker's host tests: file reading and a CHECK that counts failures.
#pragma once

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fails, checks;

#define CHECK(cond, ...)                                                    \
    do {                                                                    \
        checks++;                                                           \
        if (!(cond)) {                                                      \
            fails++;                                                        \
            printf("FAIL %s:%d: %s: ", __FILE__, __LINE__, #cond);          \
            printf(__VA_ARGS__);                                            \
            printf("\n");                                                   \
        }                                                                   \
    } while (0)

// The whole file, NUL-terminated (the NUL isn't counted in *len). NULL if it can't be read.
static inline char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *p = malloc((size_t)n + 1);
    if (p && fread(p, 1, (size_t)n, f) != (size_t)n) { free(p); p = NULL; }
    fclose(f);
    if (!p) return NULL;
    p[n] = 0;
    if (len) *len = (size_t)n;
    return p;
}

static inline char *read_file_or_die(const char *path, size_t *len)
{
    char *p = read_file(path, len);
    if (!p) { printf("can't read %s\n", path); exit(2); }
    return p;
}

static inline int report(const char *name)
{
    printf("%s: %d checks, %s\n", name, checks, fails ? "FAILED" : "all passed");
    if (fails) printf("%d failures\n", fails);
    return fails != 0;
}
