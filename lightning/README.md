# Lightning talk — Conversation Memory

Materials for a ~15 minute talk to developers who already use AI-assisted coding
tools. The pitch is the **single-machine edition** in [`../localhost/`](../localhost/):
here is the thing, here is what it does for you, here is why you should install it.

## What's here

| File | What it is |
|---|---|
| `slides.html` | The deck. Open it in a browser — no build step, no dependencies. |
| `SPEAKER-NOTES.md` | Timed running order with what to actually say, plus likely questions. |
| `demo-script.md` | The live demo, the pre-flight checklist, and the fallback when it breaks. |
| `assets/` | Screenshots for the demo fallback (you need to take these). |

## Presenting

```bash
open lightning/slides.html
```

| Key | Does |
|---|---|
| `→` `space` `click` | Next slide |
| `←` | Previous slide |
| `Home` / `End` | First / last slide |
| `f` or the ⛶ button | Fullscreen |
| ◐ button | Force light/dark, if the projector washes one out |

The current slide is in the URL hash, so `slides.html#5` opens straight at the
demo — useful when rehearsing.

The deck follows the viewer's system theme by default. **Check which way the
projector renders it before you start**, and force the other with ◐ if the
contrast is poor. Dark tends to project better in a bright room.

## Shape of the talk

15 slides, ~13 minutes of speaking, 2 for questions.

1. **The problem** (slides 2–3) — context evaporates, and the reasoning we lose is
   the most valuable thing we produce.
2. **The thing** (4–7) — a hook, a local database, three MCP tools, and a demo.
3. **The objections** (8–10) — nothing leaves your laptop; and how it failed the
   first time, which is why the default is the safe one.
4. **The ask** (11–14) — one script, honest costs, where it pays off, install it.

Slides **3** and **9** are marked cuttable in the speaker notes. Drop both if you
are two minutes over; the argument survives.

## Adapting it

- **Shorter (5 min):** slides 1, 2, 4, 5, 8, 11, 14. The demo carries it.
- **Longer (30 min):** expand slide 10 into the full reliability story, and add
  the distributed edition from [`../README.md`](../README.md) as a "where this
  went next" coda.
- **Needs to be PowerPoint:** the content is all in `SPEAKER-NOTES.md` and the
  deck's markup; say the word and it can be converted.

## Before you present

- Work through the pre-flight checklist in [`demo-script.md`](demo-script.md).
- Rehearse the demo query **on the machine you'll present from**. Semantic search
  is not deterministic across rephrasings, and the one that worked at your desk
  may not be the one you type on stage.
- Look at what your demo query returns *around* the hit — the tools return the
  surrounding turns, and you are projecting your own history onto a wall.
