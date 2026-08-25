# Demo script — slide 5

Two and a half minutes. The demo is the moment the talk either lands or doesn't,
so the priority is **that it works**, not that it is impressive.

## Before you leave the house

- [ ] `docker ps` shows `claude-chats-db` running.
- [ ] `ollama list` shows your embedding model.
- [ ] `cd localhost && ./install.sh --drain` — make sure nothing is stuck pending.
- [ ] Confirm the query below actually returns something good, **on the machine
      you will present from**. Rehearse the exact wording; semantic search is not
      deterministic across rephrasings.
- [ ] Terminal font size up to at least 18pt. Check it from the back of the room.
- [ ] Take the screenshots described under *Fallback* and put them in `assets/`.
- [ ] Close every other Claude Code session, so a stray hook can't fire mid-demo.

## The setup line

Say this while the terminal is coming up, so the silence is filled:

> This is a real session on my actual machine, against about a year of my own
> conversations. I have not staged the database.

## The run

Start a fresh Claude Code session **in a repo that is not this one** — the point
is that it reaches across projects.

Ask, out loud and typed:

```
What did we decide about retry backoff on the ingest worker, and why?
```

What to point at while it runs:

1. **The tool call.** `search_memory` appears in the transcript. Say: *"I didn't
   tell it to search. It decided the answer wasn't in this session."*
2. **The date and repo** on the result. Say: *"Different project, five months
   ago."*
3. **The reasoning, not just the conclusion.** Say: *"It's got the decision and
   the reason for the decision. That second part is what never makes it into a
   commit message."*

## The second beat, if time allows

This one gets a laugh and shows the range:

```
What was I working on last Tuesday?
```

`list_recent_sessions` comes back with project paths and titles. Say: *"That is a
standup update I did not have to write."*

## Fallback

If the network, the daemon, or the projector misbehaves — **do not debug on
stage.** Say "of course it does", and move to the mocked exchange on the slide,
which shows the same thing. Slide 5 is deliberately built to be talked through as
if it were the demo.

Screenshots to have ready in `assets/` as a second fallback:

| File | What it shows |
|---|---|
| `demo-search.png` | The `search_memory` call and its result, full terminal |
| `demo-recent.png` | `list_recent_sessions` output |

A screenshot on a slide beats a live demo that fails, and the audience will not
hold it against you. A live demo that hangs for ninety seconds, they will.

## What not to demo

- **Anything with a client name, credential, or private repo in it.** You are
  projecting your own conversation history onto a wall. Search for your demo
  query beforehand and look at what comes back *around* the hit — the tool
  returns the surrounding turns too.
- Installation. It takes ten minutes and most of it is Docker pulling an image.
  Slide 11 covers it in three lines.
