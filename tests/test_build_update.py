"""
Tests for build_update.py - the lean UPDATE-installer builder.

These cover the pure, host-independent logic (no Cython / PyInstaller / Inno
Setup, all of which are Windows-build-machine only and are imported lazily
inside build_update.main()):

  * compute_source_fingerprint - stable, and CHANGES only when an
    application source file changes; NOISE dirs/extensions and the
    version-churning installer_setup.iss are excluded so an unchanged app
    never looks changed.
  * read_last_fingerprint / write_state - round-trip the .update_state.json.
  * sha256_file - matches hashlib over the same bytes.
  * build_manifest / write_manifest - the feed JSON the app fetches.
"""

import hashlib
import json

import build_update as bu


def _touch(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_fingerprint_is_stable_across_calls(tmp_path):
    _touch(tmp_path / "src" / "core" / "ai_provider.py", b"print('hi')")
    _touch(tmp_path / "assets" / "app_icon.ico", b"\x00icon")
    fp1 = bu.compute_source_fingerprint(tmp_path)
    fp2 = bu.compute_source_fingerprint(tmp_path)
    assert fp1 == fp2
    assert len(fp1) == 64  # sha256 hexdigest


def test_fingerprint_changes_when_source_changes(tmp_path):
    src = tmp_path / "src" / "core" / "ai_provider.py"
    _touch(src, b"version A")
    before = bu.compute_source_fingerprint(tmp_path)
    _touch(src, b"version B")
    after = bu.compute_source_fingerprint(tmp_path)
    assert before != after


def test_fingerprint_ignores_iss_version_churn(tmp_path):
    """installer_setup.iss is deliberately excluded - its MyAppVersion is
    bumped every build, which would otherwise always look like a change."""
    _touch(tmp_path / "src" / "app.py", b"real code")
    iss = tmp_path / "installer_setup.iss"
    iss.write_text('#define MyAppVersion "2.1.0"', encoding="utf-8")
    before = bu.compute_source_fingerprint(tmp_path)
    iss.write_text('#define MyAppVersion "2.1.1"', encoding="utf-8")
    after = bu.compute_source_fingerprint(tmp_path)
    assert before == after


def test_fingerprint_ignores_noise_dirs_and_generated_ext(tmp_path):
    _touch(tmp_path / "src" / "app.py", b"real code")
    baseline = bu.compute_source_fingerprint(tmp_path)
    # A pyc, a __pycache__ entry, and a big re-fetchable asset dir must not
    # count as an application change.
    _touch(tmp_path / "src" / "app.pyc", b"compiled")
    _touch(tmp_path / "src" / "__pycache__" / "app.cpython-311.pyc", b"cache")
    _touch(tmp_path / "assets" / "sound_effects" / "boom.wav", b"RIFF....")
    assert bu.compute_source_fingerprint(tmp_path) == baseline


def test_state_roundtrip(tmp_path):
    assert bu.read_last_fingerprint(tmp_path) is None
    bu.write_state(tmp_path, "abc123", "2.1.5")
    assert bu.read_last_fingerprint(tmp_path) == "abc123"
    data = json.loads((tmp_path / bu.UPDATE_STATE_FILE).read_text())
    assert data["version"] == "2.1.5"


def test_read_last_fingerprint_survives_corrupt_state(tmp_path):
    (tmp_path / bu.UPDATE_STATE_FILE).write_text("{ not json", encoding="utf-8")
    assert bu.read_last_fingerprint(tmp_path) is None


def test_sha256_file_matches_hashlib(tmp_path):
    p = tmp_path / "installer.exe"
    p.write_bytes(b"pretend installer bytes" * 1000)
    assert bu.sha256_file(p) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_build_manifest_shape():
    m = bu.build_manifest("2.1.9", "LRJK_..._Update_v2.1.9.exe", "deadbeef")
    assert m == {
        "version": "2.1.9",
        "file": "LRJK_..._Update_v2.1.9.exe",
        "url": "",
        "sha256": "deadbeef",
        "notes": "",
    }


def test_write_manifest_creates_dir_and_valid_json(tmp_path):
    updates = tmp_path / "updates"
    out = bu.write_manifest(updates, bu.build_manifest("3.0.0", "u.exe", "ff"))
    assert out.exists()
    assert out.name == bu.MANIFEST_NAME
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["version"] == "3.0.0"
    assert loaded["file"] == "u.exe"


# --- GitHub Release publishing (--push) --------------------------------------
# publish_github_release shells out to `gh`; we mock the CLI so the logic
# (create, clobber-on-existing-tag, gh-missing) is tested without a network.

from unittest.mock import MagicMock, patch  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_publish_returns_false_when_gh_unavailable(tmp_path):
    inst = tmp_path / "u.exe"
    inst.write_bytes(b"x")
    man = tmp_path / "latest_update.json"
    man.write_text("{}")
    with patch.object(bu, "_gh_available", return_value=False):
        assert bu.publish_github_release(inst, man, "2.1.9", "o/r") is False


def test_publish_creates_release_with_both_assets(tmp_path):
    inst = tmp_path / "LRJK_Update_v2.1.9.exe"
    inst.write_bytes(b"x")
    man = tmp_path / "latest_update.json"
    man.write_text("{}")
    with (
        patch.object(bu, "_gh_available", return_value=True),
        patch.object(bu.subprocess, "run", return_value=_completed(0)) as run,
    ):
        assert bu.publish_github_release(inst, man, "2.1.9", "o/r") is True
    cmd = run.call_args[0][0]
    assert cmd[:3] == ["gh", "release", "create"]
    assert "v2.1.9" in cmd
    assert "--repo" in cmd and "o/r" in cmd
    assert str(inst) in cmd and str(man) in cmd  # both assets uploaded


def test_publish_clobbers_when_tag_exists(tmp_path):
    inst = tmp_path / "u.exe"
    inst.write_bytes(b"x")
    man = tmp_path / "latest_update.json"
    man.write_text("{}")
    calls = []

    def _run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[2] == "create":
            return _completed(1, stderr="release already exists")
        return _completed(0)  # the upload --clobber retry

    with (
        patch.object(bu, "_gh_available", return_value=True),
        patch.object(bu.subprocess, "run", side_effect=_run),
    ):
        assert bu.publish_github_release(inst, man, "2.1.9", "o/r") is True
    assert calls[0][2] == "create"
    assert calls[1][2] == "upload" and "--clobber" in calls[1]


def test_publish_only_ships_existing_feed_without_rebuild(tmp_path):
    # A local feed already exists (as after a build whose publish step failed).
    updates = tmp_path / bu.UPDATES_DIR
    updates.mkdir()
    inst = updates / "LRJK_Blender_AI_Studio_Update_v2.1.27.exe"
    inst.write_bytes(b"installer")
    bu.write_manifest(
        updates, bu.build_manifest("2.1.27", "LRJK_Blender_AI_Studio_Update_v2.1.27.exe", "abc")
    )

    with patch.object(bu, "publish_github_release", return_value=True) as pub:
        assert bu.publish_existing_feed(tmp_path, "o/r") is True
    args = pub.call_args[0]
    assert args[0] == inst  # installer path
    assert args[2] == "2.1.27"  # version from the manifest
    assert args[3] == "o/r"


def test_publish_only_errors_when_no_feed(tmp_path):
    with patch.object(bu, "publish_github_release") as pub:
        assert bu.publish_existing_feed(tmp_path, "o/r") is False
    pub.assert_not_called()
