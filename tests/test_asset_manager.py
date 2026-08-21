import sqlite3
from pathlib import Path

from src.core.asset_manager import StudioAssetManager


def test_store_and_retrieve_bytes_round_trip(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    manager.store_asset_bytes("rock01", "png", "polyhaven", b"fake-image-bytes")

    assert manager.get_asset_bytes("rock01") == b"fake-image-bytes"

    out_path = tmp_path / "extracted" / "rock01.png"
    assert manager.load_asset_to_disk("rock01", out_path) is True
    assert out_path.read_bytes() == b"fake-image-bytes"


def test_missing_asset_returns_none_and_false(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    assert manager.get_asset_bytes("does_not_exist") is None
    assert manager.load_asset_to_disk("does_not_exist", tmp_path / "out.bin") is False


def test_new_rows_never_store_a_blob(tmp_path):
    db_path = tmp_path / "studio_memory.db"
    manager = StudioAssetManager(db_path=db_path, asset_store_dir=tmp_path / "asset_store")
    manager.store_asset_bytes("tree01", "obj", "sketchfab", b"obj-data")

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(binary_assets)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "file_data" not in columns  # a fresh DB never gets the legacy blob column at all

    row = conn.execute(
        "SELECT file_path FROM binary_assets WHERE asset_name = ?", ("tree01",)
    ).fetchone()
    conn.close()

    assert row[0] is not None
    assert Path(row[0]).exists()
    assert Path(row[0]).read_bytes() == b"obj-data"


def test_backward_compatible_with_legacy_blob_rows(tmp_path):
    """
    Simulates an existing large database that still has the old
    file_data BLOB column with data in it (like the user's real
    18.5GB studio_memory.db) and confirms reads still work without
    running the migration script first.
    """
    db_path = tmp_path / "studio_memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE binary_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_name TEXT UNIQUE,
            asset_type TEXT,
            source_category TEXT,
            file_data BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    conn.execute(
        "INSERT INTO binary_assets (asset_name, asset_type, source_category, file_data) VALUES (?, ?, ?, ?)",
        ("legacy_asset", "png", "polyhaven", b"legacy-bytes"),
    )
    conn.commit()
    conn.close()

    manager = StudioAssetManager(db_path=db_path, asset_store_dir=tmp_path / "asset_store")
    assert manager.get_asset_bytes("legacy_asset") == b"legacy-bytes"

    out_path = tmp_path / "out" / "legacy_asset.png"
    assert manager.load_asset_to_disk("legacy_asset", out_path) is True
    assert out_path.read_bytes() == b"legacy-bytes"


def test_store_asset_bytes_overwrites_existing_name(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    manager.store_asset_bytes("rock01", "png", "polyhaven", b"v1")
    manager.store_asset_bytes("rock01", "png", "polyhaven", b"v2")
    assert manager.get_asset_bytes("rock01") == b"v2"


def test_search_assets_finds_importable_models_and_ranks(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    # Two model assets + one texture. Only importable model types should match.
    manager.store_asset_bytes("wooden_chair_old", "obj", "sketchfab", b"obj-bytes")
    manager.store_asset_bytes("modern_chair", "glb", "sketchfab", b"glb-bytes")
    manager.store_asset_bytes("wood_texture", "png", "polyhaven", b"png-bytes")  # not a mesh

    results = manager.search_assets("wooden chair")
    names = [r["name"] for r in results]
    # Both chairs match "chair"; the wooden one matches 2 keywords so ranks first.
    assert names[0] == "wooden_chair_old"
    assert "modern_chair" in names
    # The texture is never returned as importable geometry.
    assert all(r["type"] in StudioAssetManager.IMPORTABLE_MESH_TYPES for r in results)
    assert "wood_texture" not in names


def test_search_assets_empty_query_returns_nothing(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    manager.store_asset_bytes("chair", "obj", "sketchfab", b"x")
    assert manager.search_assets("") == []
    assert manager.search_assets("   ") == []


def test_search_assets_no_match_returns_empty(tmp_path):
    manager = StudioAssetManager(
        db_path=tmp_path / "studio_memory.db", asset_store_dir=tmp_path / "asset_store"
    )
    manager.store_asset_bytes("chair", "obj", "sketchfab", b"x")
    assert manager.search_assets("spaceship") == []
