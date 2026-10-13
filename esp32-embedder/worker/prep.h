// The canonical text (CHATS-5): Kata must embed exactly what Cal embeds, or a message's vector and
// its query's are of different text. Cal's embedding consumer (home-infra cal/embedding-consumer):
//   1. sanitise_nuls: every NUL in the content becomes the two characters '\' '0'
//   2. ollama::truncate: the first 800 Unicode scalar values (Rust's chars(), not bytes)
//   3. Ollama tokenises, and over 512 tokens keeps [CLS] + the first 510 + [SEP] (truncate: true);
//      Kata's tokeniser does the same (tok.h)
//
// Step 1 happens on the job's JSON text, before it's parsed: the forwarder (serde_json) writes a
// NUL as the escape \u0000, and cJSON would end the string there. Rewriting that escape as \\0
// (JSON for '\' '0') gives the parsed content exactly sanitise_nuls's result.
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "kw.h"

#define PREP_MAX_CHARS 800      // Cal's EMBED_MAX_CHARS
#define PREP_MAX_TOKENS 512     // mxbai's context, as Ollama truncates

// The JSON text json[0 .. n) with every \u0000 escape inside a string replaced by \\0. Other
// escapes, and an escaped backslash followed by "u0000", are left alone. Returns how many NULs it
// replaced (Cal logs that), or -1 if out of memory.
int prep_json_nuls(const char *json, size_t n, jbuf_t *out);

// The byte length of the first max_chars characters of s[0 .. n) (UTF-8: a character starts at
// every byte that isn't a continuation byte, as Rust's char_indices() on valid UTF-8). *truncated
// says whether anything was cut.
size_t prep_truncate(const char *s, size_t n, size_t max_chars, bool *truncated);
