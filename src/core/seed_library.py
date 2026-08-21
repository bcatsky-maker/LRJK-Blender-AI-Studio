"""
Portable "seed" library — ship the ingested asset catalog *inside the
installer* so a fresh install on someone else's machine already has the
whole library, with no ingestion step.

The problem this solves: asset file paths in studio_memory.db are absolute
(e.g. G:\\...\\asset_store\\wrench\\wrench.gltf). Bundling that database
as-is would be useless on a recipient's machine, where the install path is
different. So instead:

  build_seed()  (BUILD TIME, run by build_all.py on your machine)
      Reads the live studio_memory.db + asset_store and writes a small
      metadata-only `seed_library.db` whose paths are RELATIVE to
      asset_store (portable). No BLOBs, no absolute paths - just a few MB.

  import_seed() (RUNTIME, first launch on the recipient's machine)
      Reads the bundled seed_library.db and registers each asset in the
      recipient's own writable runtime database, turning each relative path
      back into an absolute path that points at the asset_store the
      installer laid down next to the executable. Idempotent - safe to call
      on every launch; it no-ops once the import is done.

The actual asset *files* (asset_store/) are bundled by the installer
(see installer_setup.iss) and read in place from the install directory -
they're never copied into the user profile, so there's no multi-GB
duplication.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

SEED_DB_NAME = "seed_library.db"


def _rel_to_asset_store(abs_path):
    """Return the portion of a stored path after '.../asset_store/', using
    forward slashes, or None if the path isn't under an asset_store."""
    if not abs_path:
        return None
    p = str(abs_path).replace("\\", "/")
    marker = "asset_store/"
    i = p.lower().find(marker)
    if i < 0:
        return None
    return p[i + len(marker) :]


def build_seed(source_db, source_asset_store, out_seed_db) -> int:
    """
    BUILD TIME. Write a metadata-only seed database (relative paths) from a
    live studio_memory.db + its asset_store. Returns the number of assets
    written. Assets whose file can't be found under asset_store are skipped
    so the seed never promises a file the installer won't carry.
    """
    source_asset_store = Path(source_asset_store)
    src = sqlite3.connect(str(source_db))
    try:
        rows = src.execute(
            "SELECT asset_name, asset_type, source_category, file_path FROM binary_assets"
        ).fetchall()
    finally:
        src.close()

    out = Path(out_seed_db)
    dst = sqlite3.connect(str(out))
    try:
        # Overwrite in place (drop+recreate) rather than deleting the file
        # first - avoids depending on being able to unlink it (e.g. if it's
        # open/locked, or on a filesystem that disallows unlink).
        dst.execute("DROP TABLE IF EXISTS binary_assets")
        dst.execute(
            "CREATE TABLE binary_assets "
            "(asset_name TEXT, asset_type TEXT, source_category TEXT, rel_path TEXT)"
        )
        count = 0
        for name, atype, cat, fpath in rows:
            rel = _rel_to_asset_store(fpath)
            if not rel:
                continue
            if not (source_asset_store / rel.replace("/", os.sep)).exists():
                continue
            dst.execute(
                "INSERT INTO binary_assets VALUES (?, ?, ?, ?)",
                (name, atype, cat, rel),
            )
            count += 1
        dst.commit()
    finally:
        dst.close()
    return count


def import_seed(user_db_path, seed_db_path, bundled_asset_root) -> int:
    """
    RUNTIME (first launch). Register the bundled seed's assets in the
    recipient's writable runtime database, resolving each relative path
    against the bundled asset_store. Returns the number of newly imported
    assets. Idempotent: once the bundled rows are present it returns 0.
    """
    seed_db_path = Path(seed_db_path)
    bundled_asset_root = Path(bundled_asset_root)
    if not seed_db_path.exists():
        return 0

    seed = sqlite3.connect(str(seed_db_path))
    try:
        try:
            seed_count = seed.execute("SELECT COUNT(*) FROM binary_assets").fetchone()[0]
        except sqlite3.Error:
            return 0
        if seed_count == 0:
            return 0

        user = sqlite3.connect(str(user_db_path), timeout=60.0)
        try:
            user.execute("PRAGMA busy_timeout = 60000;")
            user.execute(
                "CREATE TABLE IF NOT EXISTS binary_assets ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, asset_name TEXT UNIQUE, "
                "asset_type TEXT, source_category TEXT, file_path TEXT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            # Backward-compat: older DBs may predate the file_path column.
            cols = {r[1] for r in user.execute("PRAGMA table_info(binary_assets)").fetchall()}
            if "file_path" not in cols:
                user.execute("ALTER TABLE binary_assets ADD COLUMN file_path TEXT")

            already = user.execute(
                "SELECT COUNT(*) FROM binary_assets WHERE source_category LIKE 'bundled:%'"
            ).fetchone()[0]
            if already >= seed_count:
                return 0  # already imported

            imported = 0
            for name, atype, cat, rel in seed.execute(
                "SELECT asset_name, asset_type, source_category, rel_path FROM binary_assets"
            ):
                abs_path = bundled_asset_root / rel.replace("/", os.sep)
                if not abs_path.exists():
                    continue
                cur = user.execute(
                    "INSERT OR IGNORE INTO binary_assets "
                    "(asset_name, asset_type, source_category, file_path) VALUES (?, ?, ?, ?)",
                    (name, atype, f"bundled:{cat}", str(abs_path)),
                )
                imported += cur.rowcount
            user.commit()
            return imported
        finally:
            user.close()
    finally:
        seed.close()


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a portable seed_library.db for the installer."
    )
    parser.add_argument("--db", required=True, help="path to the live studio_memory.db")
    parser.add_argument("--store", required=True, help="path to the asset_store directory")
    parser.add_argument("--out", required=True, help="output seed_library.db path")
    args = parser.parse_args(argv)

    if not Path(args.db).exists():
        print(f"[seed] source DB not found: {args.db}", file=sys.stderr)
        return 1
    if not Path(args.store).is_dir():
        print(f"[seed] asset_store not found: {args.store}", file=sys.stderr)
        return 1
    n = build_seed(args.db, args.store, args.out)
    print(f"[seed] wrote {n} asset entries to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
