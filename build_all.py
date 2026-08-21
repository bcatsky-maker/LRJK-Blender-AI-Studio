import re
import shutil
import subprocess
import sys
import winreg
from pathlib import Path


def increment_iss_version(iss_path: Path) -> str:
    """Reads installer_setup.iss, increments patch version, and updates the file."""
    if not iss_path.exists():
        print(f"⚠️ Could not find {iss_path.name} to increment version.")
        return "2.1.0"

    content = iss_path.read_text(encoding="utf-8")
    pattern = r'#define\s+MyAppVersion\s+"(\d+)\.(\d+)\.(\d+)"'
    match = re.search(pattern, content)

    if match:
        major, minor, patch = map(int, match.groups())
        new_patch = patch + 1
        new_version = f"{major}.{minor}.{new_patch}"

        new_content = re.sub(pattern, f'#define MyAppVersion "{new_version}"', content)
        iss_path.write_text(new_content, encoding="utf-8")
        print(f"🔢 Automatically bumped version: {major}.{minor}.{patch} ➡️ {new_version}")
        return new_version
    else:
        print("⚠️ MyAppVersion pattern not found in ISS script. Keeping existing version.")
        return "2.1.0"


def run_stage(cmd, title, allow_failure=False):
    print(f"\n🚀 {title}...")
    result = subprocess.run(cmd)
    if result.returncode != 0 and not allow_failure:
        print(f"❌ Failed: {title}")
        sys.exit(result.returncode)


def run_quality_checks(project_root: Path, strict: bool = False):
    """
    Code-quality gate run before the unit tests: black (format), ruff --fix
    (lint + autofix), mypy (type-check). All three are configured in
    pyproject.toml.

    Each tool is skipped with a warning if it isn't installed, so a machine
    without the dev extras (`pip install -r dev-requirements.txt`) can still
    produce a build. By DEFAULT the gate is advisory: black and ruff apply
    their fixes in place and everything only *reports* what remains, so a
    style nit never blocks shipping an installer. Pass `--strict-checks` to
    make ruff/mypy findings fail the build (useful in CI).
    """
    print("\n🧭 Code-quality checks (black · ruff --fix · mypy)...")
    checks = [
        # (tool, argv, blocking-in-strict-mode)
        ("black", ["black", "."], False),
        ("ruff", ["ruff", "check", "--fix", "."], True),
        ("mypy", ["mypy", "."], True),
    ]
    for name, cmd, blocking in checks:
        exe = shutil.which(name)
        if not exe:
            print(f"  ⚠️ {name} not installed - skipping (pip install {name}).")
            continue
        result = subprocess.run([exe, *cmd[1:]], cwd=str(project_root))
        if result.returncode != 0:
            if strict and blocking:
                print(f"❌ {name} reported problems (--strict-checks).")
                sys.exit(result.returncode)
            print(f"  ⚠️ {name} reported problems (advisory - not blocking the build).")
        else:
            print(f"  ✅ {name} clean.")


def ensure_manifest_exists(project_root: Path) -> Path:
    manifest_path = project_root / "app.manifest"
    manifest_content = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="1.0.0.0" processorArchitecture="*" name="LRJK.BlenderAIStudio" type="win32" />
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*" />
    </dependentAssembly>
  </dependency>
</assembly>"""
    manifest_path.write_text(manifest_content.strip(), encoding="utf-8")
    print("  [+] Verified app.manifest")
    return manifest_path


def clean_build_artifacts(project_root: Path):
    print("\n🧹 Cleaning old build artifacts...")
    folders_to_remove = ["build", "dist", "installer_output", "Release_Installers"]
    for folder in folders_to_remove:
        dir_path = project_root / folder
        if dir_path.exists():
            shutil.rmtree(dir_path)
            print(f"  [-] Removed folder: {folder}")

    spec_file = project_root / "LRJK_Blender_AI_Studio.spec"
    if spec_file.exists():
        spec_file.unlink()


def compile_cython(project_root: Path):
    print("\n🔒 Compiling core modules with Cython...")
    run_stage([sys.executable, "setup_cython.py", "build_ext", "--inplace"], "Cython Compilation")
    target = project_root / "src" / "core" / "generator.py"
    if target.exists():
        shutil.copy2(target, target.with_suffix(".py.bak"))
        target.unlink()


def restore_sources(project_root: Path):
    print("\n🔄 Restoring source files from backup...")
    for bak in project_root.glob("src/**/*.py.bak"):
        original_py = bak.with_suffix("")
        shutil.move(bak, original_py)


def patch_missing_python_dll(project_root: Path):
    dist_internal = project_root / "dist" / "LRJK_Blender_AI_Studio" / "_internal"
    dist_internal.mkdir(parents=True, exist_ok=True)
    sys_python_dir = Path(sys.executable).parent
    dll_name = f"python{sys.version_info.major}{sys.version_info.minor}.dll"

    for dll in [dll_name, "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll"]:
        target_dll = dist_internal / dll
        if not target_dll.exists():
            for s_dir in [sys_python_dir, Path("C:/Windows/System32")]:
                source_dll = s_dir / dll
                if source_dll.exists():
                    shutil.copy2(source_dll, target_dll)
                    break


def find_iscc_in_registry():
    registry_keys = [
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        ),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        ),
        (
            winreg.HKEY_CURRENT_USER,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        ),
    ]
    for hkey, subkey in registry_keys:
        try:
            with winreg.OpenKey(hkey, subkey) as key:
                install_location, _ = winreg.QueryValueEx(key, "InstallLocation")
                if install_location:
                    iscc = Path(install_location) / "ISCC.exe"
                    if iscc.exists():
                        return iscc
        except OSError:
            continue
    return None


def prepare_seed_library(project_root: Path) -> bool:
    """
    Generate seed_library.db - a small, portable, metadata-only catalog of
    the locally-ingested asset library (relative paths, no BLOBs) - so the
    installer can bundle the whole library and a fresh install on someone
    else's machine has it immediately (see src/core/seed_library.py).

    Returns True if the library is present and ready to bundle. When there's
    no local library, the installer just ships without one.
    """
    db = project_root / "studio_memory.db"
    store = project_root / "asset_store"
    seed = project_root / "seed_library.db"

    if not db.exists() or not store.is_dir():
        print(
            "\n📦 No local asset library found (studio_memory.db / asset_store) - "
            "installer will ship without a bundled library."
        )
        return False

    print("\n📦 Generating portable seed-library manifest for the installer...")
    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "src" / "core" / "seed_library.py"),
            "--db",
            str(db),
            "--store",
            str(store),
            "--out",
            str(seed),
        ]
    )
    if result.returncode != 0 or not seed.exists():
        print("⚠️ Seed-library generation failed; installer will ship without a bundled library.")
        return False
    return True


def run_inno_setup(project_root: Path, bundle_library: bool = False):
    iss_script = project_root / "installer_setup.iss"
    release_dir = project_root / "Release_Installers"
    release_dir.mkdir(parents=True, exist_ok=True)

    iscc_paths = [
        shutil.which("ISCC"),
        Path("C:/Program Files (x86)/Inno Setup 6/ISCC.exe"),
        Path("C:/Program Files/Inno Setup 6/ISCC.exe"),
    ]
    iscc_bin = (
        next((p for p in iscc_paths if p and Path(p).exists()), None) or find_iscc_in_registry()
    )

    if iscc_bin:
        cmd = [str(iscc_bin), f"/O{release_dir}"]
        if bundle_library:
            # Tells installer_setup.iss to include asset_store + seed_library.db.
            cmd.append("/DBundleLibrary=1")
            print("  [+] Bundling the full ingested asset library into the installer.")
        cmd.append(str(iss_script))
        run_stage(cmd, "Inno Setup Installer Compilation")
    else:
        print("\n❌ CRITICAL ERROR: Inno Setup (ISCC.exe) was NOT found!")
        sys.exit(1)


def auto_sync_github(project_root: Path, new_version: str):
    """
    Commits and pushes build-related source changes (e.g. the version bump
    in installer_setup.iss) to GitHub.

    IMPORTANT: this is now opt-in only (pass --push on the command line).
    It used to run unconditionally and used `git add .`, which - combined
    with there being no .gitignore for a long time - staged and pushed the
    entire build/, dist/, and Release_Installers/ output directories plus
    the multi-gigabyte studio_memory.db on every single build. It now only
    stages changes to files git is *already tracking* (`git add -u`), so a
    stray large/untracked file can never be swept in silently, and it warns
    about any untracked files left in the tree so you can review them by
    hand before deciding whether they belong in git at all.
    """
    print("\n🐙 Syncing build-related source changes to GitHub repository...")
    git_bin = shutil.which("git")
    if not git_bin:
        print("⚠️ Git command not found. Skipping push.")
        return

    # Surface anything untracked instead of silently deciding for the user.
    status = subprocess.run(
        [git_bin, "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    untracked = [line[3:] for line in status.stdout.splitlines() if line.startswith("??")]
    if untracked:
        print(
            f"⚠️ {len(untracked)} untracked file(s) exist and will NOT be committed automatically:"
        )
        for path in untracked[:15]:
            print(f"    - {path}")
        if len(untracked) > 15:
            print(f"    ... and {len(untracked) - 15} more. Review with `git status`.")

    # Only re-stage files git already knows about (e.g. installer_setup.iss's
    # version bump). Never blindly stage the whole working tree.
    run_stage([git_bin, "add", "-u"], "Git Add Tracked File Changes", allow_failure=True)
    commit_msg = f"Automated build update: LRJK Blender AI Studio v{new_version}"
    run_stage([git_bin, "commit", "-m", commit_msg], "Git Commit", allow_failure=True)
    run_stage([git_bin, "push"], "Git Push to GitHub", allow_failure=True)


def main():
    project_root = Path(__file__).parent.resolve()
    iss_file = project_root / "installer_setup.iss"

    new_version = increment_iss_version(iss_file)

    if "--clean-only" in sys.argv:
        clean_build_artifacts(project_root)
        return

    run_quality_checks(project_root, strict="--strict-checks" in sys.argv)
    run_stage([sys.executable, "-m", "pytest", "tests/", "-v"], "Unit Tests")

    try:
        clean_build_artifacts(project_root)
        manifest_file = ensure_manifest_exists(project_root)
        compile_cython(project_root)

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
            # Bundled so the running app can read its own version back out
            # at runtime (get_app_version() in main_window.py) instead of
            # relying on a hardcoded string that drifts out of sync with
            # what increment_iss_version() just bumped this file to, a few
            # lines up in this same script.
            "--add-data=installer_setup.iss;.",
            "src/ui/main_window.py",
        ]
        # NOTE: the live studio_memory.db is intentionally NOT bundled into
        # the build - it's a bloated (freed-but-unVACUUMed) runtime DB with
        # machine-absolute asset paths, useless on a recipient's machine.
        # Instead, prepare_seed_library() (called below, before Inno) builds
        # a small PORTABLE seed_library.db with relative paths, and the
        # installer bundles that + the asset_store; the app registers it into
        # the recipient's own writable DB on first run (see
        # src/core/seed_library.py). The app still creates its per-user
        # database on first run (StudioAssetManager / ExecutionMemoryDB
        # default paths in src/core).

        icon_path = project_root / "assets" / "app_icon.ico"
        if icon_path.exists():
            pyinstaller_cmd.insert(6, f"--icon={icon_path}")

        run_stage(pyinstaller_cmd, "PyInstaller Bundling")
        patch_missing_python_dll(project_root)
        bundle_library = prepare_seed_library(project_root)
        run_inno_setup(project_root, bundle_library=bundle_library)

    finally:
        restore_sources(project_root)

    if "--push" in sys.argv:
        auto_sync_github(project_root, new_version)
    else:
        print(
            "\nℹ️ Skipping git sync (pass --push to commit & push tracked-file changes, e.g. the version bump)."
        )

    print(f"\n✅ Build pipeline completed successfully for v{new_version}!")


if __name__ == "__main__":
    main()
