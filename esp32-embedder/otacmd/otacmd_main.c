// otacmd: run Kata's Fleet command parser (ota_cmd.c) from the command line, so test_ota_cmd.py can
// feed it commands (CHATS-10). tinycrypt's sim/otacmd_main.c (2d490fd, CC0), less --approval.
//
//   cc -std=c11 -Wall -Wextra -Werror -o otacmd otacmd_main.c ota_cmd.c $(pkg-config --cflags --libs libcjson)
//
//   otacmd                        one JSON command per line on stdin; one JSON result per line
//                                 ({"_now": ms} sets the parser's clock, with no result)
//   otacmd --valid <version>      prints 1 or 0
//   otacmd --cmp <a> <b>          prints -1, 0 or 1
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "ota_cmd.h"

static const char *kind_name(ota_cmd_kind kind)
{
    switch (kind)
    {
    case OTA_CMD_GET_VERSION:
        return "get_version";
    case OTA_CMD_PING:
        return "ping";
    case OTA_CMD_CANCEL:
        return "cancel";
    case OTA_CMD_UPDATE:
        return "update";
    case OTA_CMD_CHUNK:
        return "chunk";
    case OTA_CMD_ERROR:
        return "error";
    }
    return "?";
}

int main(int argc, char *argv[])
{
    if (argc == 3 && strcmp(argv[1], "--valid") == 0)
    {
        printf("%d\n", ota_version_valid(argv[2]));
        return 0;
    }
    if (argc == 4 && strcmp(argv[1], "--cmp") == 0)
    {
        int c = ota_version_cmp(argv[2], argv[3]);
        printf("%d\n", c < 0 ? -1 : c > 0);
        return 0;
    }
    if (argc != 1)
    {
        fprintf(stderr, "usage: see the comment at the top of otacmd_main.c\n");
        return 2;
    }

    static ota_chunks chunks;
    static ota_cmd cmd;
    static char line[64 * 1024];
    uint32_t now_ms = 0;
    while (fgets(line, sizeof(line), stdin) != NULL)
    {
        cJSON *clock = cJSON_Parse(line);
        const cJSON *now = clock ? cJSON_GetObjectItemCaseSensitive(clock, "_now") : NULL;
        bool set_clock = cJSON_IsNumber(now);
        if (set_clock)
            now_ms = (uint32_t)now->valuedouble;
        cJSON_Delete(clock);
        if (set_clock)
            continue;
        ota_cmd_parse(&chunks, line, strlen(line), now_ms, &cmd);
        cJSON *out = cJSON_CreateObject();
        cJSON_AddStringToObject(out, "kind", kind_name(cmd.kind));
        if (cmd.kind == OTA_CMD_UPDATE)
        {
            char hex[2 * OTA_SHA256_LEN + 1];
            for (int i = 0; i < OTA_SHA256_LEN; i++)
                snprintf(&hex[2 * i], 3, "%02x", cmd.sha256[i]);
            cJSON_AddStringToObject(out, "url", cmd.url);
            cJSON_AddStringToObject(out, "version", cmd.version);
            cJSON_AddStringToObject(out, "sha256", hex);
        }
        else if (cmd.kind == OTA_CMD_CHUNK)
        {
            cJSON_AddNumberToObject(out, "index", cmd.chunk_index);
            cJSON_AddNumberToObject(out, "total", cmd.total_chunks);
        }
        else if (cmd.kind == OTA_CMD_ERROR)
        {
            cJSON_AddBoolToObject(out, "ota_error", cmd.ota_error);
            cJSON_AddStringToObject(out, "message", cmd.message);
        }
        char *text = cJSON_PrintUnformatted(out);
        printf("%s\n", text);
        fflush(stdout);
        cJSON_free(text);
        cJSON_Delete(out);
    }
    ota_cmd_reset(&chunks);
    return 0;
}
