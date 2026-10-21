"""Fleet's command protocol as Kata reads it (CHATS-10, ota_cmd.c), through the otacmd tool.

tinycrypt's embedded/tests/test_ota_cmd.py (2d490fd, CC0), less the approval test: the parser is
the same code. The wire format is Fleet's (nakomis-fleet docs/architecture/design.md and its mqtt
tests): chunks are 0-based, at most 10, and sha256 rides on the last one.

    cc -std=gnu11 -Wall -Wextra -Werror -o otacmd otacmd_main.c ota_cmd.c $(pkg-config --cflags --libs libcjson)
    python -m pytest test_ota_cmd.py
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

OTACMD = Path(os.environ.get("OTACMD", Path(__file__).parent / "otacmd"))
SHA = hashlib.sha256(b"firmware").hexdigest()
URL = "https://nakomis-firmware-sandbox.s3.eu-west-2.amazonaws.com/kata/1.2.3/firmware.bin?X-Amz-Signature=ab"


@pytest.fixture(autouse=True)
def built():
    if not OTACMD.exists():
        pytest.fail(f"{OTACMD} not built: see the top of this file")


def run(*commands):
    lines = "".join((c if isinstance(c, str) else json.dumps(c)) + "\n" for c in commands)
    out = subprocess.run([str(OTACMD)], input=lines, capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def tool(*args):
    return subprocess.run([str(OTACMD), *args], capture_output=True, text=True, check=True).stdout.split("\n")


def update(**over):
    cmd = {"command": "ota_update", "url": URL, "version": "1.2.3", "sha256": SHA}
    cmd.update(over)
    return {k: v for k, v in cmd.items() if v is not None}


def chunks(url, n, version="1.2.3", sha=SHA):
    size = -(-len(url) // n)
    parts = [url[i * size:(i + 1) * size] for i in range(n)]
    msgs = [{"command": "url_chunk", "chunk_index": i, "total_chunks": n, "chunk_data": p, "version": version}
            for i, p in enumerate(parts)]
    if sha:
        msgs[-1]["sha256"] = sha
    return msgs


def test_simple_commands():
    assert run({"command": "get_version"}, {"command": "ping"}, {"command": "ota_cancel"}) == [
        {"kind": "get_version"}, {"kind": "ping"}, {"kind": "cancel"}]


def test_update():
    assert run(update()) == [{"kind": "update", "url": URL, "version": "1.2.3", "sha256": SHA}]


def test_upper_case_sha256_is_read():
    assert run(update(sha256=SHA.upper()))[0]["sha256"] == SHA


@pytest.mark.parametrize("command", ["reboot", "wipe", "set_setting", "get_status", "OTA_UPDATE"])
def test_anything_else_is_refused(command):
    assert run({"command": command}) == [
        {"kind": "error", "ota_error": False, "message": f"Unknown command: {command}"}]


@pytest.mark.parametrize("text,message", [
    ("not json", "Invalid JSON command"),
    ("[1, 2]", "Invalid JSON command"),
    ('{"cmd": "ping"}', "Missing command"),
    ('{"command": 7}', "Missing command"),
])
def test_malformed(text, message):
    assert run(text) == [{"kind": "error", "ota_error": False, "message": message}]


@pytest.mark.parametrize("over,message", [
    ({"sha256": None}, "sha256 required"),
    ({"sha256": "00" * 31}, "Invalid sha256"),
    ({"sha256": "zz" * 32}, "Invalid sha256"),
    ({"url": "http://example.com/firmware.bin"}, "Invalid firmware URL"),
    ({"url": "https://"}, "Invalid firmware URL"),
    ({"url": "https://example.com/a b"}, "Invalid firmware URL"),
    ({"url": "https://example.com/é"}, "Invalid firmware URL"),
    ({"url": None}, "Invalid firmware URL"),
    ({"url": "https://x/" + "a" * 9000}, "Invalid firmware URL"),
    ({"version": "1.2"}, "Invalid version"),
    ({"version": "1.2.3-beta"}, "Invalid version"),
    ({"version": "1.2.3 <b>"}, "Invalid version"),
    ({"version": "12345.0.0"}, "Invalid version"),
    ({"version": None}, "Invalid version"),
])
def test_update_refused(over, message):
    assert run(update(**over)) == [{"kind": "error", "ota_error": True, "message": message}]


@pytest.mark.parametrize("n", [1, 2, 10])
def test_chunks_in_order(n):
    out = run(*chunks(URL, n))
    assert out[:-1] == [{"kind": "chunk", "index": i, "total": n} for i in range(n - 1)]
    assert out[-1] == {"kind": "update", "url": URL, "version": "1.2.3", "sha256": SHA}


def test_chunks_out_of_order_keep_sha256_from_the_last_chunk():
    msgs = chunks(URL, 3)
    out = run(msgs[2], msgs[0], msgs[1])
    assert out[-1] == {"kind": "update", "url": URL, "version": "1.2.3", "sha256": SHA}


def test_long_presigned_url_in_chunks():
    url = URL + "&X-Amz-Security-Token=" + "A" * 3000
    assert run(*chunks(url, 3))[-1]["url"] == url


def test_chunks_without_sha256_are_refused():
    assert run(*chunks(URL, 2, sha=None))[-1] == {"kind": "error", "ota_error": True, "message": "sha256 required"}


@pytest.mark.parametrize("over", [
    {"chunk_index": 2, "total_chunks": 2},
    {"chunk_index": -1},
    {"total_chunks": 11, "chunk_index": 0},
    {"total_chunks": 0},
    {"chunk_index": 0.5},
    {"chunk_index": "0"},
    {"chunk_data": None},
    {"version": None},
])
def test_bad_chunk_parameters(over):
    msg = {"command": "url_chunk", "chunk_index": 0, "total_chunks": 2, "chunk_data": "https://", "version": "1.2.3"}
    msg.update(over)
    msg = {k: v for k, v in msg.items() if v is not None}
    assert run(msg) == [{"kind": "error", "ota_error": True, "message": "Invalid chunk parameters"}]


def test_a_chunk_for_another_version_starts_again():
    old = chunks(URL, 2, version="1.2.2")
    new = chunks(URL, 2)
    out = run(old[0], new[1], old[1])
    # new[1] dropped old[0]; old[1] then dropped new[1]: nothing completes.
    assert [o["kind"] for o in out] == ["chunk", "chunk", "chunk"]


def test_cancel_and_update_drop_partial_chunks():
    msgs = chunks(URL, 2)
    assert [o["kind"] for o in run(msgs[0], {"command": "ota_cancel"}, msgs[1])] == ["chunk", "cancel", "chunk"]
    assert [o["kind"] for o in run(msgs[0], update(), msgs[1])] == ["chunk", "update", "chunk"]


def test_chunks_that_assemble_into_a_bad_url_are_refused():
    out = run(*chunks("http://example.com/firmware.bin", 2))
    assert out[-1] == {"kind": "error", "ota_error": True, "message": "Invalid firmware URL"}


@pytest.mark.parametrize("v,ok", [("0.0.0", 1), ("1.2.3", 1), ("9999.9999.9999", 1), ("1.2", 0), ("1.2.3.4", 0),
                                  ("1..3", 0), (".1.2", 0), ("1.2.", 0), ("a.b.c", 0), ("", 0), ("1.2.3 ", 0)])
def test_version_valid(v, ok):
    assert tool("--valid", v)[0] == str(ok)


@pytest.mark.parametrize("a,b,c", [("1.2.3", "1.2.3", 0), ("1.2.10", "1.2.9", 1), ("1.10.0", "1.9.99", 1),
                                   ("0.2.0", "1.0.0", -1), ("2.0.0", "10.0.0", -1)])
def test_version_cmp(a, b, c):
    assert tool("--cmp", a, b)[0] == str(c)


# CRYPT-21: a chunk set is capped as it arrives and expires.

def test_an_oversize_chunk_set_is_refused_as_it_arrives():
    big = {"command": "url_chunk", "total_chunks": 3, "version": "1.2.3", "chunk_data": "A" * 5000}
    out = run({**big, "chunk_index": 0}, {**big, "chunk_index": 1})
    assert out == [{"kind": "chunk", "index": 0, "total": 3},
                   {"kind": "error", "ota_error": True, "message": "Invalid firmware URL"}]


def test_resending_a_chunk_replaces_it_in_the_cap():
    # The same ~3.5 KB chunk three times counts once, not three times.
    msgs = chunks(URL + "&X-Amz-Security-Token=" + "a" * 7000, 2)
    out = run(msgs[0], msgs[0], msgs[0], msgs[1])
    assert out[-1]["kind"] == "update"


def test_a_url_of_exactly_the_limit_is_refused():
    url = "https://" + "a" * (8192 - 8)
    assert run(*chunks(url, 2))[-1] == {"kind": "error", "ota_error": True, "message": "Invalid firmware URL"}


@pytest.mark.parametrize("start", [0, 2**32 - 30000])  # and across the clock wrapping
def test_a_set_completes_within_eight_minutes(start):
    # CHATS-19: chunks a few phases apart, as a busy Kata reads them
    msgs = chunks(URL, 2)
    out = run({"_now": start}, msgs[0], {"_now": (start + 480000) % 2**32}, msgs[1])
    assert out[-1]["kind"] == "update"


@pytest.mark.parametrize("start", [0, 2**32 - 30000])
def test_a_stale_set_is_dropped_and_a_fresh_one_completes(start):
    msgs = chunks(URL, 2)
    later = (start + 480001) % 2**32
    out = run({"_now": start}, msgs[0], {"_now": later}, msgs[1], msgs[0])
    # msgs[1] found the set stale and started a new one; msgs[0] then completed it.
    assert [o["kind"] for o in out] == ["chunk", "chunk", "update"]
