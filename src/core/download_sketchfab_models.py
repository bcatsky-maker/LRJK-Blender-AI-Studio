import json
import os
import sqlite3
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

# SECURITY: a real Sketchfab API token used to be hardcoded here as the
# fallback default and was committed to git history. That token should be
# treated as leaked - revoke/regenerate it from your Sketchfab account
# settings (https://sketchfab.com/settings/password -> API tokens) even
# after this fix. There is now NO hardcoded fallback: the token must be
# supplied via the SKETCHFAB_API_TOKEN environment variable (or the AI
# Settings dialog, once wired to persist it there) so a secret can never
# accidentally ship inside the source tree again.
SKETCHFAB_API_TOKEN = os.getenv("SKETCHFAB_API_TOKEN", "").strip()
SEARCH_TAGS = ["blender", "game-ready", "low-poly", "architecture", "vehicle", "character"]


class SketchfabTokenMissingError(RuntimeError):
    """Raised when no Sketchfab API token is configured."""


def download_and_index_sketchfab_100(project_root: Path, target_count: int = 100):
    if not SKETCHFAB_API_TOKEN:
        raise SketchfabTokenMissingError(
            "No Sketchfab API token configured. Set the SKETCHFAB_API_TOKEN "
            "environment variable (get a free token at "
            "https://sketchfab.com/settings/password under 'API tokens') "
            "before running Sketchfab ingestion."
        )

    assets_dir = project_root / "assets" / "sketchfab"
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
        "Authorization": f"Token {SKETCHFAB_API_TOKEN}",
        "User-Agent": "LRJK-Blender-AI-Studio/1.0",
    }

    discovered_models: list = []

    print(f"🔍 Fetching model catalog from Sketchfab API (Target: {target_count} models)...")

    # Phase 1: Query across multiple tags to collect model metadata
    for tag in SEARCH_TAGS:
        if len(discovered_models) >= target_count:
            break

        current_url = f"https://api.sketchfab.com/v3/search?type=models&downloadable=true&tags={tag}&sort_by=-likeCount"

        while len(discovered_models) < target_count and current_url:
            try:
                req = urllib.request.Request(current_url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    results = data.get("results", [])

                    if not results:
                        break

                    for model in results:
                        uid = model.get("uid")
                        name = model.get("name", f"model_{uid}")
                        if uid and (uid, name) not in discovered_models:
                            discovered_models.append((uid, name))
                            if len(discovered_models) >= target_count:
                                break

                    # Safe cursor and next URL resolution
                    next_link = data.get("next") or data.get("cursors", {}).get("next")
                    if not next_link:
                        break

                    if next_link.startswith("http"):
                        current_url = next_link
                    elif next_link.startswith("/"):
                        current_url = f"https://api.sketchfab.com{next_link}"
                    else:
                        current_url = f"https://api.sketchfab.com/v3/search?type=models&downloadable=true&tags={tag}&cursor={next_link}"

            except Exception as e:
                print(f"⚠️ Search notice for tag '{tag}': {e}")
                break

    total_queue = len(discovered_models)
    print(f"✅ Queued {total_queue} Sketchfab models for processing.\n")

    # Phase 2: Download archives, unpack assets, and index into DB
    total_indexed = 0

    for idx, (uid, name) in enumerate(discovered_models):
        clean_name = "".join(c for c in name if c.isalnum() or c in (" ", "_", "-")).rstrip()
        extract_target = assets_dir / f"{clean_name}_{uid}"

        if extract_target.exists():
            print(f"⏭️ [{idx+1}/{total_queue}] '{clean_name}' exists locally. Skipping...")
            continue

        print(f"🚀 [{idx+1}/{total_queue}] Downloading model: {clean_name} ({uid})...")
        dl_endpoint = f"https://api.sketchfab.com/v3/models/{uid}/download"

        try:
            dl_req = urllib.request.Request(dl_endpoint, headers=headers)
            with urllib.request.urlopen(dl_req, timeout=30) as dl_resp:
                dl_data = json.loads(dl_resp.read().decode("utf-8"))

            gltf_info = dl_data.get("gltf", {})
            zip_download_url = gltf_info.get("url")

            if not zip_download_url:
                print(f"   └─ No direct glTF payload for {uid}. Skipping...\n")
                continue

            zip_dest = assets_dir / f"{uid}.zip"

            zip_req = urllib.request.Request(zip_download_url)
            with (
                urllib.request.urlopen(zip_req, timeout=60) as zip_resp,
                open(zip_dest, "wb") as out_f,
            ):
                total_size = zip_resp.headers.get("content-length")
                total_bytes = int(total_size) if total_size else 0
                downloaded = 0
                chunk_size = 65536

                while True:
                    chunk = zip_resp.read(chunk_size)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes > 0:
                        pct = (downloaded / total_bytes) * 100
                        print(
                            f"\r      progress: {downloaded/(1024*1024):.2f} MB / {total_bytes/(1024*1024):.2f} MB ({pct:.1f}%)",
                            end="",
                            flush=True,
                        )
                print()

            if zip_dest.exists() and zipfile.is_zipfile(zip_dest):
                extract_target.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_dest, "r") as zip_ref:
                    zip_ref.extractall(extract_target)
                os.remove(zip_dest)

                pack_indexed = 0
                for root, _, files in os.walk(extract_target):
                    for file in files:
                        ext = file.lower().split(".")[-1]
                        if ext in ["gltf", "bin", "blend", "obj", "fbx", "png", "jpg", "jpeg"]:
                            full_path = str(Path(root) / file)
                            cursor.execute(
                                "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                                (full_path, ext, f"Sketchfab Model: {clean_name}"),
                            )
                            if cursor.rowcount > 0:
                                pack_indexed += 1

                conn.commit()
                total_indexed += pack_indexed
                print(f"✅ Extracted & indexed {pack_indexed} asset files into studio_memory.db.\n")

        except Exception as e:
            print(f"⚠️ Failed processing model {uid}: {e}\n")
            if "zip_dest" in locals() and zip_dest.exists():
                os.remove(zip_dest)

    conn.close()
    print("=========================================================")
    print(
        f"🎉 Complete! Successfully ingested {total_indexed} Sketchfab asset files into studio_memory.db."
    )


if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    download_and_index_sketchfab_100(project_root, target_count=100)
