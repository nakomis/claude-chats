# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["mem0ai", "ollama", "qdrant-client", "boto3", "anthropic", "psycopg[binary]"]
# ///
"""Score a fact extractor against the reference set: one command.

  uv run facts_eval/run.py mem0:anthropic:claude-haiku-5-5        # extract, judge, print the table
  uv run facts_eval/run.py --compare                               # every stored run, side by side
  uv run facts_eval/run.py --export SESSION_ID [...]              # transcript text, for writing reference facts

The reference set and every run's output live in $FACTS_EVAL_DIR (default
ignored/facts-eval/ in this repo, which git ignores): reference facts name hosts,
addresses and paths, and the runs hold transcript text and raw model replies, none
of which belongs in a public repo. See facts_eval/README.md for the formats.

Needs: CM's database (CM_DATABASE_URL or --db), AWS credentials for the Bedrock
judge (AWS_PROFILE / AWS_REGION), and whatever the extractor calls (an
ANTHROPIC_API_KEY, Bedrock access, or a local Ollama).
"""

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))  # so module: specs can name repo packages, e.g. facts.extract

import export  # noqa: E402
import extractors  # noqa: E402
import judge  # noqa: E402

REPO = HERE.parent


def eval_dir():
    import os

    return Path(os.environ.get("FACTS_EVAL_DIR", REPO / "ignored" / "facts-eval"))


def load_reference(path):
    """{"sessions": [{"session_id", "name", "facts": {id: text}}]} -> list of those dicts."""
    data = json.loads(Path(path).read_text())
    ids = [f for s in data["sessions"] for f in s["facts"]]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"reference fact ids must be unique across sessions: {sorted(dupes)}")
    return data["sessions"]


def run_extractor(spec, sessions, out):
    ex = extractors.make(spec, out)
    rows = []
    for s in sessions:
        for i, exchange in enumerate(s.exchanges, 1):
            t = time.perf_counter()
            res = ex.add(s, i, exchange)
            row = {"session_id": s.session_id, "i": i, "seconds": time.perf_counter() - t, "error": res.error,
                   "facts": [asdict(f) for f in res.facts],
                   "calls": [{k: v for k, v in c.items() if k != "messages"} for c in res.calls]}
            row["parse_ok"] = [extractors.parses_as_json(c.get("response")) for c in res.calls]
            rows.append(row)
            with open(out / "raw.jsonl", "a") as f:  # every prompt and reply, for reading failures back
                f.write(json.dumps({**row, "prompts": [c.get("messages") for c in res.calls]}) + "\n")
            print(f"  {s.name[:28]:28} #{i:<3} {row['seconds']:6.1f} s  facts {len(res.facts):2}"
                  f"{'  ERROR ' + res.error[:60] if res.error else ''}", flush=True)
    return rows


def token_totals(rows):
    calls = [c for r in rows for c in r["calls"]]
    keys = ["uncached_in", "cache_read", "cache_write", "output_tokens"]
    return {k: sum(c.get(k) or 0 for c in calls) for k in keys}


def score(sessions, reference, rows):
    by_session = {s["session_id"]: s for s in reference}
    jobs = []
    for s in sessions:
        facts = [f["text"] for r in rows if r["session_id"] == s.session_id for f in r["facts"]]
        jobs.append((s, by_session[s.session_id]["facts"], facts))
    with ThreadPoolExecutor(8) as pool:
        verdicts = list(pool.map(lambda j: judge.judge(*j), jobs))
    return [{"session_id": s.session_id, "facts": facts, **v} for (s, _, facts), v in zip(jobs, verdicts)]


def print_table(summaries):
    cols = ["recall", "full", "partial", "missing", "facts"] + judge.VERDICTS + ["unparseable"]
    print(f"\n{'run':44} " + " ".join(f"{c:>9}" for c in cols))
    for name, s in summaries:
        cells = [f"{s['recall']:9.0%}"] + [f"{s.get(c, 0):9}" for c in cols[1:]]
        print(f"{name[:44]:44} " + " ".join(cells))


def stored_runs():
    runs = []
    for p in sorted((eval_dir() / "runs").glob("*/summary.json")):
        runs.append((p.parent.name, json.loads(p.read_text())))
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("extractor", nargs="?", help="mem0:<anthropic|bedrock|ollama>:<model> or module:<mod>:<factory>")
    ap.add_argument("--reference", help="reference set (default $FACTS_EVAL_DIR/reference.json)")
    ap.add_argument("--sessions", help="comma-separated session IDs: only these from the reference set")
    ap.add_argument("--label", help="suffix for the run's folder name")
    ap.add_argument("--db", help="CM database DSN (default $CM_DATABASE_URL or Luke)")
    ap.add_argument("--compare", action="store_true", help="print every stored run and exit")
    ap.add_argument("--export", nargs="+", metavar="SESSION_ID", help="print these sessions' exchanges and exit")
    args = ap.parse_args()

    if args.compare:
        print_table(stored_runs())
        return

    import psycopg

    conn = psycopg.connect(export.dsn(args.db), connect_timeout=10)

    if args.export:
        for sid in args.export:
            s = export.load_session(conn, sid)
            print(f"==================== {s.name} ({s.session_id}, started {s.started_at:%Y-%m-%d})")
            print(judge.transcript(s))
        return

    if not args.extractor:
        ap.error("give an extractor spec, --compare or --export")
    extractors.parse_spec(args.extractor)  # fail fast on a typo

    reference = load_reference(args.reference or eval_dir() / "reference.json")
    if args.sessions:
        wanted = set(args.sessions.split(","))
        reference = [s for s in reference if s["session_id"] in wanted]
    sessions = [export.load_session(conn, s["session_id"]) for s in reference]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"{stamp}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', args.extractor)}" + (f"-{args.label}" if args.label else "")
    out = eval_dir() / "runs" / name
    out.mkdir(parents=True)
    print(f"run {name}: {len(sessions)} sessions, {sum(len(s.exchanges) for s in sessions)} exchanges")

    rows = run_extractor(args.extractor, sessions, out)
    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    print("judging...", flush=True)
    verdicts = score(sessions, reference, rows)
    (out / "verdicts.json").write_text(json.dumps(verdicts, indent=1))

    summary = judge.summarise(verdicts)
    summary["unparseable"] = sum(1 for r in rows for ok in r["parse_ok"] if ok is False)
    summary["errors"] = sum(1 for r in rows if r["error"])
    summary["tokens"] = token_totals(rows)
    summary["extractor"] = args.extractor
    (out / "summary.json").write_text(json.dumps(summary, indent=1))

    print_table(stored_runs())
    print(f"\ntokens this run: {summary['tokens']}; results in {out}")


if __name__ == "__main__":
    main()
