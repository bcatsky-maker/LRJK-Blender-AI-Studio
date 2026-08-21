"""
Tests for the portable seed library that lets the installer bundle the
whole ingested asset catalog (build_seed) and register it on a fresh
install (import_seed).
"""

import sqlite3

from src.core.seed_library import _rel_to_asset_store, build_seed, import_seed


def _make_source(tmp_path):
    """Create a fake asset_store + studio_memory.db with a real file and a dangling row."""
    store = tmp_path / "asset_store"
    (store / "Adjustable Wrench_adjustable_wrench").mkdir(parents=True)
    gltf = store / "Adjustable Wrench_adjustable_wrench" / "adjustable_wrench.gltf"
    gltf.write_text("{}")
    (store / "Adjustable Wrench_adjustable_wrench" / "adjustable_wrench.bin").write_bytes(b"bin")

    db = tmp_path / "studio_memory.db"
    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE binary_assets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "asset_name TEXT UNIQUE, asset_type TEXT, source_category TEXT, file_path TEXT)"
    )
    c.execute(
        "INSERT INTO binary_assets (asset_name, asset_type, source_category, file_path) VALUES (?,?,?,?)",
        ("adjustable_wrench", "gltf", "polyhaven", str(gltf)),
    )
    # a row whose file does not exist on disk -> must be excluded from the seed
    c.execute(
        "INSERT INTO binary_assets (asset_name, asset_type, source_category, file_path) VALUES (?,?,?,?)",
        ("ghost", "obj", "polyhaven", str(store / "ghost" / "ghost.obj")),
    )
    c.commit()
    c.close()
    return db, store


def test_rel_to_asset_store_extracts_portable_path():
    p = r"G:\Software\LRJK Blender AI Studio\lrjk-blender-ai-studio\asset_store\wrench\wrench.gltf"
    assert _rel_to_asset_store(p) == "wrench/wrench.gltf"
    assert _rel_to_asset_store(None) is None
    assert _rel_to_asset_store("C:/somewhere/else/file.obj") is None


def test_build_seed_writes_relative_paths_and_skips_missing(tmp_path):
    db, store = _make_source(tmp_path)
    seed = tmp_path / "seed_library.db"
    n = build_seed(db, store, seed)
    assert n == 1  # ghost row skipped (file missing)

    rows = (
        sqlite3.connect(seed)
        .execute("SELECT asset_name, asset_type, source_category, rel_path FROM binary_assets")
        .fetchall()
    )
    assert rows == [
        (
            "adjustable_wrench",
            "gltf",
            "polyhaven",
            "Adjustable Wrench_adjustable_wrench/adjustable_wrench.gltf",
        )
    ]


def test_import_seed_resolves_against_bundled_store(tmp_path):
    db, store = _make_source(tmp_path)
    seed = tmp_path / "seed_library.db"
    build_seed(db, store, seed)

    # Recipient machine: fresh empty user DB, bundled asset_store == our store.
    user_db = tmp_path / "user_runtime.db"
    imported = import_seed(user_db, seed, store)
    assert imported == 1

    row = (
        sqlite3.connect(user_db)
        .execute("SELECT asset_name, source_category, file_path FROM binary_assets")
        .fetchone()
    )
    assert row[0] == "adjustable_wrench"
    assert row[1] == "bundled:polyhaven"  # tagged so we can detect prior import
    assert row[2].endswith("adjustable_wrench.gltf")
    # The resolved absolute path points at a real file under the bundled store.
    from pathlib import Path

    assert Path(row[2]).exists()


def test_import_seed_is_idempotent(tmp_path):
    db, store = _make_source(tmp_path)
    seed = tmp_path / "seed_library.db"
    build_seed(db, store, seed)
    user_db = tmp_path / "user_runtime.db"

    assert import_seed(user_db, seed, store) == 1
    assert import_seed(user_db, seed, store) == 0  # second run does nothing


def test_import_seed_missing_seed_is_noop(tmp_path):
    assert import_seed(tmp_path / "user.db", tmp_path / "nonexistent_seed.db", tmp_path) == 0


def test_seeded_assets_are_searchable_via_asset_manager(tmp_path):
    """End-to-end: after seeding, StudioAssetManager.search_assets finds the model."""
    from src.core.asset_manager import StudioAssetManager

    db, store = _make_source(tmp_path)
    seed = tmp_path / "seed_library.db"
    build_seed(db, store, seed)

    user_db = tmp_path / "user_runtime.db"
    import_seed(user_db, seed, store)

    manager = StudioAssetManager(db_path=user_db, asset_store_dir=tmp_path / "unused_store")
    results = manager.search_assets("wrench")
    assert any(r["name"] == "adjustable_wrench" for r in results)
