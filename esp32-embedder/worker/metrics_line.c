// CHATS-20. Copied from tinycrypt's embedded/port/metrics_line.c (CRYPT-28; CC0). See metrics_line.h.
#include "metrics_line.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

typedef struct
{
    char *p;
    size_t left;
    bool ok;
} sink_t;

static void put(sink_t *s, const char *str, size_t n)
{
    if (!s->ok || n >= s->left)
    {
        s->ok = false;
        return;
    }
    memcpy(s->p, str, n);
    s->p += n;
    s->left -= n;
    *s->p = 0;
}

static void put_str(sink_t *s, const char *str)
{
    put(s, str, strlen(str));
}

// A measurement name or tag value: escape what line protocol treats as syntax.
static bool put_escaped(sink_t *s, const char *str)
{
    if (str == NULL || *str == 0)
        return false;
    for (; *str; str++)
    {
        if (*str == ',' || *str == ' ' || *str == '=' || *str == '\\')
            put(s, "\\", 1);
        if ((unsigned char)*str < 0x20)
            return false;  // no control characters (a newline would start a new line)
        put(s, str, 1);
    }
    return true;
}

static bool plain_name(const char *name)
{
    if (name == NULL || *name == 0)
        return false;
    for (; *name; name++)
        if (!((*name >= 'a' && *name <= 'z') || (*name >= '0' && *name <= '9') || *name == '_'))
            return false;
    return true;
}

int metrics_line(char *out, size_t cap, const char *measurement, const char *host_name,
                 const char *version, const metrics_field_t *fields, size_t n)
{
    if (out == NULL || cap == 0 || n == 0 || !plain_name(measurement))
        return -1;
    sink_t s = {out, cap, true};
    *out = 0;
    put_str(&s, measurement);
    put_str(&s, ",host_name=");
    if (!put_escaped(&s, host_name))
        return -1;
    if (version != NULL)
    {
        put_str(&s, ",version=");
        if (!put_escaped(&s, version))
            return -1;
    }
    for (size_t i = 0; i < n; i++)
    {
        if (!plain_name(fields[i].name))
            return -1;
        char num[24];
        snprintf(num, sizeof(num), "=%" PRId64 "i", fields[i].value);
        put_str(&s, i == 0 ? " " : ",");
        put_str(&s, fields[i].name);
        put_str(&s, num);
    }
    put_str(&s, "\n");
    return s.ok ? (int)(cap - s.left) : -1;
}
