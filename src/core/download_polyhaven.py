import os
import json
import sqlite3
import urllib.request
from pathlib import Path

# Endpoints for all Poly Haven asset types
ASSET_TYPES = ["models", "textures", "hdris"]

def download_and_index_polyhaven_all(project_root: Path, target_count: int = None):
    assets_dir = project_root / "assets" / "polyhaven"
    db_path = project_root / "studio_memory.db"
    assets_dir.mkdir(parents=True, exist_ok=True)

    print(f"📁 Target Assets Directory: {assets_dir}")
    print(f"💾 Target Memory Database: {db_path}\n")

    # Connect with an extended 60-second busy timeout to prevent "database is locked" errors
    conn = sqlite3.connect(db_path, timeout=60.0)
    cursor = conn.cursor()
    
    # Enable WAL mode and explicit busy timeout handling for high-concurrency environments
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

    headers = {'User-Agent': 'LRJK-Blender-AI-Studio/1.0'}
    all_assets = []

    print("🔍 Fetching full catalog indices (Models, Textures, HDRIs)...")
    for asset_type in ASSET_TYPES:
        api_url = f"https://api.polyhaven.com/assets?type={asset_type}"
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                for asset_id, info in data.items():
                    all_assets.append((asset_id, asset_type, info.get('name', asset_id)))
            print(f"   └─ Loaded {len(data)} {asset_type} entries.")
        except Exception as e:
            print(f"⚠️ Error fetching {asset_type} index: {e}")

    total_available = len(all_assets)
    print(f"\n✅ Discovered {total_available} total Poly Haven assets.")

    # Process all available assets if target_count is None
    batch_list = all_assets if target_count is None else all_assets[:target_count]
    total_batch = len(batch_list)
    total_indexed = 0

    print(f"🚀 Processing full inventory queue of {total_batch} items...\n")

    for idx, (asset_id, asset_type, asset_name) in enumerate(batch_list):
        clean_name = "".join(c for c in asset_name if c.isalnum() or c in (' ', '_', '-')).rstrip()
        extract_target = assets_dir / asset_type / f"{clean_name}_{asset_id}"

        if extract_target.exists():
            print(f"⏭️ [{idx+1}/{total_batch}] {clean_name} ({asset_type}) exists locally. Skipping...")
            continue

        files_endpoint = f"https://api.polyhaven.com/files/{asset_id}"

        try:
            f_req = urllib.request.Request(files_endpoint, headers=headers)
            with urllib.request.urlopen(f_req, timeout=30) as f_resp:
                files_data = json.loads(f_resp.read().decode('utf-8'))

            dl_url = None
            file_extension = "glb"

            # Parse target formats based on asset category
            if asset_type == "models":
                gltf_node = files_data.get('gltf', {})
                if '1k' in gltf_node and 'gltf' in gltf_node['1k']:
                    dl_url = gltf_node['1k']['gltf'].get('url')
                    file_extension = "gltf"
                elif '1k' in gltf_node and 'glb' in gltf_node['1k']:
                    dl_url = gltf_node['1k']['glb'].get('url')
                    file_extension = "glb"
                else:
                    for quality in gltf_node.values():
                        if isinstance(quality, dict):
                            for fmt in quality.values():
                                if isinstance(fmt, dict) and 'url' in fmt:
                                    dl_url = fmt['url']
                                    break
                        if dl_url:
                            break

            elif asset_type == "textures":
                tex_node = (
                    files_data.get('Diffuse', {}).get('1k', {}).get('jpg', {}) or
                    files_data.get('diffuse', {}).get('1k', {}).get('jpg', {}) or
                    files_data.get('displacement', {}).get('1k', {}).get('jpg', {})
                )
                if isinstance(tex_node, dict):
                    dl_url = tex_node.get('url')
                    file_extension = "jpg"

            elif asset_type == "hdris":
                hdri_node = (
                    files_data.get('hdri', {}).get('1k', {}).get('hdr', {}) or
                    files_data.get('hdri', {}).get('1k', {}).get('exr', {})
                )
                if isinstance(hdri_node, dict):
                    dl_url = hdri_node.get('url')
                    file_extension = "hdr"

            if not dl_url:
                continue

            extract_target.mkdir(parents=True, exist_ok=True)
            file_dest = extract_target / f"{asset_id}.{file_extension}"

            print(f"📥 [{idx+1}/{total_batch}] Downloading {asset_type[:-1]}: {clean_name}...")
            dl_req = urllib.request.Request(dl_url, headers=headers)
            with urllib.request.urlopen(dl_req, timeout=60) as dl_resp, open(file_dest, 'wb') as out_f:
                out_f.write(dl_resp.read())

            pack_indexed = 0
            for root, _, files in os.walk(extract_target):
                for file in files:
                    ext = file.lower().split('.')[-1]
                    if ext in ["gltf", "glb", "blend", "obj", "fbx", "png", "jpg", "exr", "hdr"]:
                        full_path = str(Path(root) / file)
                        cursor.execute(
                            "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                            (full_path, ext, f"Poly Haven {asset_type.capitalize()}: {clean_name}")
                        )
                        if cursor.rowcount > 0:
                            pack_indexed += 1

            conn.commit()
            total_indexed += pack_indexed

        except Exception as e:
            print(f"⚠️ [{idx+1}/{total_batch}] Skipped {asset_id}: {e}")

    conn.close()
    print("=========================================================")
    print(f"🎉 Complete! Successfully ingested all available Poly Haven assets into studio_memory.db.")

if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    # Set target_count=None to iterate through the entire catalog
    download_and_index_polyhaven_all(project_root, target_count=None)