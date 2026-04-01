"""
Provider-agnostic embedding helper.

Controlled by environment variables:

  CLAUDE_CHATS_PROVIDER   ollama (default) | bedrock | openai
  CLAUDE_CHATS_MODEL      override the default model for the chosen provider
  CLAUDE_CHATS_DIMENSIONS output dimensions (default 1024; ignored by some models)

Provider-specific variables:

  ollama  — OLLAMA_BASE_URL  (default http://localhost:11434)
  bedrock — standard AWS env vars / profile (AWS_PROFILE, AWS_REGION, …)
  openai  — OPENAI_API_KEY
"""

from __future__ import annotations

import os

PROVIDER   = os.environ.get("CLAUDE_CHATS_PROVIDER",   "ollama").lower()
DIMENSIONS = int(os.environ.get("CLAUDE_CHATS_DIMENSIONS", "1024"))

_DEFAULT_MODELS: dict[str, str] = {
    "ollama":  "mxbai-embed-large",
    "bedrock": "amazon.titan-embed-text-v2:0",
    "openai":  "text-embedding-3-small",
}
MODEL = os.environ.get("CLAUDE_CHATS_MODEL", _DEFAULT_MODELS.get(PROVIDER, "mxbai-embed-large"))


def get_embedding(text: str, *, for_query: bool = False) -> list[float]:
    """Return an embedding vector for *text* using the configured provider.

    Args:
        text:      The text to embed (truncated to 8 192 chars if longer).
        for_query: When True, use query-optimised encoding where the provider
                   distinguishes between document and query inputs (Cohere on
                   Bedrock).  Ignored by Ollama, Titan, and OpenAI.
    """
    text = text[:8192]
    if PROVIDER == "ollama":
        return _ollama(text)
    if PROVIDER == "bedrock":
        return _bedrock(text, for_query=for_query)
    if PROVIDER == "openai":
        return _openai(text)
    raise ValueError(
        f"Unknown provider {PROVIDER!r}. "
        "Set CLAUDE_CHATS_PROVIDER to 'ollama', 'bedrock', or 'openai'."
    )


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _ollama(text: str) -> list[float]:
    import httpx
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    r = httpx.post(
        f"{base}/api/embeddings",
        json={"model": MODEL, "prompt": text},
        timeout=60.0,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def _bedrock(text: str, *, for_query: bool) -> list[float]:
    import json as _json
    import boto3  # type: ignore[import-untyped]

    client = boto3.client("bedrock-runtime")

    if "titan" in MODEL:
        body = _json.dumps({
            "inputText": text,
            "dimensions": DIMENSIONS,
            "normalize": True,
        })
        resp = client.invoke_model(
            modelId=MODEL,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return _json.loads(resp["body"].read())["embedding"]

    if "cohere" in MODEL:
        # Cohere distinguishes document vs query encoding — this matters for
        # retrieval quality.
        input_type = "search_query" if for_query else "search_document"
        body = _json.dumps({"texts": [text], "input_type": input_type})
        resp = client.invoke_model(
            modelId=MODEL,
            body=body,
            contentType="application/json",
            accept="application/json",
        )
        return _json.loads(resp["body"].read())["embeddings"][0]

    raise ValueError(
        f"Unsupported Bedrock model {MODEL!r}. "
        "Use a Titan model (amazon.titan-embed-text-v2:0) or "
        "Cohere model (cohere.embed-english-v3)."
    )


def _openai(text: str) -> list[float]:
    from openai import OpenAI  # type: ignore[import-untyped]
    client = OpenAI()  # reads OPENAI_API_KEY from environment
    resp = client.embeddings.create(
        model=MODEL,
        input=text,
        dimensions=DIMENSIONS,
    )
    return resp.data[0].embedding
