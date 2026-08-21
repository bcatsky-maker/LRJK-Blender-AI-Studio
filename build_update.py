"""
build_update.py - build a lean UPDATE installer for LRJK Blender AI Studio.

Unlike build_all.py (which builds the full first-time installer and bundles
the multi-GB asset library), this script:

  1. Checks whether the APPLICATION actually changed since the last update
     build (a content fingerprint of src/ + assets/). If nothing changed, it
     does nothing - so you can run it any time and it only rebuilds when there
     is something to ship. (--force overrides.)

  2. Builds a small, single-file UPDATE installer (app + add-on only, NO asset
     library) that installs over an existing install (same AppId) and can be
     run silently by the app's own auto-updater.

  3. Writes an update FEED into ./updates/ : the installer .exe plus a tiny
     `latest_update.json` manifest {version, file, url, sha256}. Host the
     contents of ./updates/ anywhere reachable by HTTPS (your web host, a
     GitHub raw URL, a OneDrive/Dropbox direct link, or a UNC/file:// path),
     and point the app's "Update feed URL" (AI Settings) at the manifest. The
     app checks it on startup and silently installs newer updates.

  4. (--push) Publishes the installer + manifest as a GitHub RELEASE on a
     SEPARATE PUBLIC "releases" repo, so the app can fetch updates over a
     tokenless public URL while the SOURCE repo stays PRIVATE. The app's
     "Update feed URL" is then:
         https://github.com/<owner>/<releases-repo>/releases/latest/download/latest_update.json
     GitHub's ".../releases/latest/download/<name>" always resolves to the
     newest release's asset, and the app resolves the installer file relative
     to that manifest URL - so both assets just work with no app change.

Usage:
    python build_update.py                    # build only if the app changed
    python build_update.py --force            # build even if nothing changed
    python build_update.py --push             # build, then publish a GitHub Release
    python build_update.py --push --force      # force a build and publish
    python build_update.py --publish-only      # publish the EXISTING updates/ feed, no rebuild
    python build_update.py --push --releases-repo=owner/name   # override target repo

--push requires the GitHub CLI (`gh`) to be installed and authenticated
(`gh auth status`). It creates the release on DEFAULT_RELEASES_REPO (a PUBLIC
repo), uploading over an existing tag if one is already there.

The heavy build steps (Cython, PyInstaller, Inno Setup) are reused from
build_all.py; that module is imported lazily inside main() because it pulls in
Windows-only modules (winreg).
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

UPDATE_STATE_FILE = ".update_state.json"  # remembers the last-built fingerprint
UPDATES_DIR = "updates"  # the feed folder (installer + manifest)
MANIFEST_NAME = "latest_update.json"

# The PUBLIC repo that holds ONLY the update feed (installer + manifest) as
# release assets, kept separate from the private source repo so the app can
# fetch updates without a token. Override per-run with --releases-repo=owner/name.
DEFAULT_RELEASES_REPO = "bcatsky-maker/LRJK-Studio-Releases"

# Directories under the scanned roots whose contents are large, generated, or
# re-fetchable and therefore must NOT count toward "did the app change".
_SKIP_DIRS = {
    "__pycache__",
    "runtime_cache",
    "absorbed_addons",
    "blender_extensions",
    "freesound",
    "makehuman",
    "polyhaven",
    "sketchfab",
    "sound_effects",
}
_SKIP_EXT = {".pyc", ".pyo", ".pyd", ".so", ".bak", ".c"}


def _iter_source_files(project_root: Path):
    """
    Yield the files whose contents define the *application* build, so we can
    tell whether an update actually needs to be produced. Deliberately EXCLUDES
    installer_setup.iss (its version number is bumped every build, which would
    otherwise always look like a change) and all large/generated asset staging.
    """
    root_files = ["app.manifest", "requirements.txt", "setup_cython.py"]
    for rel in root_files:
        p = project_root / rel
        if p.is_file():
            yield p
    for r in ("src", "assets"):
        base = project_root / r
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_dir():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _SKIP_EXT:
                continue
            yield path


def compute_source_fingerprint(project_root) -> str:
    """Stable sha256 over the app's source (each file's relative path + bytes)."""
    project_root = Path(project_root)
    h = hashlib.sha256()
    for path in _iter_source_files(project_root):
        rel = path.relative_to(project_root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(path.read_bytes())
        except OSError:
            pass
        h.update(b"\0")
    return h.hexdigest()


def read_last_fingerprint(project_root) -> str | None:
    p = Path(project_root) / UPDATE_STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("fingerprint")
        except (ValueError, OSError):
            return None
    return None


def write_state(project_root, fingerprint: str, version: str):
    p = Path(project_root) / UPDATE_STATE_FILE
    p.write_text(
        json.dumps({"fingerprint": fingerprint, "version": version}, indent=2), encoding="utf-8"
    )


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest(version: str, filename: str, sha256: str, notes: str = "") -> dict:
    """The update feed manifest the app fetches. `url` empty => the app
    resolves the installer relative to the manifest URL."""
    return {"version": version, "file": filename, "url": "", "sha256": sha256, "notes": notes}


def write_manifest(updates_dir, manifest: dict) -> Path:
    updates_dir = Path(updates_dir)
    updates_dir.mkdir(parents=True, exist_ok=True)
    out = updates_dir / MANIFEST_NAME
    out.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


def _run_update_inno(project_root: Path, ba):
    """Compile the UPDATE installer via Inno Setup with /DUpdateMode=1."""
    iss_script = project_root / "installer_setup.iss"
    release_dir = project_root / "Release_Installers"
    release_dir.mkdir(parents=True, exist_ok=True)

    iscc_paths = [
        shutil.which("ISCC"),
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    iscc_bin = (
        next((p for p in iscc_paths if p and Path(p).exists()), None) or ba.find_iscc_in_registry()
    )
    if not iscc_bin:
        print("\n❌ CRITICAL ERROR: Inno Setup (ISCC.exe) was NOT found!")
        sys.exit(1)

    cmd = [str(iscc_bin), f"/O{release_dir}", "/DUpdateMode=1", str(iss_script)]
    ba.run_stage(cmd, "Inno Setup UPDATE Installer Compilation")


def _gh_available() -> bool:
    """True if the GitHub CLI is installed and authenticated."""
    if not shutil.which("gh"):
        return False
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
        return r.returncode == 0
    except OSError:
        return False


def publish_github_release(installer_path, manifest_path, version, releases_repo) -> bool:
    """
    Publish the update installer + manifest as a GitHub Release on the PUBLIC
    releases repo via the `gh` CLI. Creates the release for tag v<version>; if
    that tag already exists, re-uploads the assets over it (--clobber) instead
    of failing. Returns True on success.

    Kept separate from the (private) source repo so the app can fetch updates
    from a tokenless public URL:
        https://github.com/<releases_repo>/releases/latest/download/latest_update.json
    """
    if not _gh_available():
        print(
            "\n⚠️ --push was requested but the GitHub CLI (`gh`) isn't installed "
            "or isn't authenticated.\n   Install it and run `gh auth login`, then "
            "re-run with --push. (The build + local feed were still produced.)"
        )
        return False

    tag = f"v{version}"
    title = f"LRJK Blender AI Studio {tag}"
    notes = (
        f"Automated update build {tag}.\n\n"
        f"This release is the auto-update feed consumed by the desktop app "
        f"(latest_update.json + the update installer). Installed apps pointed "
        f"at this repo's `releases/latest/download/latest_update.json` pick it "
        f"up on next launch."
    )
    assets = [str(installer_path), str(manifest_path)]

    create = [
        "gh",
        "release",
        "create",
        tag,
        *assets,
        "--repo",
        releases_repo,
        "--title",
        title,
        "--notes",
        notes,
        "--latest",
    ]
    print(f"\n📤 Publishing GitHub Release {tag} to {releases_repo} ...")
    r = subprocess.run(create, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"✅ Created release {tag} on {releases_repo}.")
        return True

    # Tag already exists -> upload the assets over it instead.
    combined = (r.stdout + r.stderr).lower()
    if "already exists" in combined or "already_exists" in combined or "tag_name" in combined:
        print(f"ℹ️ Release {tag} already exists - uploading assets over it (--clobber).")
        upload = ["gh", "release", "upload", tag, *assets, "--repo", releases_repo, "--clobber"]
        r2 = subprocess.run(upload, capture_output=True, text=True)
        if r2.returncode == 0:
            print(f"✅ Updated release {tag} assets on {releases_repo}.")
            return True
        print(f"❌ Failed to upload assets to existing release {tag}:\n{r2.stderr.strip()}")
        return False

    print(f"❌ Failed to create GitHub Release {tag}:\n{r.stderr.strip()}")
    return False


def publish_existing_feed(project_root, releases_repo) -> bool:
    """
    --publish-only: publish whatever is ALREADY in updates/ (installer +
    latest_update.json) as a GitHub Release, with NO rebuild. Use this after
    a build produced the local feed but publishing failed (e.g. gh wasn't
    installed yet) - so you don't rebuild and bump the version just to ship
    the package you already have.
    """
    updates_dir = Path(project_root) / UPDATES_DIR
    manifest_path = updates_dir / MANIFEST_NAME
    if not manifest_path.exists():
        print(
            f"❌ No feed to publish: {manifest_path} doesn't exist. "
            f"Run `python build_update.py` first."
        )
        return False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"❌ Could not read {manifest_path}: {e}")
        return False

    version = str(data.get("version", "")).strip()
    filename = str(data.get("file", "")).strip()
    installer = updates_dir / filename
    if not version or not filename or not installer.exists():
        print(
            f"❌ Feed manifest is incomplete or the installer is missing "
            f"(version={version!r}, file={filename!r})."
        )
        return False

    print(f"📦 Publishing the existing local feed (v{version}) without rebuilding.")
    ok = publish_github_release(installer, manifest_path, version, releases_repo)
    if ok:
        feed = f"https://github.com/{releases_repo}/releases/latest/download/{MANIFEST_NAME}"
        print(f"\n🚀 Published v{version}. App 'Update feed URL' (AI Settings):\n   {feed}")
    return ok


def main():
    project_root = Path(__file__).parent.resolve()
    force = "--force" in sys.argv
    push = "--push" in sys.argv
    publish_only = "--publish-only" in sys.argv
    releases_repo = DEFAULT_RELEASES_REPO
    for a in sys.argv:
        if a.startswith("--releases-repo="):
            releases_repo = a.split("=", 1)[1].strip() or releases_repo

    # Publish the already-built feed and stop - no fingerprinting, no rebuild.
    if publish_only:
        publish_existing_feed(project_root, releases_repo)
        return

    current_fp = compute_source_fingerprint(project_root)
    last_fp = read_last_fingerprint(project_root)

    if current_fp == last_fp and not force:
        print("✅ No application changes since the last update build - nothing to do.")
        print("   (Run with --force to build an update installer anyway.)")
        return

    if last_fp is None:
        print("ℹ️ No previous update fingerprint found - building the first update package.")
    else:
        print("🔎 Application changes detected - building an update installer.")

    # build_all pulls in winreg (Windows-only); import lazily so this module's
    # pure functions stay importable/testable off-Windows.
    import build_all as ba

    iss_file = project_root / "installer_setup.iss"
    new_version = ba.increment_iss_version(iss_file)

    ba.run_quality_checks(project_root, strict="--strict-checks" in sys.argv)
    ba.run_stage([sys.executable, "-m", "pytest", "tests/", "-v"], "Unit Tests")

    try:
        ba.clean_build_artifacts(project_root)
        manifest_file = ba.ensure_manifest_exists(project_root)
        ba.compile_cython(project_root)

        pyinstaller_cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            f"--manifest={manifest_file}",
            "--name=LRJK_Blender_AI_Studio",
            "--add-data=assets;assets",
            "--add-data=installer_setup.iss;.",
            "src/ui/main_window.py",
        ]
        icon_path = project_root / "assets" / "app_icon.ico"
        if icon_path.exists():
            pyinstaller_cmd.insert(6, f"--icon={icon_path}")

        ba.run_stage(pyinstaller_cmd, "PyInstaller Bundling (app only)")
        ba.patch_missing_python_dll(project_root)
        _run_update_inno(project_root, ba)
    finally:
        ba.restore_sources(project_root)

    # Locate the produced update installer.
    installer = (
        project_root / "Release_Installers" / f"LRJK_Blender_AI_Studio_Update_v{new_version}.exe"
    )
    if not installer.exists():
        matches = sorted(
            (project_root / "Release_Installers").glob("LRJK_Blender_AI_Studio_Update_v*.exe")
        )
        if not matches:
            print("❌ Could not find the produced update installer in Release_Installers/.")
            sys.exit(1)
        installer = matches[-1]

    # Publish into the update feed folder.
    updates_dir = project_root / UPDATES_DIR
    updates_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256_file(installer)
    dest = updates_dir / installer.name
    if dest.resolve() != installer.resolve():
        shutil.copy2(installer, dest)
    manifest_path = write_manifest(updates_dir, build_manifest(new_version, installer.name, digest))
    write_state(project_root, current_fp, new_version)

    print(f"\n✅ Update package ready: v{new_version}")
    print(f"   Installer: {dest}")
    print(f"   Manifest:  {manifest_path}")

    if push:
        published = publish_github_release(dest, manifest_path, new_version, releases_repo)
        if published:
            feed = (
                f"https://github.com/{releases_repo}/releases/latest/download/" f"{MANIFEST_NAME}"
            )
            print(
                f"\n🚀 Published. Set this as the app's 'Update feed URL' (AI Settings):\n   {feed}"
            )
            print(
                "Installed apps pointed at that feed silently pick this update up on next launch."
            )
        else:
            print("\n(Local feed in 'updates/' is ready; publishing did not complete - see above.)")
    else:
        print("\nNext: run again with --push to publish this as a GitHub Release on")
        print(f"   {releases_repo}, or host the 'updates/' folder yourself. Then set the")
        print("   manifest URL as the 'Update feed URL' in the desktop app's AI Settings.")


if __name__ == "__main__":
    main()
