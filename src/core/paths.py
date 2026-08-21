"""
Shared helpers for locating LRJK Blender AI Studio's writable data
directory (where studio_memory.db and the on-disk asset store live).

Historically this defaulted to "next to the source tree" (project root)
everywhere, including in the frozen/installed build. That breaks for a
normal install: a non-admin user has no write access under
`C:\\Program Files\\...`, so the very first database write after
installation would fail. It also meant a single database could quietly
balloon to many GB sitting inside what looks like the application's own
install folder, which is what happened during development here.

Behavior now (first match wins):
  - LRJK_STUDIO_DATA_DIR env var, if set: use it verbatim. This is the
    override that lets an INSTALLED build point at an existing data
    directory - e.g. the project checkout where a large ingested library
    already lives - instead of the empty per-user folder. Set it to the
    folder that contains studio_memory.db (and asset_store/):
        setx LRJK_STUDIO_DATA_DIR "G:\\Software\\LRJK Blender AI Studio\\lrjk-blender-ai-studio"
    (Asset file paths in the DB are absolute, so the installed app then
    resolves them straight to the existing asset_store - nothing needs
    copying.)
  - Running from source (not frozen by PyInstaller): use the project root,
    convenient for local development and matching the existing repo layout
    / the current large dev database.
  - Running as a frozen/installed build with no override: use a per-user,
    always-writable app-data directory (a fresh install starts empty and
    ingests its own library there).
"""

import os
import sys
from pathlib import Path


def get_studio_data_dir() -> Path:
    """Directory studio_memory.db and the asset store should live under."""
    override = os.getenv("LRJK_STUDIO_DATA_DIR", "").strip()
    if override:
        data_dir = Path(override).expanduser()
    elif getattr(sys, "frozen", False):
        if sys.platform == "win32":
            base = os.getenv("LOCALAPPDATA")
            base_dir = Path(base) if base else Path.home() / "AppData" / "Local"
        else:
            base_dir = Path.home() / ".local" / "share"
        data_dir = base_dir / "LRJK_Blender_AI_Studio"
    else:
        # Dev mode: src/core/paths.py -> parents[2] is the project root.
        data_dir = Path(__file__).resolve().parent.parent.parent

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_studio_db_path() -> Path:
    return get_studio_data_dir() / "studio_memory.db"


def get_asset_store_dir() -> Path:
    """Directory ingested asset *files* live in (DB stores metadata + this path, not bytes)."""
    d = get_studio_data_dir() / "asset_store"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_bundled_seed_db():
    """
    Path to the installer-bundled seed_library.db (a portable metadata
    catalog), or None if there isn't one. Only present in a frozen/installed
    build where the installer laid it down next to the executable - running
    from source returns None (the dev database is used directly instead).
    """
    if not getattr(sys, "frozen", False):
        return None
    cand = Path(sys.executable).parent / "seed_library.db"
    return cand if cand.exists() else None


def get_bundled_asset_root():
    """
    Path to the installer-bundled asset_store (the actual asset files,
    read in place from the install directory), or None if not present /
    running from source.
    """
    if not getattr(sys, "frozen", False):
        return None
    cand = Path(sys.executable).parent / "asset_store"
    return cand if cand.is_dir() else None
