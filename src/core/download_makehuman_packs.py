import os
import re
import zipfile
import sqlite3
import socket
import urllib.request
from pathlib import Path

# Set global socket timeout to prevent indefinite hangs on stalled web connections
socket.setdefaulttimeout(15.0)

PACK_PAGES = [
    "https://static.makehumancommunity.org/assets/assetpacks/makehuman_system_assets.html",
    "https://static.makehumancommunity.org/assets/assetpacks/targets01_bodyparts.html",
    "https://static.makehumancommunity.org/assets/assetpacks/skins01.html",
    "https://static.makehumancommunity.org/assets/assetpacks/hair01.html",
    "https://static.makehumancommunity.org/assets/assetpacks/poses01.html"
]

def get_direct_zip_url(page_url: str) -> str:
    """Scrapes individual pack page to retrieve valid zip mirror URL."""
    try:
        req = urllib.request.Request(page_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) LRJK-Blender-AI-Studio'
        })
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            html = resp.read().decode('utf-8')
            zip_matches = re.findall(r'href="([^"]+\.zip)"', html)
            if zip_matches:
                link = zip_matches[0]
                if not link.startswith("http"):
                    link = f"https://static.makehumancommunity.org/assets/assetpacks/{link}"
                return link
    except Exception as e:
        print(f"⚠️ Page scrape error for {page_url}: {e}")
    return None

def download_file_with_progress(url: str, dest_path: Path):
    """Downloads files in 64KB chunks with live terminal progress reporting."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) LRJK-Blender-AI-Studio'
    })
    
    with urllib.request.urlopen(req, timeout=15.0) as resp, open(dest_path, 'wb') as out_file:
        total_size = resp.headers.get('content-length')
        total_bytes = int(total_size) if total_size else None
        downloaded = 0
        chunk_size = 65536  # 64 KB

        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out_file.write(chunk)
            downloaded += len(chunk)

            if total_bytes:
                percent = (downloaded / total_bytes) * 100
                print(f"\r progress: {downloaded / (1024*1024):.2f} MB / {total_bytes / (1024*1024):.2f} MB ({percent:.1f}%)", end="", flush=True)
            else:
                print(f"\r progress: {downloaded / (1024*1024):.2f} MB downloaded", end="", flush=True)
        print()

def download_and_index_packs(project_root: Path):
    assets_dir = project_root / "assets" / "makehuman"
    db_path = project_root / "studio_memory.db"
    assets_dir.mkdir(parents=True, exist_ok=True)

    print(f"📁 Target Extraction Directory: {assets_dir}")
    print(f"💾 Target SQLite Memory Database: {db_path}\n")

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

    total_indexed = 0

    for page_url in PACK_PAGES:
        pack_name = page_url.split("/")[-1].replace(".html", "")
        print(f"🔍 Locating download mirror for: {pack_name}...")
        
        direct_zip = get_direct_zip_url(page_url)
        if not direct_zip:
            print(f"❌ Skipping {pack_name}: No valid ZIP link found on page.\n")
            continue

        zip_filename = direct_zip.split("/")[-1]
        zip_temp_path = assets_dir / zip_filename

        print(f"🚀 Downloading: {zip_filename}...")
        try:
            download_file_with_progress(direct_zip, zip_temp_path)

            print(f"📦 Extracting {zip_filename}...")
            with zipfile.ZipFile(zip_temp_path, 'r') as zip_ref:
                zip_ref.extractall(assets_dir)

            if zip_temp_path.exists():
                os.remove(zip_temp_path)

            pack_indexed = 0
            for root, _, files in os.walk(assets_dir):
                for file in files:
                    ext = file.lower().split('.')[-1]
                    if ext in ["target", "obj", "blend", "py", "mhmat", "png", "npz"]:
                        file_full_path = str(Path(root) / file)
                        cursor.execute(
                            "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                            (file_full_path, ext, f"MakeHuman Asset: {file}")
                        )
                        if cursor.rowcount > 0:
                            pack_indexed += 1

            total_indexed += pack_indexed
            print(f"✅ Extracted & indexed {pack_indexed} asset files from {pack_name}\n")

        except Exception as e:
            print(f"\n❌ Failed processing {pack_name}: {e}\n")
            if zip_temp_path.exists():
                os.remove(zip_temp_path)

    conn.commit()
    conn.close()

    print("=========================================================")
    print(f"🎉 Complete! Added {total_indexed} local 3D assets into studio_memory.db.")

if __name__ == "__main__":
    root = Path(__file__).parent.parent.parent
    download_and_index_packs(root)