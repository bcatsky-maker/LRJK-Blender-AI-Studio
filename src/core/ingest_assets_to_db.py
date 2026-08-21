import os
import shutil
from pathlib import Path

from src.core.asset_manager import StudioAssetManager


def _register_moved_asset(
    manager: StudioAssetManager, asset_name: str, ext: str, category: str, dest_path: Path
) -> None:
    """Records metadata for a file that has already been moved into the asset store."""
    conn = manager._connect()
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
            (asset_name, ext, category, str(dest_path)),
        )
        conn.commit()
    finally:
        conn.close()


def ingest_assets_to_sqlite(project_root: Path):
    """
    Registers every supported file under assets/ into the on-disk asset
    store + studio_memory.db metadata (see StudioAssetManager). Files are
    MOVED into the asset store (not copied) so this doesn't double disk
    usage. Previously this function read each file into memory and wrote
    the bytes as a BLOB directly into studio_memory.db, which is what grew
    that file into the tens of gigabytes.
    """
    assets_root = project_root / "assets"
    manager = StudioAssetManager(project_root=project_root)

    print(f"💾 Target Memory Database: {manager.db_path}")
    print(f"📦 Target Asset Store: {manager.asset_store_dir}")
    print(f"📁 Scanning Assets Directory: {assets_root}\n")

    if not assets_root.exists():
        print("⚠️ Assets directory not found. Run download scripts first.")
        return

    supported_extensions = [
        "mp3",
        "ogg",
        "wav",
        "gltf",
        "glb",
        "obj",
        "fbx",
        "hdr",
        "exr",
        "jpg",
        "png",
    ]
    total_ingested = 0

    print("🚀 Moving raw asset files into the on-disk asset store and indexing metadata...")

    for root, _, files in os.walk(assets_root):
        category = Path(root).name
        for file in files:
            ext = file.lower().split(".")[-1]
            if ext not in supported_extensions:
                continue

            file_path = Path(root) / file
            asset_name = file_path.stem

            try:
                dest_dir = manager.asset_store_dir / category
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / f"{asset_name}.{ext}"
                shutil.move(str(file_path), str(dest_path))
                _register_moved_asset(manager, asset_name, ext, category, dest_path)
                total_ingested += 1
            except Exception as e:
                print(f"⚠️ Failed registering {file}: {e}")

    print("=========================================================")
    print(f"🎉 Success! Registered {total_ingested} assets.")
    print(f"   Files now live under: {manager.asset_store_dir}")
    print("   (Only metadata + this path is stored in studio_memory.db.)")


if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    ingest_assets_to_sqlite(project_root)
