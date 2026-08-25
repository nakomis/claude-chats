"""Image archiving (HOME-298), single-machine edition.

The property that matters most, and fails silently when broken: **identity is
the sha256 of the decoded bytes**, matching the marker written into
conversation-memory (HOME-315). Dedupe keys off that hash, never off filenames —
the hook's wall-clock and the transcript timestamp differ, so one image can
legitimately produce two names.

The distributed edition's flush-to-SMB tests have no counterpart here: staging
is the archive, so there is no second step to get wrong.
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
        monkeypatch.setattr(im, "_staged_hashes", lambda: (_ for _ in ()).throw(RuntimeError))
        assert im.stage_images([_msg("u1")], {}, "sess", "/proj", "br") == 0
