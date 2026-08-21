"""
Tests for the desktop app's self-hosted silent auto-updater
(src/ui/main_window.py):

  * get_app_version / _version_tuple - version parsing + ordering used to
    decide whether a feed entry is actually newer.
  * SilentUpdateWorker._decide - the pure decision (does this manifest mean
    "update to X at URL Y?") exercised without any real network or Qt event
    loop, by driving run() with a mocked urlopen.

The heavy AIGenerator import that main_window pulls in is stubbed by
tests/conftest.py when the real module isn't on the path, so these run
anywhere - not just on the Windows build machine.
"""

import json
from unittest.mock import MagicMock, patch

from src.ui.main_window import (
    SilentUpdateWorker,
    _version_tuple,
    get_app_version,
)


def test_version_tuple_orders_correctly():
    assert _version_tuple("2.1.10") > _version_tuple("2.1.9")
    assert _version_tuple("v2.2.0") > _version_tuple("2.1.99")
    assert _version_tuple("2.1.0") == _version_tuple("2.1.0")


def test_version_tuple_is_crash_proof():
    assert _version_tuple("") == (0, 0, 0)
    assert _version_tuple(None) == (0, 0, 0)
    assert _version_tuple("weird-tag") == (0, 0, 0)
    assert _version_tuple("2.1") == (2, 1, 0)  # padded to 3 parts


def test_get_app_version_returns_dotted_or_zero():
    v = get_app_version()
    parts = v.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def _run_with_manifest(worker, manifest_bytes):
    """Drive SilentUpdateWorker.run() with a mocked HTTP response and capture
    whatever it emits on update_ready (or None if it stays silent)."""
    emitted = {}
    worker.update_ready = MagicMock()
    worker.update_ready.emit = lambda *a: emitted.update(version=a[0], url=a[1], sha256=a[2])

    fake_resp = MagicMock()
    fake_resp.read.return_value = manifest_bytes
    fake_resp.__enter__ = lambda s: fake_resp
    fake_resp.__exit__ = lambda s, *a: False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        worker.run()
    return emitted


def test_emits_when_feed_is_newer():
    worker = SilentUpdateWorker("https://host/updates/latest_update.json", current_version="2.1.0")
    manifest = json.dumps(
        {"version": "2.1.5", "file": "LRJK_Update_v2.1.5.exe", "sha256": "abc"}
    ).encode()
    out = _run_with_manifest(worker, manifest)
    assert out["version"] == "2.1.5"
    # relative "file" resolves against the manifest URL
    assert out["url"] == "https://host/updates/LRJK_Update_v2.1.5.exe"
    assert out["sha256"] == "abc"


def test_absolute_url_is_used_verbatim():
    worker = SilentUpdateWorker("https://host/updates/latest_update.json", current_version="2.1.0")
    manifest = json.dumps(
        {
            "version": "2.2.0",
            "file": "ignored.exe",
            "url": "https://cdn.example.com/pkg/LRJK.exe",
            "sha256": "ff",
        }
    ).encode()
    out = _run_with_manifest(worker, manifest)
    assert out["url"] == "https://cdn.example.com/pkg/LRJK.exe"


def test_no_emit_when_not_newer():
    worker = SilentUpdateWorker("https://host/updates/latest_update.json", current_version="2.1.5")
    manifest = json.dumps({"version": "2.1.5", "file": "x.exe"}).encode()
    assert _run_with_manifest(worker, manifest) == {}


def test_no_emit_on_empty_feed_url():
    worker = SilentUpdateWorker("", current_version="2.1.0")
    # run() should short-circuit before any network call; urlopen must never fire.
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
        worker.update_ready = MagicMock()
        worker.run()
    worker.update_ready.emit.assert_not_called()
