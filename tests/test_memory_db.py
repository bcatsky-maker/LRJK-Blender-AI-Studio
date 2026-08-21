import json

import pytest

from src.core.memory_db import ExecutionMemoryDB


def test_fresh_db_has_zero_indexed_and_no_scripts(tmp_path):
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")
    assert db.get_indexed_count() == 0
    assert db.get_all_saved_scripts() == []


def test_index_reference_file_and_search(tmp_path):
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")
    db.index_reference_file(
        "blendkit_asset_id:abc123 asset_type:model",
        "blendkit_model",
        source="Attached BlendKit Reference: foo donut",
    )
    assert db.get_indexed_count() == 1

    results = db.search_reference("give me a donut please")
    assert any("blendkit_asset_id:abc123" in r for r in results)


def test_search_reference_ignores_empty_prompt(tmp_path):
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")
    assert db.search_reference("") == []
    assert db.search_reference("   ") == []


def test_search_reference_ranks_by_relevance_not_recency(tmp_path):
    """
    Regression test for a real bug: search_reference used to rank purely
    by recency (ORDER BY id DESC) and treated every word in the prompt -
    including stopwords like "the"/"with"/"her" - as a search keyword.
    That meant an old, unrelated BlendKit reference could keep getting
    selected for brand-new, unrelated prompts for as long as it was the
    most recently attached one and happened to share a single common
    word - which is exactly what a user hit in practice (two completely
    different prompts both matched whichever BlendKit reference had been
    attached most recently).
    """
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")

    # Indexed first (older / lower id) - genuinely relevant to the prompt below.
    db.index_reference_file(
        "blendkit_asset_id:relevant111 asset_type:model",
        "blendkit_model",
        source="Attached BlendKit Reference: female figure handbag street scene",
    )
    # Indexed second (newer / higher id, so it would win under pure
    # recency ranking) - shares only a stopword ("her") with the prompt,
    # nothing meaningful.
    db.index_reference_file(
        "blendkit_asset_id:irrelevant222 asset_type:model",
        "blendkit_model",
        source="Attached BlendKit Reference: https://blendkit.com/models/spaceship-with-her-crew",
    )

    results = db.search_reference(
        "Create a female figure that walks down the street with her handbag"
    )
    assert results, "expected the genuinely relevant reference to match"
    assert "blendkit_asset_id:relevant111" in results[0]
    assert not any("irrelevant222" in r for r in results)


def test_clear_blendkit_references_removes_only_blendkit_rows(tmp_path):
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")
    db.index_reference_file(
        "blendkit_asset_id:abc123 asset_type:model",
        "blendkit_model",
        source="Attached BlendKit Reference: foo donut",
    )
    db.index_reference_file(
        "blendkit_asset_id:def456 asset_type:hdri",
        "blendkit_hdri",
        source="Attached BlendKit Reference: bar sky",
    )
    # A non-BlendKit reference (e.g. from bulk extension ingestion) should
    # survive - clearing BlendKit references is not a blanket wipe.
    db.index_reference_file("some/addon/manual.pdf", "pdf")

    assert db.get_indexed_count() == 3

    removed = db.clear_blendkit_references()
    assert removed == 2
    assert db.get_indexed_count() == 1
    assert db.search_reference("give me a donut please") == []

    # Calling again on an already-cleared DB is a safe no-op.
    assert db.clear_blendkit_references() == 0


def test_save_and_fetch_scripts(tmp_path):
    db = ExecutionMemoryDB(db_path=tmp_path / "test.db")
    payload = json.dumps({"action": "generate_terrain", "params": {}})
    assert db.save_successful_script("blue terrain", payload, 0.1234)

    rows = db.get_all_saved_scripts()
    assert len(rows) == 1
    _, prompt, exec_time, _ = rows[0]
    assert prompt == "blue terrain"
    assert exec_time == pytest.approx(0.1234)


def test_reopening_same_db_path_persists_data(tmp_path):
    db_path = tmp_path / "test.db"
    db1 = ExecutionMemoryDB(db_path=db_path)
    db1.index_reference_file("some/file.py", "py")

    db2 = ExecutionMemoryDB(db_path=db_path)
    assert db2.get_indexed_count() == 1
