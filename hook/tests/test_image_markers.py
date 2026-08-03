"""Image markers for conversation-memory (HOME-315).

Image messages reach the outbox already — Claude Code always writes an
"[Image #N]" text block beside the image, so they were never dropped. But that
placeholder was the entirety of what got stored: no filename, no way to tell
what the picture was, and no route to it once HOME-298 archives it.

The invariant these tests exist to protect is the sha256: it is hashed over the
DECODED bytes, because the archive writes decoded bytes and the two must agree.
Hashing the base64 text instead would still produce a stable-looking digest that
silently never matched an archived file.
"""

import base64
import hashlib
import os
import sys

# Make the `hook` package importable whether run directly or under pytest: add
# the directory that contains the `hook/` package (two levels up from this file).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook.record import _extract_text, _image_markers, _image_sources  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"
PNG_B64 = base64.b64encode(PNG).decode()
PNG_SHA8 = hashlib.sha256(PNG).hexdigest()[:8]


def _img(data=PNG_B64, media_type="image/png"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _meta(parent, path):
    return {
        "isMeta": True,
        "parentUuid": parent,
        "message": {"content": [{"type": "text", "text": f"[Image: source: {path}]"}]},
    }


class TestImageSources:
    def test_maps_parent_uuid_to_paths(self):
        entries = [_meta("abc", "/tmp/Screenshot One.png")]
        assert _image_sources(entries) == {"abc": ["/tmp/Screenshot One.png"]}

    def test_preserves_order_for_multiple_images(self):
        entries = [_meta("abc", "/tmp/a.png"), _meta("abc", "/tmp/b.png")]
        assert _image_sources(entries) == {"abc": ["/tmp/a.png", "/tmp/b.png"]}

    def test_ignores_non_meta_entries(self):
        """An assistant quoting "[Image: source: ...]" must not be mistaken for
        a real source record — which happens, because Claude discusses these."""
        entry = {
            "parentUuid": "abc",
            "message": {"content": [{"type": "text", "text": "[Image: source: /tmp/x.png]"}]},
        }
        assert _image_sources([entry]) == {}

    def test_ignores_meta_without_parent(self):
        assert _image_sources([{"isMeta": True, "message": {"content": []}}]) == {}

    def test_ignores_unrelated_meta_text(self):
        entries = [{"isMeta": True, "parentUuid": "abc",
                    "message": {"content": [{"type": "text", "text": "something else"}]}}]
        assert _image_sources(entries) == {}


class TestImageMarkers:
    def test_includes_basename_media_type_and_hash(self):
        markers = _image_markers([_img()], ["/var/folders/xx/Screenshot 2026-07-22 at 18.29.57.png"])
        assert markers == [
            f"[image: Screenshot 2026-07-22 at 18.29.57.png (image/png, sha256:{PNG_SHA8})]"
        ]

    def test_hash_is_over_decoded_bytes_not_base64(self):
        """The join key with the image archive. If this ever hashes the base64
        text, markers and archived files stop agreeing and nothing notices."""
        marker = _image_markers([_img()], [])[0]
        assert PNG_SHA8 in marker
        assert hashlib.sha256(PNG_B64.encode()).hexdigest()[:8] not in marker

    def test_clipboard_paste_has_no_filename(self):
        assert _image_markers([_img()], []) == [
            f"[image: pasted (image/png, sha256:{PNG_SHA8})]"
        ]

    def test_multiple_images_pair_with_paths_in_order(self):
        markers = _image_markers([_img(), _img()], ["/tmp/first.png", "/tmp/second.png"])
        assert "first.png" in markers[0]
        assert "second.png" in markers[1]

    def test_more_images_than_paths_falls_back_per_image(self):
        """59 meta entries against 63 image blocks in one real transcript — a
        message can mix file-sourced and clipboard-pasted images."""
        markers = _image_markers([_img(), _img()], ["/tmp/only-one.png"])
        assert "only-one.png" in markers[0]
        assert "pasted" in markers[1]

    def test_corrupt_base64_still_produces_a_marker(self):
        """A bad payload must not cost us the whole message."""
        markers = _image_markers([_img(data="not!valid!base64")], ["/tmp/x.png"])
        assert markers == ["[image: x.png (image/png)]"]

    def test_ignores_non_image_blocks(self):
        assert _image_markers([{"type": "text", "text": "hello"}], []) == []

    def test_plain_string_content(self):
        assert _image_markers("just text", []) == []


class TestCombinedWithText:
    def test_placeholder_is_kept_alongside_the_marker(self):
        """"[Image #1]" carries the numbering the conversation refers to, so it
        is appended to, never replaced."""
        content = [{"type": "text", "text": "[Image #1]"}, _img()]
        text = _extract_text(content)
        markers = _image_markers(content, ["/tmp/shot.png"])
        combined = "\n".join(p for p in (text, *markers) if p)
        assert combined == f"[Image #1]\n[image: shot.png (image/png, sha256:{PNG_SHA8})]"
