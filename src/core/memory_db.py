"""
Execution & Knowledge Memory Database for LRJK Blender AI Studio.

This module defines ExecutionMemoryDB, the class the desktop UI
(src/ui/main_window.py) relies on for:
  - Tracking how many reference/knowledge files have been ingested
    ("Indexed Documents" counter).
  - Storing successfully generated & executed scene-generation results
    so they can be reviewed later in the "Saved Scripts" history dialog.
  - A lightweight keyword search over ingested reference material, used
    to surface a relevant BlendKit asset (or other reference) for a
    given prompt before falling back to AI/rule-based generation.

NOTE: Previously this file accidentally contained an unrelated
MakeHuman catalog-scraping helper instead of this class, which meant
`from src.core.memory_db import ExecutionMemoryDB` in main_window.py
failed at import time and the application could not start. That helper
now lives in src/core/ingest_makehuman_catalog.py where it belongs;
this file is dedicated to ExecutionMemoryDB.
"""

import sqlite3
from pathlib import Path

from src.core.paths import get_studio_db_path

# Common English words excluded from search_reference()'s keyword matching.
# Without this, a prompt like "a female figure that walks down the street
# with her handbag" contributes stopwords ("that", "down", "the", "with",
# "her") as search keywords - and since search_reference used to rank
# purely by recency (ORDER BY id DESC) rather than actual relevance, ANY
# indexed reference whose file_path/source happened to contain even one of
# those extremely common words would outrank everything else and get
# returned as the "best" match, regardless of whether it had anything to
# do with the prompt.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "then",
        "than",
        "that",
        "this",
        "these",
        "those",
        "with",
        "from",
        "into",
        "onto",
        "down",
        "up",
        "over",
        "under",
        "her",
        "him",
        "his",
        "she",
        "he",
        "it",
        "its",
        "they",
        "them",
        "their",
        "you",
        "your",
        "for",
        "of",
        "on",
        "in",
        "at",
        "to",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "as",
        "by",
        "not",
        "can",
        "will",
        "would",
        "should",
        "could",
    }
)


class ExecutionMemoryDB:
    """Thin, defensive SQLite wrapper backing the Studio's local memory."""

    def __init__(self, project_root: Path | None = None, db_path: Path | None = None):
        if db_path is not None:
            self.db_path = Path(db_path)
        elif project_root is not None:
            self.db_path = Path(project_root) / "studio_memory.db"
        else:
            self.db_path = get_studio_db_path()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Connection / schema helpers
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS saved_scripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt TEXT,
                    python_code TEXT,
                    exec_time REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reference_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE,
                    file_type TEXT,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Reference / knowledge ingestion index
    # ------------------------------------------------------------------
    def get_indexed_count(self) -> int:
        """Total number of ingested reference/knowledge entries."""
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM reference_index")
                row = cursor.fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error counting indexed documents: {e}")
            return 0

    def index_reference_file(self, file_path: str, file_type: str, source: str = "") -> bool:
        """Registers a reference file (add-on, model, doc, BlendKit ref string, ...)."""
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                    (str(file_path), file_type, source),
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error indexing reference file '{file_path}': {e}")
            return False

    def clear_blendkit_references(self) -> int:
        """
        Removes every attached-BlendKit-reference row from the searchable
        index (file_path values look like 'blendkit_asset_id:<id> ...' -
        see attach_blendkit_reference() in main_window.py). Returns how
        many rows were deleted.

        Without this, there was no way to make an attached reference stop
        being suggested short of attaching a new one over it - it stayed
        in reference_index (and therefore eligible to be matched by
        search_reference()) forever, for the lifetime of the database.
        """
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM reference_index WHERE file_path LIKE 'blendkit_asset_id:%'"
                )
                deleted = cursor.rowcount
                conn.commit()
                return max(deleted, 0)
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error clearing BlendKit references: {e}")
            return 0

    def search_reference(self, prompt: str, limit: int = 5) -> list[str]:
        """
        Small keyword search over ingested references. Returns matching
        file_path strings (which, for BlendKit references, look like
        'blendkit_asset_id:<id> asset_type:<type> ...') ranked by actual
        keyword overlap, most-relevant first - callers (notably
        main_window.py's generate_prompt handling, which treats
        result[0] as authoritative if it looks like a BlendKit reference)
        rely on that ordering being meaningful.

        Common stopwords ("the", "with", "her", ...) are excluded from
        matching - a previous version counted every word in the prompt as
        a search keyword and ranked purely by recency (most-recently-
        indexed reference wins), which meant an old, unrelated attached
        BlendKit reference could keep getting selected for completely
        different prompts for as long as it merely shared one common
        word with them.
        """
        prompt = (prompt or "").strip().lower()
        if not prompt:
            return []

        keywords = [w for w in prompt.split() if len(w) > 2 and w not in _STOPWORDS]
        if not keywords:
            return []

        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                # Cast a reasonably wide net at the SQL level (any keyword
                # match at all is a candidate), then rank candidates by
                # how many distinct keywords they actually match - this
                # part cannot be expressed as a single portable SQL ORDER
                # BY without FTS, so it's done here instead.
                clauses = " OR ".join(
                    ["LOWER(file_path) LIKE ? OR LOWER(source) LIKE ?"] * len(keywords)
                )
                params: list[str] = []
                for kw in keywords:
                    like = f"%{kw}%"
                    params.extend([like, like])

                cursor.execute(
                    f"SELECT id, file_path, source FROM reference_index WHERE {clauses} "
                    f"ORDER BY id DESC LIMIT 200",
                    params,
                )
                candidates = cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error searching references: {e}")
            return []

        scored = []
        for row_id, file_path, source in candidates:
            haystack = f"{file_path or ''} {source or ''}".lower()
            score = sum(1 for kw in keywords if kw in haystack)
            if score > 0:
                scored.append((score, row_id, file_path))

        # Highest keyword overlap first; recency only breaks ties between
        # otherwise-equally-relevant matches, never overrides relevance.
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [file_path for _score, _row_id, file_path in scored[:limit]]

    # ------------------------------------------------------------------
    # Saved / executed generation history
    # ------------------------------------------------------------------
    def save_successful_script(self, prompt: str, python_code: str, exec_time: float) -> bool:
        """
        Records a generation result for the history dialog. Despite the
        (kept-for-compatibility) parameter name, this is no longer raw
        exec()-able Python - see src/core/ai_provider.py and
        src/blender_addon/blender_rag_addon.py - it's the JSON-encoded
        {"action": ..., "params": ...} descriptor that was sent to Blender.
        """
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO saved_scripts (prompt, python_code, exec_time) VALUES (?, ?, ?)",
                    (prompt, python_code, float(exec_time)),
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error saving script history: {e}")
            return False

    def get_all_saved_scripts(self) -> list[tuple[int, str, float, str]]:
        """Returns (id, prompt, exec_time, created_at) rows, newest first."""
        try:
            conn = self._connect()
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, prompt, exec_time, created_at FROM saved_scripts ORDER BY id DESC"
                )
                return cursor.fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[WARN] Error fetching saved scripts: {e}")
            return []


if __name__ == "__main__":
    db = ExecutionMemoryDB()
    print(f"Database: {db.db_path}")
    print(f"Indexed references: {db.get_indexed_count()}")
    print(f"Saved scripts: {len(db.get_all_saved_scripts())}")
