"""Image archiving to the share (HOME-298).

Two properties matter more than the rest, and both fail silently if broken:

1. **Identity is the sha256 of the decoded bytes**, matching the marker written
   into conversation-memory (HOME-315). Dedupe keys off it, never off filenames
   — the hook's wall-clock and the transcript timestamp differ, so one image can
   legitimately produce two names.

2. **Never write into an unmounted `/Volumes/share`.** A blind makedirs there
   creates a *local* directory at the mountpoint, which then prevents the real
   mount attaching and hides everything written into it. That looks like success
   until someone goes looking for their files.
"""

import base64
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import images as im  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"payload one"
PNG2 = b"\x89PNG\r\n\x1a\n" + b"payload two"


def _b64(data):
    return base64.b64encode(data).decode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _msg(uuid, data=PNG, media_type="image/png", text="[Image #1]",
         ts="2026-07-22T17:30:02.083Z"):
    return {
        "uuid": uuid,
        "timestamp": ts,
        "promptId": "prompt-1",
        "parentUuid": "parent-1",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": _b64(data)}},
            ],
        },
    }


@pytest.fixture
def staging(tmp_path, monkeypatch):
    d = tmp_path / "staging"
    monkeypatch.setattr(im, "STAGING_DIR", str(d))
    monkeypatch.setattr(im, "FLUSHED_INDEX", str(d / ".flushed"))
    return d


class TestSlug:
    def test_spaces_and_punctuation_become_hyphens(self):
        assert im._slug("Screenshot 2026-07-22 at 18.29.57.png") == "screenshot-2026-07-22-at-18-29-57"

    def test_empty_name_still_yields_something(self):
        assert im._slug("...") == "image"


class TestStaging:
    def test_writes_image_and_sidecar(self, staging):
        n = im.stage_images([_msg("u1")], {"u1": ["/tmp/Shot One.png"]}, "sess", "/proj", "br")
        assert n == 1
        files = sorted(p.name for p in (staging / "sess").iterdir())
        assert files == [
            "20260722-173002-%s-shot-one.json" % _sha(PNG)[:8],
            "20260722-173002-%s-shot-one.png" % _sha(PNG)[:8],
        ]

    def test_sidecar_records_the_full_hash_and_ids(self, staging):
        im.stage_images([_msg("u1")], {"u1": ["/tmp/Shot.png"]}, "sess", "/proj", "br")
        doc = json.loads(next((staging / "sess").glob("*.json")).read_text())
        assert doc["sha256"] == _sha(PNG)
        assert doc["message_uuid"] == "u1"
        assert doc["prompt_id"] == "prompt-1"
        assert doc["original_filename"] == "Shot.png"
        assert doc["bytes"] == len(PNG)
        assert doc["schema_version"] == im.SCHEMA_VERSION

    def test_sidecar_omits_unknown_fields_rather_than_writing_null(self, staging):
        """A reader must be able to tell "not available" from "was empty"."""
        msg = _msg("u1")
        del msg["promptId"]
        im.stage_images([msg], {}, "sess", "/proj", "br")
        doc = json.loads(next((staging / "sess").glob("*.json")).read_text())
        assert "prompt_id" not in doc
        assert "source_path" not in doc
        assert None not in doc.values()

    def test_clipboard_paste_without_source_path(self, staging):
        im.stage_images([_msg("u1")], {}, "sess", "/proj", "br")
        assert list((staging / "sess").glob("*-pasted.png"))

    def test_same_image_twice_is_stored_once(self, staging):
        """Dedupe is by content hash, not filename — the same screenshot sent in
        two different messages has two timestamps and one identity."""
        a = _msg("u1", ts="2026-07-22T17:30:02.083Z")
        b = _msg("u2", ts="2026-07-23T09:00:00.000Z")
        assert im.stage_images([a, b], {}, "sess", "/proj", "br") == 1
        assert len(list((staging / "sess").glob("*.png"))) == 1

    def test_different_images_both_stored(self, staging):
        msgs = [_msg("u1", data=PNG), _msg("u2", data=PNG2)]
        assert im.stage_images(msgs, {}, "sess", "/proj", "br") == 2

    def test_rerun_stages_nothing_new(self, staging):
        im.stage_images([_msg("u1")], {}, "sess", "/proj", "br")
        assert im.stage_images([_msg("u1")], {}, "sess", "/proj", "br") == 0

    def test_corrupt_base64_is_skipped_not_fatal(self, staging):
        bad = _msg("u1")
        bad["message"]["content"][1]["source"]["data"] = "not!base64"
        good = _msg("u2", data=PNG2)
        assert im.stage_images([bad, good], {}, "sess", "/proj", "br") == 1

    def test_extension_follows_media_type_not_original_name(self, staging):
        """Claude Code transcodes pasted PNGs to JPEG; the extension must
        describe the bytes on disk, not the name they arrived under."""
        im.stage_images([_msg("u1", media_type="image/jpeg")], {"u1": ["/tmp/Shot.png"]},
                        "sess", "/proj", "br")
        assert list((staging / "sess").glob("*.jpg"))
        assert not list((staging / "sess").glob("*.png"))

    def test_never_raises(self, staging, monkeypatch):
        """Capture must survive a broken archive."""
        monkeypatch.setattr(im, "_load_flushed", lambda: (_ for _ in ()).throw(RuntimeError))
        assert im.stage_images([_msg("u1")], {}, "sess", "/proj", "br") == 0


class TestLiveMountGuard:
    def test_plain_local_directory_at_the_mountpoint_is_refused(self, tmp_path):
        """The dangerous case: the share is absent and something has created a
        real directory where it should mount."""
        fake = tmp_path / "Volumes" / "share"
        fake.mkdir(parents=True)
        assert im._is_live_mount(str(fake / "claude-chats" / "images")) is False

    def test_nonexistent_path_is_refused(self, tmp_path):
        assert im._is_live_mount(str(tmp_path / "nope" / "deeper")) is False


class TestFlush:
    def test_refuses_when_share_not_mounted(self, staging, tmp_path, monkeypatch):
        im.stage_images([_msg("u1")], {}, "sess", "/proj", "br")
        monkeypatch.setattr(im, "_is_live_mount", lambda p: False)
        moved, msg = im.flush(str(staging), str(tmp_path / "share"))
        assert moved == 0
        assert "not mounted" in msg
        # Nothing lost — still staged, ready for the next attempt.
        assert len(list((staging / "sess").glob("*.png"))) == 1

    def test_moves_files_and_records_delivery(self, staging, tmp_path, monkeypatch):
        im.stage_images([_msg("u1")], {}, "sess", "/proj", "br")
        share = tmp_path / "share"
        monkeypatch.setattr(im, "_is_live_mount", lambda p: True)
        moved, _ = im.flush(str(staging), str(share))
        assert moved == 1
        assert len(list((share / "sess").glob("*.png"))) == 1
        assert len(list((share / "sess").glob("*.json"))) == 1
        assert _sha(PNG) in (staging / ".flushed").read_text()

    def test_flushed_images_are_not_staged_again(self, staging, tmp_path, monkeypatch):
        """The reason .flushed exists: flushing empties staging, so without it
        the next hook run would re-stage every image in the transcript."""
        im.stage_images([_msg("u1")], {}, "sess", "/proj", "br")
        monkeypatch.setattr(im, "_is_live_mount", lambda p: True)
        im.flush(str(staging), str(tmp_path / "share"))
        assert im.stage_images([_msg("u1")], {}, "sess", "/proj", "br") == 0

    def test_nothing_staged_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(im, "_is_live_mount", lambda p: True)
        moved, msg = im.flush(str(tmp_path / "absent"), str(tmp_path / "share"))
        assert moved == 0
        assert "nothing staged" in msg
