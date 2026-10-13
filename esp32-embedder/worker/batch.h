// CHATS-5: which queued messages go into the next pass.
//
// A pass reads every weight off the card whatever it carries, so while it is SD-bound (up to about
// 150 tokens on the 1-bit slot) another message in the same pass costs almost nothing, and past that
// it costs only its compute. So a pass starts as soon as one message is queued, and takes whatever
// else is waiting:
//   - the oldest message always goes
//   - then the rest of the queue in order, each message that still fits under cap tokens (512: one
//     pass's activations). One that doesn't fit is passed over, not a place to stop, and keeps its
//     place in the queue
//   - and no more once the pass holds target tokens or more. The target is soft: the message that
//     crosses it still goes, up to the cap. target >= cap packs as much as fits; a lower target keeps
//     passes short once compute, not the card, sets their length (a 4-bit card, CHATS-6), so the next
//     message waits less
// Every pass takes the oldest message, so one waits at most as many passes as there are messages
// ahead of it, however many small ones overtake it. A message is at most 512 tokens (the tokeniser
// truncates), so the oldest always fits on its own.
//
// Portable C99, no allocation.
#pragma once

// len[0 .. n): token counts of the queued messages, oldest first. Writes the queue indices of the
// messages to put in the next pass to pick[], in queue order (pick needs room for n), and returns how
// many: 0 if the queue is empty, otherwise at least 1 (pick[0] = 0).
int batch_plan(const int *len, int n, int cap, int target, int *pick);
