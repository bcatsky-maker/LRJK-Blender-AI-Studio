"""
Manages ingested binary assets (models, textures, audio, ...).

Storage model (current): asset bytes live as regular files under an
on-disk asset store; only metadata + the file path are kept in SQLite.
This replaced an earlier design that stored raw file bytes as BLOBs
directly inside studio_memory.db, which let that single SQLite file
balloon into the tens of gigabytes - unbundlable in installers, unpushable
to git, and slow to back up or VACUUM.

Reading stays backward-compatible with legacy rows that still have their
bytes in the old `file_data` BLOB column (see _fetch_row / load_asset_to_disk
/ get_asset_bytes below), so an existing large database keeps working
as-is. Use src/core/migrate_blobs_to_files.py, run by hand, to convert an
existing large database over to the new file-on-disk layout and shrink it.
"""

import sqlite3
from pathlib import Path
from typing import Any

from src.core.paths import get_asset_store_dir, get_studio_db_path


class StudioAssetManager:
    def __init__(
        self,
        project_root: Path | None = None,
        db_path: Path | None = None,
        asset_store_dir: Path | None = None,
    ):
        if db_path is not None:
            self.db_path = Path(db_path)
        elif project_root is not None:
            self.db_path = Path(project_root) / "studio_memory.db"
        else:
            self.db_path = get_studio_db_path()

        self.project_root = self.db_path.parent
        self.asset_store_dir = Path(asset_store_dir) if asset_store_dir else get_asset_store_dir()
        self.asset_store_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")
        return conn

    def _init_db(self) -> None:
        """Ensures binary_assets exists with a file_path column (metadata-only for new rows)."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS binary_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_name TEXT UNIQUE,
                    asset_type TEXT,
                    source_category TEXT,
                    file_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
            # Backward compatibility: pre-existing databases have this table
            # with a `file_data BLOB` column (and no file_path). Add
            # file_path if missing so old and new rows coexist safely.
            cursor.execute("PRAGMA table_info(binary_assets)")
            columns = {row[1] for row in cursor.fetchall()}
            if "file_path" not in columns:
                cursor.execute("ALTER TABLE binary_assets ADD COLUMN file_path TEXT")
            conn.commit()
        finally:
            conn.close()

    def _has_blob_column(self, conn: sqlite3.Connection) -> bool:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(binary_assets)")
        return "file_data" in {row[1] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def store_asset_bytes(
        self, asset_name: str, asset_type: str, source_category: str, data: bytes
    ) -> Path:
        """Writes bytes to the on-disk asset store and records metadata only (never a new BLOB row)."""
        category_dir = self.asset_store_dir / source_category
        category_dir.mkdir(parents=True, exist_ok=True)
        dest_path = category_dir / f"{asset_name}.{asset_type}"
        dest_path.write_bytes(data)

        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO binary_assets (asset_name, asset_type, source_category, file_path)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asset_name) DO UPDATE SET
                    asset_type=excluded.asset_type,
                    source_category=excluded.source_category,
                    file_path=excluded.file_path
                """,
                (asset_name, asset_type, source_category, str(dest_path)),
            )
            conn.commit()
        finally:
            conn.close()
        return dest_path

    # ------------------------------------------------------------------
    # Reading (transparently supports legacy BLOB rows)
    # ------------------------------------------------------------------
    def _fetch_row(self, asset_name: str):
        try:
            conn = self._connect()
            try:
                if self._has_blob_column(conn):
                    cursor = conn.execute(
                        "SELECT file_path, file_data FROM binary_assets WHERE asset_name = ?",
                        (asset_name,),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT file_path, NULL FROM binary_assets WHERE asset_name = ?",
                        (asset_name,),
                    )
                return cursor.fetchone()
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error looking up asset '{asset_name}': {e}")
            return None

    def load_asset_to_disk(self, asset_name: str, output_destination: Path) -> bool:
        """Extracts an asset (from the file store, or a legacy BLOB row) to output_destination."""
        row = self._fetch_row(asset_name)
        if row is None:
            return False
        file_path, file_data = row

        try:
            output_destination.parent.mkdir(parents=True, exist_ok=True)
            if file_path:
                src = Path(file_path)
                if not src.exists():
                    print(f"[WARN] Indexed file for '{asset_name}' is missing on disk: {src}")
                    return False
                output_destination.write_bytes(src.read_bytes())
                return True
            if file_data:
                output_destination.write_bytes(file_data)
                return True
            return False
        except Exception as e:
            print(f"[WARN] Error loading asset '{asset_name}' from store: {e}")
            return False

    def get_asset_bytes(self, asset_name: str) -> bytes | None:
        """Retrieves raw asset bytes directly into memory without writing to disk."""
        row = self._fetch_row(asset_name)
        if row is None:
            return None
        file_path, file_data = row
        try:
            if file_path:
                p = Path(file_path)
                return p.read_bytes() if p.exists() else None
            return file_data
        except Exception as e:
            print(f"[WARN] Error reading asset bytes for '{asset_name}': {e}")
            return None

    # Mesh types that can actually be imported into a Blender scene by the
    # add-on's import_mesh_file handler. Textures/audio/etc. are indexed
    # but not importable as scene geometry, so library search ignores them.
    IMPORTABLE_MESH_TYPES = ("glb", "gltf", "obj", "fbx")

    def search_assets(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """
        Keyword search over ingested *importable* 3D assets (models only),
        ranked by how many query keywords appear in the asset name/category.

        This is the retrieval half of the ingestion pipeline - for a long
        time assets were downloaded and indexed but nothing ever searched
        them at generation time, so a multi-gigabyte library was effectively
        write-only. import_asset_from_library (in main_window.py) calls this
        to turn a prompt like "a wooden chair" into a real imported model.
        Returns dicts: {name, type, category, file_path}.
        """
        query = (query or "").strip().lower()
        if not query:
            return []
        keywords = [w for w in query.replace("/", " ").replace("-", " ").split() if len(w) > 2]
        if not keywords:
            return []

        placeholders = ",".join("?" for _ in self.IMPORTABLE_MESH_TYPES)
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT asset_name, asset_type, source_category, file_path "
                    f"FROM binary_assets WHERE LOWER(asset_type) IN ({placeholders})",
                    tuple(self.IMPORTABLE_MESH_TYPES),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error searching assets: {e}")
            return []

        scored = []
        for name, atype, category, file_path in rows:
            haystack = f"{name or ''} {category or ''}".lower()
            score = sum(1 for kw in keywords if kw in haystack)
            if score > 0:
                scored.append(
                    (
                        score,
                        {
                            "name": name,
                            "type": atype,
                            "category": category,
                            "file_path": file_path,
                        },
                    )
                )
        scored.sort(key=lambda t: -t[0])
        return [item for _score, item in scored[:limit]]

    def list_available_assets(self, asset_type: str | None = None) -> list[dict[str, Any]]:
        """Lists metadata for all assets stored in the database, optionally filtered by type."""
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                if asset_type:
                    cursor.execute(
                        "SELECT asset_name, asset_type, source_category FROM binary_assets WHERE asset_type = ?",
                        (asset_type,),
                    )
                else:
                    cursor.execute(
                        "SELECT asset_name, asset_type, source_category FROM binary_assets"
                    )
                rows = cursor.fetchall()
                return [{"name": r[0], "type": r[1], "category": r[2]} for r in rows]
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error listing assets: {e}")
            return []


if __name__ == "__main__":
    manager = StudioAssetManager()
    assets = manager.list_available_assets()
    print(f"Database: {manager.db_path}")
    print(f"Asset store: {manager.asset_store_dir}")
    print(f"Total binary assets registered: {len(assets)}")
