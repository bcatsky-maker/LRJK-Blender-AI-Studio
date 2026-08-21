import os
import re
import json
import sqlite3
import urllib.request
from pathlib import Path

# Possible Blender extension repository manifests
MANIFEST_CANDIDATES = [
    "https://extensions.blender.org/index.json",
    "https://extensions.blender.org/add-ons/index.json",
    "https://extensions.blender.org/api/v1/extensions/",
    "https://extensions.blender.org/catalog/index.json"
]

def fetch_and_absorb_all_extensions(project_root: Path):
    """
    Downloads extension zip files from official Blender repository endpoints
    and indexes them into studio_memory.db.
    """
    extensions_dir = project_root / "assets" / "blender_extensions"
    db_path = project_root / "studio_memory.db"
    extensions_dir.mkdir(parents=True, exist_ok=True)

    print(f"📁 Target Extension Directory: {extensions_dir}")
    print(f"💾 Target Memory Database: {db_path}\n")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reference_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE,
            file_type TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Fix: Define total_downloaded before any processing block
    total_downloaded = 0
    headers = {
        'User-Agent': 'Blender/4.2.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json, text/html'
    }

    manifest_data = None
    successful_url = None

    # Step 1: Attempt candidate JSON endpoints
    for url in MANIFEST_CANDIDATES:
        print(f"🔍 Testing repository endpoint: {url}...")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                if resp.status == 200:
                    raw_content = resp.read().decode('utf-8')
                    try:
                        manifest_data = json.loads(raw_content)
                        successful_url = url
                        print(f"✅ Endpoint matched: {url}")
                        break
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"   └─ Endpoint unreachable ({e})")

    # Step 2: Process JSON results if matched
    if manifest_data:
        extensions_list = []
        if isinstance(manifest_data, dict):
            extensions_list = manifest_data.get("data", manifest_data.get("extensions", manifest_data.get("results", [])))
        elif isinstance(manifest_data, list):
            extensions_list = manifest_data

        print(f"\n📦 Found {len(extensions_list)} items in manifest. Downloading...")
        for addon in extensions_list:
            if not isinstance(addon, dict):
                continue

            addon_id = addon.get("id", addon.get("module", ""))
            name = addon.get("name", addon_id or "Unknown Addon")
            archive_path = addon.get("archive_url") or addon.get("archive") or addon.get("download_url")

            if not archive_path:
                continue

            download_url = archive_path if archive_path.startswith("http") else f"https://extensions.blender.org/{archive_path.lstrip('/')}"
            zip_filename = f"{addon_id if addon_id else 'ext_' + str(total_downloaded)}.zip"
            zip_file_path = extensions_dir / zip_filename

            print(f"🚀 Downloading: {name}...")
            try:
                dl_req = urllib.request.Request(download_url, headers=headers)
                with urllib.request.urlopen(dl_req, timeout=30.0) as dl_resp, open(zip_file_path, 'wb') as out_f:
                    out_f.write(dl_resp.read())

                cursor.execute(
                    "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                    (str(zip_file_path), "blender_extension_zip", f"Blender Extension: {name}")
                )
                if cursor.rowcount > 0:
                    total_downloaded += 1
            except Exception as e:
                print(f"⚠️ Could not download {name}: {e}")

    # Step 3: Fallback direct HTML page scraper if endpoints are blocked/missing
    else:
        print("\n🌐 No JSON manifest resolved. Executing web catalog scraper...")
        portal_url = "https://extensions.blender.org/add-ons/"
        try:
            req = urllib.request.Request(portal_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                html = resp.read().decode('utf-8')
                zip_urls = set(re.findall(r'href="([^"]+\.zip)"', html))
                
                print(f"📦 Discovered {len(zip_urls)} extension ZIP archives.")
                for link in zip_urls:
                    full_url = link if link.startswith("http") else f"https://extensions.blender.org{link}"
                    zip_filename = full_url.split("/")[-1]
                    zip_file_path = extensions_dir / zip_filename

                    print(f"🚀 Downloading: {zip_filename}...")
                    try:
                        dl_req = urllib.request.Request(full_url, headers=headers)
                        with urllib.request.urlopen(dl_req, timeout=30.0) as dl_resp, open(zip_file_path, 'wb') as out_f:
                            out_f.write(dl_resp.read())

                        cursor.execute(
                            "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                            (str(zip_file_path), "blender_extension_zip", f"Blender Extension: {zip_filename}")
                        )
                        if cursor.rowcount > 0:
                            total_downloaded += 1
                    except Exception as e:
                        print(f"⚠️ Download failed for {zip_filename}: {e}")
        except Exception as e:
            print(f"❌ Portal catalog scrape failed: {e}")

    conn.commit()
    conn.close()

    print("\n=========================================================")
    print(f"🎉 Complete! Downloaded and indexed {total_downloaded} Blender extensions into studio_memory.db.")

if __name__ == "__main__":
    root_dir = Path(__file__).parent.parent.parent
    fetch_and_absorb_all_extensions(root_dir)