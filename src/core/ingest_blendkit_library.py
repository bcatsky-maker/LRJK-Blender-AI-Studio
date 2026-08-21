import sqlite3
import json
import urllib.request
import time
from pathlib import Path

# Asset types available on BlendKit
BLENDKIT_ASSET_TYPES = ["model", "material", "hdri", "scene", "brush"]

def ingest_blendkit_assets(db_path: Path, max_pages_per_category: int = 5):
    """
    Queries BlendKit's API for free assets across all categories
    and registers their metadata and download links into studio_memory.db.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Ensure reference_index table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reference_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE,
            file_type TEXT,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    total_absorbed = 0

    for asset_type in BLENDKIT_ASSET_TYPES:
        print(f"🔍 Fetching free BlendKit assets for category: '{asset_type}'...")
        
        for page in range(1, max_pages_per_category + 1):
            # Query BlendKit search API for free assets in current category
            api_url = f"https://www.blendkit.com/api/v1/search/?asset_type={asset_type}&is_free=true&page={page}"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'LRJK-Blender-AI-Studio'})
            
            try:
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                    results = data.get("results", [])
                    
                    if not results:
                        break

                    for item in results:
                        asset_name = item.get("name", "Unknown Asset")
                        asset_id = item.get("id", "")
                        asset_base_id = item.get("assetBaseId", "")
                        tags = ", ".join(item.get("tags", []))
                        
                        # Format BlendKit Reference String for internal RAG parsing
                        ref_string = f"blendkit_asset_id:{asset_base_id} asset_type:{asset_type} name:{asset_name} tags:{tags}"
                        source_info = f"BlendKit Free Asset ({asset_type.upper()}): {asset_name}"

                        cursor.execute(
                            "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                            (ref_string, f"blendkit_{asset_type}", source_info)
                        )
                        if cursor.rowcount > 0:
                            total_absorbed += 1

                    print(f"  └─ Page {page}: Processed {len(results)} {asset_type} assets.")
                    time.sleep(0.2) # Polite API throttling

            except Exception as e:
                print(f"⚠️ Failed fetching page {page} for {asset_type}: {e}")
                break

    conn.commit()
    conn.close()

    print("=========================================================")
    print(f"🎉 Complete! Ingested {total_absorbed} free BlendKit assets into studio_memory.db.")

if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    db_file = project_root / "studio_memory.db"
    ingest_blendkit_assets(db_file, max_pages_per_category=10)