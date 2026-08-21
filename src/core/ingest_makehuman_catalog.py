"""
Lightweight MakeHuman asset-pack catalog scraper.

This is intentionally separate from download_makehuman_packs.py:
this module only records *reference links* (zip URLs + titles) into
reference_index for later browsing/RAG use, it does not download or
extract anything. download_makehuman_packs.py is the module that
actually fetches a fixed set of packs and unpacks them to disk.

(Previously this function lived inside src/core/memory_db.py, which
was a bug: that file is supposed to define the ExecutionMemoryDB
class used by the desktop UI, and having this unrelated function
there instead meant the UI's `from src.core.memory_db import
ExecutionMemoryDB` import failed. Moved here so both modules have a
single, clear responsibility.)
"""

import re
import sqlite3
import urllib.request
from pathlib import Path


def ingest_makehuman_assets(db_path: Path) -> None:
    """Fetches MakeHuman asset pack metadata and stores it in the local SQLite memory DB."""
    url = "https://static.makehumancommunity.org/assets/assetpacks.html"
    req = urllib.request.Request(url, headers={"User-Agent": "LRJK-Blender-AI-Studio"})

    print("Fetching MakeHuman Asset Pack data...")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as response:
            html = response.read().decode("utf-8")

            # Simple regex parser for zip links and asset titles
            matches = re.findall(r'href="([^"]+\.zip)"[^>]*>([^<]+)</a>', html)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()

            # Create references table if missing
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reference_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE,
                    file_type TEXT,
                    source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            inserted_count = 0
            for zip_url, title in matches:
                full_url = (
                    zip_url
                    if zip_url.startswith("http")
                    else f"https://static.makehumancommunity.org/assets/{zip_url}"
                )
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                        (full_url, "zip", f"MakeHuman Asset Pack: {title.strip()}"),
                    )
                    if cursor.rowcount > 0:
                        inserted_count += 1
                except Exception as e:
                    print(f"Error inserting {full_url}: {e}")

            conn.commit()
            conn.close()
            print(
                f"Successfully stored {inserted_count} MakeHuman asset pack references into {db_path.name}!"
            )

    except Exception as e:
        print(f"Failed to retrieve MakeHuman data: {e}")


if __name__ == "__main__":
    db_file = Path(__file__).parent.parent.parent / "studio_memory.db"
    ingest_makehuman_assets(db_file)
