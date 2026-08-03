"""Archive images sent to Claude, with a sidecar recording every id (HOME-298).

Images live in the transcript as base64 and nowhere else that lasts: Claude Code
drops the original into a macOS temp directory which is cleared soon after, so a
conversation recalled months later refers to a screenshot nobody can look at.

WHY THIS STAGES LOCALLY INSTEAD OF WRITING TO THE SHARE
------------------------------------------------------
The destination is `/Volumes/share`, an SMB mount from Leia. record.py's whole
design is that capture does no network I/O and cannot fail — it was rewritten
after writing straight to Postgres lost messages whenever the database was down.
Writing images to an SMB mount from the hook would put that back, and worse: a
write to a hung mount blocks uninterruptibly, and the hook has a 120s timeout,
so a wedged share could stall Claude itself.

So this mirrors the split the codebase already uses for exactly this problem:

  * the hook stages to a local directory — no network, nothing that can be down;
  * `flush-images` moves staged files to the share, and when that fails the
    staging directory simply grows and drains later.

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
import shutil
from datetime import datetime, timezone

# Sits beside the outbox, and derived from it for the same reason: overriding
# the outbox location must not leave images writing somewhere unrelated.
_OUTBOX = os.environ.get(
    "CLAUDE_CHATS_OUTBOX", os.path.expanduser("~/.claude-chats/outbox.db")
)
STAGING_DIR = os.environ.get(
    "CLAUDE_CHATS_IMAGE_STAGING", os.path.join(os.path.dirname(_OUTBOX), "images")
)
# Where flush-images delivers to. Overridable so tests never touch a real mount.
SHARE_DIR = os.environ.get(
    "CLAUDE_CHATS_IMAGE_SHARE", "/Volumes/share/claude-chats/images"
)
# Hashes already delivered. Without this, flushing (which empties staging) would
# let the next hook run re-stage every image in the transcript, for ever.
FLUSHED_INDEX = os.path.join(STAGING_DIR, ".flushed")

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


def _load_flushed() -> set[str]:
    try:
        with open(FLUSHED_INDEX) as fh:
            return {line.strip() for line in fh if line.strip()}
    except FileNotFoundError:
        return set()


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
        # Truncated on purpose: useful context, but this lands on a Samba share
        # and whole conversations do not belong there.
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
        seen = _load_flushed() | _staged_hashes()
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


# ---------------------------------------------------------------------------
# flush-images — the half that is allowed to fail
# ---------------------------------------------------------------------------

def _is_live_mount(path: str) -> bool:
    """True only if `path`'s nearest existing ancestor is a real mount point.

    This guard is the whole reason flushing is a separate step. If the share is
    absent, a blind makedirs would create a LOCAL directory at the mountpoint,
    which then stops the real mount attaching and hides every file written into
    it — a failure that looks like success until someone goes looking.
    """
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    while probe and probe != os.path.sep:
        if os.path.ismount(probe):
            return True
        probe = os.path.dirname(probe)
    return False


def flush(staging_dir: str = None, share_dir: str = None) -> tuple[int, str]:
    """Move staged images to the share. Returns (moved, message)."""
    staging_dir = staging_dir or STAGING_DIR
    share_dir = share_dir or SHARE_DIR

    if not os.path.isdir(staging_dir):
        return 0, "nothing staged"
    if not _is_live_mount(share_dir):
        return 0, f"share not mounted ({share_dir}) — leaving files staged"

    moved = 0
    flushed_index = os.path.join(staging_dir, ".flushed")
    for session_id in sorted(os.listdir(staging_dir)):
        session_stage = os.path.join(staging_dir, session_id)
        if not os.path.isdir(session_stage):
            continue
        session_share = os.path.join(share_dir, session_id)
        os.makedirs(session_share, exist_ok=True)
        for fname in sorted(os.listdir(session_stage)):
            src = os.path.join(session_stage, fname)
            if not os.path.isfile(src):
                continue
            shutil.move(src, os.path.join(session_share, fname))
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(session_share, fname)) as fh:
                        digest = json.load(fh).get("sha256")
                except (OSError, ValueError):
                    digest = None
                if digest:
                    # Record delivery only after the bytes have landed, so an
                    # interrupted flush re-sends rather than losing the image.
                    with open(flushed_index, "a") as fh:
                        fh.write(digest + "\n")
                moved += 1
        if not os.listdir(session_stage):
            os.rmdir(session_stage)
    return moved, f"flushed {moved} image(s) to {share_dir}"


def main() -> None:
    moved, message = flush()
    print(message)
    raise SystemExit(0 if moved >= 0 else 1)
