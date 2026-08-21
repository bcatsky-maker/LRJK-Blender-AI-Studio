"""
One-time migration: move legacy `binary_assets.file_data` BLOBs out of
studio_memory.db and onto disk as regular files, recording only the
resulting path in the database.

WHY THIS IS A SEPARATE, MANUALLY-RUN SCRIPT
---------------------------------------------
Your current studio_memory.db is roughly 18.5 GB, almost entirely because
raw asset bytes (models/textures/audio) were being stored as SQLite BLOBs.
StudioAssetManager (src/core/asset_manager.py) no longer *writes* new
BLOBs - new assets go straight to disk with only metadata in the DB - but
your existing rows still have their bytes sitting inside the database
file, and nothing converts those automatically. Doing that conversion
requires reading and rewriting ~18 GB, which is slow, disk-intensive, and
not something that should happen silently in the background of an
automated fix. Run this yourself, when you have time and free disk space.

USAGE
-----
    python src/core/migrate_blobs_to_files.py --dry-run
        Reports how many legacy BLOB rows exist and their total size,
        without changing anything.

    python src/core/migrate_blobs_to_files.py
        Performs the migration: for every row that still has file_data
        but no file_path, writes the bytes to the asset store and updates
        the row to reference that file (file_data is cleared on that row
        so it stops taking up space logically, but SQLite does not shrink
        the file on disk until you VACUUM - see below). Commits every
        --batch-size rows (default 25) so an interruption only loses the
        current partial batch and can simply be re-run - completed rows
        are skipped on the next run.

    python src/core/migrate_blobs_to_files.py --db-path "D:\\path\\to\\studio_memory.db"
        Target a specific database file instead of the default location.

AFTER MIGRATING: RECLAIMING DISK SPACE
---------------------------------------
Clearing file_data does not shrink studio_memory.db by itself - SQLite
just marks that space as free for reuse inside the file. To actually
reclaim the disk space, run (separately, once you've confirmed the
migration worked and you have roughly 2x the *current* db size free on
disk, since VACUUM builds a full rewritten copy before replacing the
original):

    sqlite3 studio_memory.db "VACUUM;"

This can take a long time on an 18+ GB database. Make sure nothing else
(the desktop app, Blender) has the file open while you run it.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.paths import get_asset_store_dir, get_studio_db_path  # noqa: E402


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn


def _ensure_file_path_column(conn: sqlite3.Connection) -> None:
    cursor = conn.execute("PRAGMA table_info(binary_assets)")
    columns = {row[1] for row in cursor.fetchall()}
    if "file_path" not in columns:
        conn.execute("ALTER TABLE binary_assets ADD COLUMN file_path TEXT")
        conn.commit()


def migrate(db_path: Path, asset_store_dir: Path, batch_size: int, dry_run: bool) -> None:
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    conn = _connect(db_path)
    try:
        cursor = conn.execute("PRAGMA table_info(binary_assets)")
        columns = {row[1] for row in cursor.fetchall()}
        if "file_data" not in columns:
            print("This database has no legacy 'file_data' BLOB column - nothing to migrate.")
            return

        _ensure_file_path_column(conn)

        count_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(file_data)), 0) FROM binary_assets "
            "WHERE file_data IS NOT NULL AND (file_path IS NULL OR file_path = '')"
        ).fetchone()
        pending_count, pending_bytes = count_row
        print(f"Pending legacy BLOB rows: {pending_count} ({pending_bytes / (1024**3):.2f} GB)")

        if pending_count == 0:
            print("Nothing to migrate.")
            return

        if dry_run:
            print("Dry run - no changes made. Re-run without --dry-run to migrate.")
            return

        asset_store_dir.mkdir(parents=True, exist_ok=True)
        migrated = 0
        cursor = conn.execute(
            "SELECT id, asset_name, asset_type, source_category, file_data FROM binary_assets "
            "WHERE file_data IS NOT NULL AND (file_path IS NULL OR file_path = '')"
        )

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            for row_id, asset_name, asset_type, source_category, file_data in rows:
                try:
                    category = source_category or "misc"
                    ext = asset_type or "bin"
                    dest_dir = asset_store_dir / category
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest_path = dest_dir / f"{asset_name}.{ext}"
                    dest_path.write_bytes(file_data)

                    conn.execute(
                        "UPDATE binary_assets SET file_path = ?, file_data = NULL WHERE id = ?",
                        (str(dest_path), row_id),
                    )
                    migrated += 1
                except Exception as e:
                    print(f"  [WARN] Failed migrating row id={row_id} ({asset_name}): {e}")

            conn.commit()
            print(f"  ...migrated {migrated}/{pending_count} rows", end="\r", flush=True)

        print(f"\nDone. Migrated {migrated}/{pending_count} rows to {asset_store_dir}")
        print(
            'Run `sqlite3 studio_memory.db "VACUUM;"` when ready to reclaim disk space (see module docstring).'
        )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="Path to studio_memory.db (default: auto-detected)",
    )
    parser.add_argument(
        "--asset-store-dir",
        type=str,
        default=None,
        help="Destination directory for extracted files",
    )
    parser.add_argument(
        "--batch-size", type=int, default=25, help="Rows committed per batch (default: 25)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would happen without changing anything"
    )
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_studio_db_path()
    asset_store_dir = Path(args.asset_store_dir) if args.asset_store_dir else get_asset_store_dir()

    print(f"Database: {db_path}")
    print(f"Asset store destination: {asset_store_dir}\n")

    migrate(db_path, asset_store_dir, batch_size=args.batch_size, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
