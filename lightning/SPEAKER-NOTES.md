# Speaker notes — Conversation Memory

15 minutes. Roughly 13 minutes of talking, 2 for questions. Timings are per slide,
cumulative in the right-hand column.

If you are running long, **cut slide 3 and slide 9** — the talk still works. If you
are running short, the demo on slide 5 will absorb as much time as you give it.

| # | Slide | Time | Cum. |
|---|---|---|---|
| 1 | Title | 0:30 | 0:30 |
| 2 | You have had this conversation before | 1:15 | 1:45 |
| 3 | Design docs you never wrote *(cuttable)* | 0:45 | 2:30 |
| 4 | What I built | 1:15 | 3:45 |
| 5 | **Demo** | 2:30 | 6:15 |
| 6 | The three tools | 1:00 | 7:15 |
| 7 | Under the hood | 1:00 | 8:15 |
| 8 | Nothing leaves your laptop | 1:15 | 9:30 |
| 9 | The first version lost messages *(cuttable)* | 1:00 | 10:30 |
| 10 | Split what must not fail | 1:00 | 11:30 |
| 11 | One script | 0:45 | 12:15 |
| 12 | What it costs | 0:45 | 13:00 |
| 13 | Where it earns its keep | 0:45 | 13:45 |
| 14 | Take one thing away | 0:30 | 14:15 |
| 15 | Questions | — | — |

---

## 1 · Title — 0:30

> Quick show of hands: who's used Claude Code, or Copilot, or Cursor this week?
>
> Right — so you all know the feeling where you open a new session and it has no
> idea who you are. I got annoyed enough about that to fix it, and the fix turned
> out to be small enough to be worth fifteen minutes of your time.

Don't explain the architecture yet. Set the problem first.

## 2 · You have had this conversation before — 1:15

Work down the four, but **land the third one properly** — it's the one that gets
nods.

> The context window fills up, auto-compaction fires, and you get a summary. The
> summary is not the conversation. All the reasoning about *why* you rejected the
> other three approaches — gone.
>
> Next morning, new session, and you're explaining your own codebase again.
>
> And this one — six weeks ago you and Claude debugged this exact thing. You know
> you did. You can't find it, and neither can it.

## 3 · Design docs you never wrote — 0:45 *(cut if long)*

This is the emotional core of the talk. Say it slowly.

> Think about what's actually in those transcripts. Every trade-off you talked
> through out loud. Every "no, not like that, because the downstream is
> rate-limited". That's design documentation. Better than most design
> documentation, because you wrote it while you actually cared.
>
> And we throw it in the bin every time the session ends.

## 4 · What I built — 1:15

Keep this brisk — the demo does the persuading, not this slide.

> Three pieces. A hook that fires automatically, so there's no discipline
> required — you can't forget to save. A local Postgres that stores each message
> with an embedding. And an MCP server, which is the bit that matters: it doesn't
> show *you* the archive, it gives *Claude* the archive.
>
> That last distinction is the whole design. I don't search my history. I ask
> Claude a question and it decides to go and look.

## 5 · Demo — 2:30

**Switch to the terminal.** Full script in `demo-script.md`, including the
fallback if the room's machine misbehaves. The slide holds a mocked-up version of
the same exchange, so you can talk to it if the demo dies.

The beat to hit afterwards:

> Notice what I didn't do. I didn't grep. I didn't remember which repo it was in.
> I asked a question in English and it went and found a decision I'd forgotten I
> made.

## 6 · The three tools — 1:00

> `search_memory` is hybrid — semantic and full-text at the same time, results
> fused. Semantic catches "retry backoff" when you wrote "wait between attempts";
> full-text catches the exact error string you pasted. You want both.
>
> `get_conversation` is the compaction rescue. Session id in, whole transcript
> out, verbatim.
>
> `list_recent_sessions` is mostly for me on a Monday morning.

## 7 · Under the hood — 1:00

Walk the diagram left to right, once, then stop.

> Hook reads the transcript Claude Code already writes to disk. Embeds it locally.
> Writes to Postgres in a container. The MCP server reads the same database back.
>
> That's it. There's no service, no account, nothing to sign up for.

## 8 · Nothing leaves your laptop — 1:15

**Do not rush this one.** For a work audience it is the difference between "neat"
and "I'm allowed to install it".

> By default the embedding happens in Ollama, on your machine. Your conversations
> are not sent to me, they're not sent to Anthropic, they're not sent to a cloud
> region. The database is a container on localhost. The archive is a folder in
> your home directory.
>
> You *can* point it at Bedrock or OpenAI, and it's a one-line change — but be
> clear-eyed that if you do, you're sending message text to that provider to be
> embedded. The default is local precisely so you don't have to have that
> conversation with anyone.

Expect a question here. See "Likely questions" below.

## 9 · The first version lost messages — 1:00 *(cut if long)*

Self-deprecating, quick, and it buys credibility for slide 10.

> Worth admitting how the first version failed, because it's a mistake worth not
> repeating. The hook wrote straight to Postgres. If Docker happened to be down,
> the write threw, and there was a bare except that swallowed it.
>
> No error. No log line. The conversation just didn't exist. I found out weeks
> later, looking for something that was never saved. Silent data loss is the worst
> kind of bug — you don't get told, you just slowly stop trusting the thing.

## 10 · Split what must not fail — 1:00

> The fix wasn't to try harder at the write. It was to notice these are two jobs
> with two completely different requirements.
>
> Capture must not fail. So capture does the least possible: append to a SQLite
> file on local disk. No network, no credentials, no daemon — nothing that can be
> down.
>
> Delivery is allowed to fail. A drainer moves it into Postgres a couple of
> minutes later, and if that doesn't work the queue just gets longer and drains
> next time.
>
> That's durable mode, and it's the default. If you'd rather have fewer moving
> parts, direct mode writes straight through and you pick it at install time.

If anyone looks sceptical about the extra machinery: *"you get one launchd job.
That's the whole cost."*

## 11 · One script — 0:45

> Clone it, go into the localhost directory, run install.sh. It starts Postgres,
> pulls the model, registers the hook and the MCP server, and asks which capture
> mode you want.
>
> Restart Claude Code and it's already recording. Re-running is always safe, so if
> you want to change provider or mode later you just run it again.

## 12 · What it costs — 0:45

Being honest here is what makes the recommendation land.

> Vectors aren't free — budget a few hundred megabytes a year if you're heavy.
> There's a couple of minutes' lag before something is searchable, unless you
> force a drain.
>
> And the one to actually think about: it records everything you type at Claude,
> including what you paste. That's the point of it, but know that before you put
> it on a work machine.

## 13 · Where it earns its keep — 0:45

Pick **two** of the four and speak to them; don't read all four.

Recommended pair: *post-compaction* (universal pain) and *decision archaeology*
(the one that makes managers interested).

## 14 · Take one thing away — 0:30

> Even if you never install this, take this bit away. We are all generating an
> enormous amount of genuinely good reasoning through these tools, and almost all
> of us are throwing every word of it away.
>
> It's one Docker container to stop doing that.

## 15 · Questions

---

## Likely questions

**"Does this send my code anywhere?"**
> Not on the default setup. Ollama embeds locally, Postgres is a local container.
> The only way anything leaves is if you deliberately choose Bedrock or OpenAI as
> the embedding provider.

**"How is this different from just using `/resume`?"**
> Resume gets you one session back. This searches every session you've ever had,
> across every project, and Claude reaches for it on its own without you having to
> remember which one it was in.

**"Does it slow Claude Code down?"**
> In durable mode, no — the hook writes to a local file and exits, it doesn't wait
> on the model. In direct mode, stopping a session waits for embedding, which is
> typically a second or two.

**"What if I want to delete something?"**
> It's your Postgres. `DELETE FROM messages WHERE …`. There's no sync, nothing to
> revoke, no copy anywhere else.

**"Does it work with Cursor / Copilot / other tools?"**
> Not today — the capture side reads Claude Code's transcript format specifically.
> The storage and search half is generic, so another front end is a hook away, and
> I'd take the PR.

**"Why Postgres rather than a vector database?"**
> pgvector is genuinely good enough at this scale, and it means one container
> instead of two — plus full-text search comes free in the same query, which is
> what makes hybrid search cheap to build.

**"How big does it get?"**
> Text is small. The vectors dominate — a thousand-odd dimensions of float per
> message. Backups exclude them and regenerate on restore, which is a ten-times
> size difference.

**"Can several machines share one?"**
> That's what I actually run — the repo has a distributed edition with a queue and
> a shared database. It's considerably more to operate, and it is not what I'm
> recommending to you today. Start with the single-machine one.
