"""Archive images sent to Claude, with a sidecar recording every id (HOME-298).

Images live in the transcript as base64 and nowhere else that lasts: Claude Code
drops the original into a macOS temp directory which is cleared soon after, so a
conversation recalled months later refers to a screenshot nobody can look at.

WHERE THE IMAGES GO
-------------------
Straight to a local staging directory beside the outbox, and that is the end of
it. The distributed edition stages here and a separate `flush-images` step moves
the files onto an SMB share; single-machine has nowhere else to put them, so the
staging directory *is* the archive.

Identity is the sha256 of the DECODED bytes, which is also the join key used by
the conversation-memory markers (HOME-315) and recorded in the sidecar. Dedupe
is by hash, never by filename: the hook's wall-clock and the transcript's
timestamp differ, so the same image can legitimately produce two names.
"""

import base64
import binascii
import hashlib
import json
import os
import re
from datetime import datetime, timezone

# Sits beside the outbox, and derived from it for the same reason: overriding
# the outbox location must not leave images writing somewhere unrelated.
_OUTBOX = os.environ.get(
    "CLAUDE_CHATS_OUTBOX", os.path.expanduser("~/.claude-chats/outbox.db")
)
STAGING_DIR = os.environ.get(
    "CLAUDE_CHATS_IMAGE_STAGING", os.path.join(os.path.dirname(_OUTBOX), "images")
)

SCHEMA_VERSION = 1

_EXT_BY_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _slug(name: str) -> str:
    """Lower-case, hyphen-separated, no spaces — safe in a shell and a URL."""
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return stem or "image"


def _staged_hashes() -> set[str]:
    """Hashes sitting in staging, read from the sidecars rather than filenames.

    The filename carries only the first 8 hex chars; the sidecar has the full
    digest, and it is the sidecar that is authoritative.
    """
    hashes: set[str] = set()
    for root, _dirs, files in os.walk(STAGING_DIR):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, fname)) as fh:
                    digest = json.load(fh).get("sha256")
            except (OSError, ValueError):
                continue
            if digest:
                hashes.add(digest)
    return hashes


def iter_images(messages: list[dict], image_sources: dict[str, list[str]]):
    """Yield (entry, block_index, image_index, source_path_or_None, block)."""
    for entry in messages:
        content = entry.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        paths = image_sources.get(entry.get("uuid") or "", [])
        image_index = 0
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image":
                continue
            source_path = paths[image_index] if image_index < len(paths) else None
            yield entry, block_index, image_index, source_path, block
            image_index += 1


def _sidecar(entry, block_index, image_index, source_path, digest, data,
             media_type, session_id, project_path, git_branch, text) -> dict:
    """Every id we can lay hands on.

    Deliberately omits unknown fields rather than writing nulls, so a later
    reader can tell "not available" from "was empty". Bump SCHEMA_VERSION if the
    shape changes — this archive will outlive the current thinking about it.
    """
    doc = {
        "schema_version": SCHEMA_VERSION,
        "sha256": digest,
        "bytes": len(data),
        "media_type": media_type,
        "captured_by": "hook",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "block_index": block_index,
        "image_index_in_message": image_index,
    }
    for key, value in (
        ("message_uuid", entry.get("uuid")),
        ("parent_uuid", entry.get("parentUuid")),
        ("prompt_id", entry.get("promptId")),
        ("image_paste_ids", entry.get("imagePasteIds")),
        ("message_timestamp", entry.get("timestamp")),
        ("cwd", entry.get("cwd")),
        ("git_branch", entry.get("gitBranch") or git_branch),
        ("project_path", project_path),
        ("claude_version", entry.get("version")),
        ("user_type", entry.get("userType")),
        ("permission_mode", entry.get("permissionMode")),
        ("entrypoint", entry.get("entrypoint")),
        ("origin", entry.get("origin")),
        ("prompt_source", entry.get("promptSource")),
        ("role", entry.get("message", {}).get("role")),
        ("source_path", source_path),
        ("original_filename", os.path.basename(source_path) if source_path else None),
    ):
        if value not in (None, "", []):
            doc[key] = value
    if text:
        # Truncated on purpose: enough context to recognise the image by,
        # without duplicating whole conversations into the archive.
        doc["prompt_excerpt"] = text[:200]
    return doc


def stage_images(messages, image_sources, session_id, project_path, git_branch,
                 text_by_uuid=None) -> int:
    """Write any not-yet-seen images (plus sidecars) to the staging directory.

    Returns the number staged. Never raises: a failure here must not cost the
    message capture that is happening in the same hook run.
    """
    text_by_uuid = text_by_uuid or {}
    staged = 0
    try:
        seen = _staged_hashes()
        for entry, block_index, image_index, source_path, block in iter_images(
            messages, image_sources
        ):
            source = block.get("source", {})
            if source.get("type") != "base64" or not source.get("data"):
                continue
            try:
                data = base64.b64decode(source["data"], validate=True)
            except (binascii.Error, ValueError):
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                continue

            media_type = source.get("media_type") or "application/octet-stream"
            ext = _EXT_BY_MEDIA_TYPE.get(media_type, "bin")
            stamp_raw = entry.get("timestamp")
            try:
                stamp = datetime.fromisoformat(
                    (stamp_raw or "").replace("Z", "+00:00")
                )
            except ValueError:
                stamp = datetime.now(timezone.utc)
            name = os.path.basename(source_path) if source_path else "pasted"
            base = f"{stamp.strftime('%Y%m%d-%H%M%S')}-{digest[:8]}-{_slug(name)}"

            session_dir = os.path.join(STAGING_DIR, session_id)
            os.makedirs(session_dir, exist_ok=True)
            with open(os.path.join(session_dir, f"{base}.{ext}"), "wb") as fh:
                fh.write(data)
            doc = _sidecar(
                entry, block_index, image_index, source_path, digest, data,
                media_type, session_id, project_path, git_branch,
                text_by_uuid.get(entry.get("uuid") or ""),
            )
            with open(os.path.join(session_dir, f"{base}.json"), "w") as fh:
                json.dump(doc, fh, indent=2, sort_keys=True)
            seen.add(digest)
            staged += 1
    except Exception:
        # Capture is the priority. A broken archive is recoverable from the
        # transcript later; a hook that raises is not.
        pass
    return staged
