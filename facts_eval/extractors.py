"""Extractors the harness can score, chosen by a spec string.

  mem0:anthropic:<model>   Mem0 2.x with its LLM calls sent to the Anthropic API
  mem0:bedrock:<model-id>  Mem0 2.x with its LLM calls sent to Bedrock's Converse API
  mem0:ollama:<model>      Mem0 2.x on a local Ollama model (context window raised to 12k)
  module:<module>:<factory>  our own extractor: factory(run_dir) returns an object with add()

Every extractor is used the same way: make it once per run, then call
add(session, index, exchange) for each exchange in order, which returns the facts
it extracted plus a record of the LLM call (tokens, raw reply, stop reason).
"""

import importlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Fact:
    text: str
    kind: str | None = None
    subject: str | None = None
    attribute: str | None = None


@dataclass
class AddResult:
    facts: list[Fact]
    calls: list[dict] = field(default_factory=list)  # one per LLM call: tokens, response, stop, ...
    error: str | None = None


def parse_spec(spec: str):
    """'mem0:anthropic:claude-haiku-5-5' -> ('mem0', 'anthropic', 'claude-haiku-5-5')."""
    kind, _, rest = spec.partition(":")
    if kind == "mem0":
        backend, _, model = rest.partition(":")
        if backend not in ("anthropic", "bedrock", "ollama") or not model:
            raise ValueError(f"bad mem0 spec {spec!r}: want mem0:<anthropic|bedrock|ollama>:<model>")
        return kind, backend, model
    if kind == "module":
        module, _, factory = rest.rpartition(":")
        if not module or not factory:
            raise ValueError(f"bad module spec {spec!r}: want module:<python.module>:<factory>")
        return kind, module, factory
    raise ValueError(f"unknown extractor spec {spec!r}")


def make(spec: str, run_dir: Path):
    kind, a, b = parse_spec(spec)
    if kind == "module":
        return getattr(importlib.import_module(a), b)(run_dir)
    return Mem0Extractor(a, b, run_dir)


# --- Mem0 baseline ------------------------------------------------------------

OLLAMA_NUM_CTX = 12288  # Mem0's prompt is ~9k tokens; Ollama's default 2048 silently truncates it


def _anthropic_llm(model_id, calls):
    """Mem0's generate_response via the Anthropic API: effort low, Mem0's fixed system prompt cached."""
    import anthropic

    client = anthropic.Anthropic(max_retries=6)

    def generate_response(messages, *a, **k):
        system = [{"type": "text", "text": x["content"]} for x in messages if x["role"] == "system"]
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        turns = [{"role": x["role"], "content": x["content"]} for x in messages if x["role"] != "system"]
        t = time.perf_counter()
        r = client.messages.create(model=model_id, max_tokens=8000, system=system, messages=turns,
                                   output_config={"effort": "low"})
        text = "".join(blk.text for blk in r.content if blk.type == "text")
        u = r.usage
        calls.append({"uncached_in": u.input_tokens, "cache_read": u.cache_read_input_tokens,
                      "cache_write": u.cache_creation_input_tokens, "output_tokens": u.output_tokens,
                      "seconds": time.perf_counter() - t, "stop": r.stop_reason,
                      "response": text, "messages": messages})
        return text

    return generate_response


def _bedrock_llm(model_id, calls):
    """Mem0's generate_response via Bedrock's Converse API (one request shape for every model)."""
    import boto3
    from botocore.config import Config

    br = boto3.Session().client("bedrock-runtime", config=Config(read_timeout=300, retries={"max_attempts": 6}))

    def generate_response(messages, *a, **k):
        system = [{"text": x["content"]} for x in messages if x["role"] == "system"]
        turns = []
        for x in messages:
            if x["role"] == "system":
                continue
            if turns and turns[-1]["role"] == x["role"]:
                turns[-1]["content"].append({"text": x["content"]})
            else:
                turns.append({"role": x["role"], "content": [{"text": x["content"]}]})
        t = time.perf_counter()
        r = br.converse(modelId=model_id, system=system, messages=turns,
                        inferenceConfig={"maxTokens": 4000, "temperature": 0})
        text = "".join(blk["text"] for blk in r["output"]["message"]["content"] if "text" in blk)
        calls.append({"uncached_in": r["usage"]["inputTokens"], "output_tokens": r["usage"]["outputTokens"],
                      "seconds": time.perf_counter() - t, "stop": r["stopReason"],
                      "response": text, "messages": messages})
        return text

    return generate_response


def _ollama_ctx_variant(model, run_dir):
    """An Ollama copy of `model` with the context window raised, so prompts are not cut."""
    name = f"{model.replace(':', '-').replace('/', '-')}-ctx{OLLAMA_NUM_CTX // 1024}k"
    modelfile = run_dir / f"Modelfile.{name}"
    modelfile.write_text(f"FROM {model}\nPARAMETER num_ctx {OLLAMA_NUM_CTX}\n")
    subprocess.run(["ollama", "create", name, "-f", str(modelfile)], check=True, capture_output=True)
    return name


class Mem0Extractor:
    """Mem0 2.x add() with a fresh local store per run; facts are what add() reports as added."""

    def __init__(self, backend, model, run_dir: Path):
        os.environ.setdefault("MEM0_TELEMETRY", "False")
        from mem0 import Memory

        self.name = f"mem0:{backend}:{model}"
        self.calls = []
        ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        embed_model = os.environ.get("FACTS_EVAL_EMBED_MODEL", "mxbai-q8")
        llm_model = _ollama_ctx_variant(model, run_dir) if backend == "ollama" else embed_model
        self.memory = Memory.from_config({
            # For anthropic/bedrock the Ollama LLM is only a placeholder: generate_response is replaced below.
            "llm": {"provider": "ollama", "config": {"model": llm_model, "temperature": 0, "max_tokens": 2000,
                                                     "ollama_base_url": ollama_url}},
            "embedder": {"provider": "ollama", "config": {"model": embed_model, "ollama_base_url": ollama_url}},
            "vector_store": {"provider": "qdrant", "config": {"collection_name": "facts_eval",
                                                              "embedding_model_dims": 1024,
                                                              "path": str(run_dir / "qdrant"), "on_disk": True}},
            "history_db_path": str(run_dir / "history.db"),
        })
        if backend == "anthropic":
            self.memory.llm.generate_response = _anthropic_llm(model, self.calls)
        elif backend == "bedrock":
            self.memory.llm.generate_response = _bedrock_llm(model, self.calls)
        else:
            self._wrap_ollama()

    def _wrap_ollama(self):
        client, calls = self.memory.llm.client, self.calls
        orig = client.chat

        def chat(*a, **k):
            t = time.perf_counter()
            r = orig(*a, **k)
            calls.append({"uncached_in": r.get("prompt_eval_count"), "output_tokens": r.get("eval_count"),
                          "seconds": time.perf_counter() - t, "stop": r.get("done_reason"),
                          "response": r["message"]["content"], "messages": k.get("messages")})
            return r

        client.chat = chat

    def add(self, session, index, exchange) -> AddResult:
        self.calls.clear()
        run_id = re.sub(r"[^A-Za-z0-9_-]+", "-", session.session_id)  # Mem0 rejects whitespace in run_id
        try:
            res = self.memory.add(exchange, user_id="martin", run_id=run_id)
            err = None
        except Exception as e:  # a bad reply from a small model is a result too
            res, err = {"results": []}, repr(e)
        facts = [Fact(r["memory"]) for r in res.get("results", []) if r.get("event") == "ADD" and r.get("memory")]
        return AddResult(facts, list(self.calls), err)


def parses_as_json(response):
    """Would the facts extractor accept this reply as JSON? Same parser as facts/extract.py."""
    from facts.extract import load_json_object

    if response is None:
        return None
    if not response.strip():
        return True
    try:
        load_json_object(response)
        return True
    except ValueError:
        return False
