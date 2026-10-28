"""Score an extractor's facts against the reference set with an LLM judge.

The judge is gpt-oss-120b on Bedrock: deliberately not one of the models being
compared, so it cannot favour its own phrasing. For each session it gets the
transcript, the reference facts and the extracted facts, and returns:

  gold:      each reference fact -> full | partial | missing
  memories:  each extracted fact -> correct | wrong | transient | advice | trivial
"""

import json
import os
import re

JUDGE_MODEL = os.environ.get("FACTS_EVAL_JUDGE", "openai.gpt-oss-120b-1:0")
VERDICTS = ["correct", "wrong", "transient", "advice", "trivial"]
GOLD_SCORE = {"full": 1.0, "partial": 0.5, "missing": 0.0}

PROMPT = """You are grading a memory-extraction system. It read a conversation between Martin and his AI assistant
and wrote "memories": standalone facts to remember about Martin, his machines and projects.

CONVERSATION (Martin = M, assistant = C):
{transcript}

REFERENCE FACTS a good extractor should capture:
{gold}

EXTRACTED MEMORIES (numbered):
{facts}

Return ONLY a JSON object, no prose:
{{"gold": {{"<reference id>": "full" | "partial" | "missing", ...one entry per reference fact}},
  "memories": [{{"n": <number>, "verdict": "correct" | "wrong" | "transient" | "advice" | "trivial"}}, ...one per memory]}}

Definitions:
- gold "full": some memory (or two together) states the reference fact's substance; "partial": only part of it.
- "wrong": contradicts the conversation or states something it does not support (wrong host, path, cause, number,
  or a date the conversation does not give).
- "transient": true only at that moment (current %, load, 'restarted 12 minutes ago', 'is offline now').
- "advice": only records that the assistant suggested or explained something, without a durable fact.
- "trivial": too vague or context-dependent to be useful on its own (e.g. 'Every box is back up').
- "correct": an accurate, durable, standalone fact.
"""


def transcript(session):
    out = []
    for i, ex in enumerate(session.exchanges, 1):
        out.append(f"--- exchange {i}")
        out += [("M: " if m["role"] == "user" else "C: ") + m["content"] for m in ex]
    return "\n".join(out)


def build_prompt(session, gold_facts, facts):
    return PROMPT.format(
        transcript=transcript(session),
        gold="\n".join(f"{k}: {v}" for k, v in gold_facts.items()),
        facts="\n".join(f"{n}. {f}" for n, f in enumerate(facts, 1)) or "(none)")


class JudgeError(ValueError):
    """The judge's reply held no usable verdict."""


def parse_verdict(text, gold_ids, n_facts):
    """Pull the judge's JSON out of its reply.

    A reply with no JSON object or no "gold" map raises JudgeError: a failed judge
    must never read as a bad extractor. Within a usable reply, a reference fact it
    skipped counts as missing and an extracted fact it skipped as unjudged.
    """
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError as e:
        raise JudgeError(f"judge reply is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("gold"), dict):
        raise JudgeError("judge reply has no gold verdicts")
    gold = {g: data["gold"].get(g, "missing") for g in gold_ids}
    by_n = {x.get("n"): x.get("verdict") for x in data.get("memories", []) if isinstance(x, dict)}
    memories = [by_n.get(n, "unjudged") for n in range(1, n_facts + 1)]
    return {"gold": gold, "memories": memories}


def judge(session, gold_facts, facts, client=None, attempts=3):
    """Judge one session. Retries an unusable reply; after the last attempt the session
    is marked judge_failed, its reference facts 'unjudged', and left out of recall."""
    if client is None:
        import boto3
        from botocore.config import Config

        client = boto3.Session().client(
            "bedrock-runtime", config=Config(read_timeout=600, retries={"max_attempts": 8}))
    prompt = build_prompt(session, gold_facts, facts)
    error = None
    for _ in range(attempts):
        r = client.converse(modelId=JUDGE_MODEL, messages=[{"role": "user", "content": [{"text": prompt}]}],
                            inferenceConfig={"maxTokens": 32000, "temperature": 0})
        text = "".join(b["text"] for b in r["output"]["message"]["content"] if "text" in b)
        try:
            return parse_verdict(text, list(gold_facts), len(facts))
        except JudgeError as e:
            error = f"{e} (stop: {r.get('stopReason')})"
    return {"gold": {g: "unjudged" for g in gold_facts}, "memories": ["unjudged"] * len(facts),
            "judge_failed": error}


def summarise(verdicts):
    """verdicts: list of judge() results for one run -> the numbers the comparison table shows."""
    judged = [v for v in verdicts if not v.get("judge_failed")]
    gold = [g for v in judged for g in v["gold"].values()]
    mems = [m for v in verdicts for m in v["memories"]]
    row = {
        "recall": sum(GOLD_SCORE.get(g, 0) for g in gold) / len(gold) if gold else 0.0,
        "full": gold.count("full"), "partial": gold.count("partial"), "missing": gold.count("missing"),
        "facts": len(mems),
    }
    row.update({v: mems.count(v) for v in VERDICTS})
    row["judge_failed"] = len(verdicts) - len(judged)  # sessions left out of recall
    return row
