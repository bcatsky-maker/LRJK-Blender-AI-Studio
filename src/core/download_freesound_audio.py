import os
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from pathlib import Path

# Internet Archive Advanced Search API
IA_SEARCH_URL = "https://archive.org/advancedsearch.php"

# Expanded audio category topics to reach 5,000 items
SEARCH_TOPICS = [
    'mediatype:audio AND (subject:"sound effects" OR subject:"foley" OR subject:"sfx")',
    'mediatype:audio AND (subject:"game audio" OR subject:"field recording" OR subject:"ambient")',
    'mediatype:audio AND (subject:"audio sample" OR subject:"royalty free" OR subject:"soundbank")'
]

def download_and_index_5000_ia_audio(project_root: Path, target_count: int = 5000):
    assets_dir = project_root / "assets" / "sound_effects"
    db_path = project_root / "studio_memory.db"
    assets_dir.mkdir(parents=True, exist_ok=True)

    print(f"📁 Target Assets Directory: {assets_dir}")
    print(f"💾 Target Memory Database: {db_path}\n")

    conn = sqlite3.connect(db_path, timeout=60.0)
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout = 60000;")
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

    headers = {
        'User-Agent': 'LRJK-Blender-AI-Studio/1.0 (contact@lrjk-studio.org)'
    }

    discovered_items = []

    print(f"🔍 Crawling Internet Archive audio repositories (Target: {target_count} files)...")

    # Phase 1: Harvest bulk audio collection IDs across pages
    for topic_query in SEARCH_TOPICS:
        if len(discovered_items) >= 1000:
            break

        for page in range(1, 6):
            params = {
                'q': topic_query,
                'fl[]': ['identifier', 'title'],
                'sort[]': 'downloads desc',
                'rows': '200',
                'page': str(page),
                'output': 'json'
            }
            encoded_url = f"{IA_SEARCH_URL}?{urllib.parse.urlencode(params, doseq=True)}"

            try:
                req = urllib.request.Request(encoded_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    docs = data.get('response', {}).get('docs', [])
                    for doc in docs:
                        item_tuple = (doc.get('identifier'), doc.get('title', 'sound_effect'))
                        if item_tuple not in discovered_items:
                            discovered_items.append(item_tuple)
                time.sleep(0.1)
            except Exception as e:
                print(f"⚠️ Search page notice: {e}")
                break

    print(f"   └─ Identified {len(discovered_items)} primary sound collections.")

    discovered_files = []
    print("\n🔍 Extracting direct MP3/OGG file paths...")

    # Phase 2: Extract file entries up to target_count
    for identifier, title in discovered_items:
        if len(discovered_files) >= target_count:
            break

        meta_url = f"https://archive.org/metadata/{identifier}/files"
        try:
            req = urllib.request.Request(meta_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                file_list = data.get('result', [])

                for f in file_list:
                    fname = f.get('name', '')
                    # Filter for playable audio types and ignore system metadata hidden files
                    if fname.endswith(('.mp3', '.ogg', '.wav')) and not fname.startswith('.'):
                        clean_fname = "".join(c for c in fname if c.isalnum() or c in ('.', '_', '-'))
                        dl_url = f"https://archive.org/download/{identifier}/{urllib.parse.quote(fname)}"
                        discovered_files.append((clean_fname, dl_url, identifier))
                        if len(discovered_files) >= target_count:
                            break
            time.sleep(0.05)
        except Exception:
            continue

    total_queue = len(discovered_files)
    print(f"\n✅ Queued {total_queue} direct audio download links.")
    print("🚀 Ingesting audio files into studio_memory.db...\n")

    # Phase 3: Mass download & database registration
    total_indexed = 0

    for idx, (clean_name, dl_url, identifier) in enumerate(discovered_files):
        ext = clean_name.split('.')[-1].lower()
        file_dest = assets_dir / f"{identifier}_{clean_name}"

        if file_dest.exists():
            continue

        try:
            req = urllib.request.Request(dl_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_length = resp.headers.get('Content-Length')
                # Skip files larger than 50MB (full audiobooks or continuous background loops)
                if content_length and int(content_length) > 52428800:
                    continue

                data = resp.read()
                if len(data) < 1000:
                    continue

                with open(file_dest, 'wb') as out_f:
                    out_f.write(data)

            cursor.execute(
                "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                (str(file_dest), ext, f"Internet Archive Audio ({identifier}): {clean_name}")
            )
            if cursor.rowcount > 0:
                total_indexed += 1
                if total_indexed % 50 == 0:
                    print(f"📥 High-capacity Progress: Ingested {total_indexed}/{target_count} files into studio_memory.db...")

            conn.commit()
            time.sleep(0.05)

        except Exception as e:
            continue

    conn.close()
    print("\n=========================================================")
    print(f"🎉 Complete! Successfully ingested {total_indexed} audio files into studio_memory.db.")

if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    download_and_index_5000_ia_audio(project_root, target_count=5000)