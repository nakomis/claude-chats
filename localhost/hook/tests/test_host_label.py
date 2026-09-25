"""The fallback host label is stable across networks (HOME-395).

macOS's gethostname() says ``phi`` on some networks and ``phi.local`` on
others; before this, one Mac's conversations were split across both labels.

Runs standalone (``python hook/tests/test_host_label.py``) or under pytest.
Standard library only.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hook import record  # noqa: E402


def _host(name: str) -> str:
    with mock.patch.object(record.socket, "gethostname", return_value=name):
        return record._default_host()


def test_strips_the_local_suffix():
    assert _host("phi.local") == "phi"


def test_a_bare_hostname_is_unchanged():
    assert _host("phi") == "phi"


def test_only_a_trailing_local_is_stripped():
    assert _host("local-box") == "local-box"
    assert _host("phi.localdomain") == "phi.localdomain"


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok: {_name}")
