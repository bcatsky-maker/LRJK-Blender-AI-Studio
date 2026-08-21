"""
shrink_database.py - reclaim empty space in studio_memory.db.

WHAT THIS DOES
--------------
After the BLOB->file migration, every asset's bytes live on disk under
asset_store/ and the database only holds small metadata. But SQLite does
NOT shrink its file when rows/BLOBs are deleted - it just marks those pages
"free" and reuses them later. That's why studio_memory.db can sit at many
GB while containing only a few MB of real data.

This script runs VACUUM, which rewrites the database into a fresh, compact
file, releasing all that free space back to the operating system. It does
NOT delete or modify any asset files, and it preserves 100% of the live
data (asset metadata, saved-script history, the reference index).

Typical result here: ~18.6 GB -> ~20 MB.

BEFORE YOU RUN
--------------
1. CLOSE the LRJK Blender AI Studio desktop app (and anything else using the
   database). VACUUM needs an exclusive lock; if the app is open you'll get
   "database is locked".
2. Make sure you have a little free disk space (VACUUM briefly writes a
   compact temporary copy - here that's only tens of MB, since the live data
   is tiny).

USAGE
-----
    python shrink_database.py                 # uses ./studio_memory.db
    python shrink_database.py --db "C:\\path\\to\\studio_memory.db"
    python shrink_database.py --force         # VACUUM even if BLOBs remain

If the script finds assets that still carry BLOB bytes (i.e. the migration
was only partly done), it stops and tells you - VACUUM alone would keep
those bytes. Run migrate_blobs_to_files.py first, then re-run this. Use
--force to compact anyway without migrating.
"""

import argparse
import sqlite3
from pathlib import Path


def _human(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.2f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.2f} TB"  # unreachable (loop always returns at TB), keeps mypy happy


def _blob_status(conn: sqlite3.Connection):
    """Return (rows_with_blob, total_blob_bytes) for binary_assets, or (0, 0)
    if the table has no legacy file_data column at all."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(binary_assets)").fetchall()}
    if "file_data" not in cols:
        return 0, 0
    row = conn.execute(
        "SELECT COUNT(file_data), COALESCE(SUM(LENGTH(file_data)), 0) FROM binary_assets"
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compact studio_memory.db (reclaim free space via VACUUM)."
    )
    parser.add_argument(
        "--db",
        default="studio_memory.db",
        help="path to studio_memory.db (default: ./studio_memory.db)",
    )
    parser.add_argument(
        "--force", action="store_true", help="VACUUM even if BLOB data is still present"
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        return 1

    size_before = db_path.stat().st_size
    print(f"Database: {db_path.resolve()}")
    print(f"Size before: {_human(size_before)}")

    try:
        # isolation_level=None -> autocommit, required so VACUUM isn't run
        # inside an implicit transaction.
        conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    except sqlite3.OperationalError as e:
        print(f"❌ Could not open the database: {e}")
        return 1

    try:
        # Report what's actually inside, so you can see the space is free.
        try:
            ps = conn.execute("PRAGMA page_size").fetchone()[0]
            pc = conn.execute("PRAGMA page_count").fetchone()[0]
            fl = conn.execute("PRAGMA freelist_count").fetchone()[0]
            print(
                f"Live data: {_human((pc - fl) * ps)}   Reclaimable free space: {_human(fl * ps)}"
            )
        except sqlite3.Error:
            pass

        blob_rows, blob_bytes = _blob_status(conn)
        if blob_rows and not args.force:
            print()
            print(
                f"⚠️  {blob_rows} asset(s) still hold BLOB data ({_human(blob_bytes)}) inside the database."
            )
            print("    VACUUM would keep those bytes, not remove them. Move them to disk first:")
            print(
                "        python src/core/migrate_blobs_to_files.py            # dry run: add --dry-run"
            )
            print(
                "    then re-run this script. Or pass --force to compact anyway without migrating."
            )
            return 2
        if blob_rows and args.force:
            print(
                f"⚠️  --force: {blob_rows} asset(s) with BLOB data ({_human(blob_bytes)}) will be KEPT in the DB."
            )

        # Fold any WAL contents back into the main file first, then VACUUM.
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        print(
            "\n🧹 Running VACUUM (this reads the whole file; on a large DB it can take a few minutes)..."
        )
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                print(
                    "❌ Database is locked. Close the LRJK Blender AI Studio desktop app and try again."
                )
            else:
                print(f"❌ VACUUM failed: {e}")
            return 1
    finally:
        conn.close()

    size_after = db_path.stat().st_size
    reclaimed = size_before - size_after
    print("\n✅ Done.")
    print(f"Size after:  {_human(size_after)}")
    print(
        f"Reclaimed:   {_human(reclaimed)}  ({(reclaimed / size_before * 100) if size_before else 0:.1f}% smaller)"
    )
    print(
        "\nYour asset files under asset_store/ were not touched - only empty space in the DB was released."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
