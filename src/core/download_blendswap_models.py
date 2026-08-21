import os
import re
import sys
import zipfile
import asyncio
import sqlite3
from pathlib import Path
from playwright.async_api import async_playwright

BLENDSWAP_CATALOG_URL = "https://blendswap.com/blends"

async def run_blendswap_ingestion(project_root: Path, max_downloads: int = 10):
    assets_dir = project_root / "assets" / "blendswap"
    db_path = project_root / "studio_memory.db"
    auth_state_path = project_root / "assets" / "blendswap_session.json"
    assets_dir.mkdir(parents=True, exist_ok=True)

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

    async with async_playwright() as p:
        print("🌐 Launching Chromium browser instance...")
        browser = await p.chromium.launch(headless=False)

        if auth_state_path.exists():
            print("🔑 Restoring saved login session...")
            context = await browser.new_context(storage_state=str(auth_state_path), accept_downloads=True)
        else:
            context = await browser.new_context(accept_downloads=True)

        page = await context.new_page()

        print("🔍 Accessing catalog...")
        await page.goto(BLENDSWAP_CATALOG_URL, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Check for authentication state
        if "login" in page.url.lower():
            print("\n⚠️ Session expired or not logged in. Please log in manually...")
            await page.wait_for_timeout(35000)
            await context.storage_state(path=str(auth_state_path))

        # Discover model links
        hrefs = await page.eval_on_selector_all('a[href*="/blend/"]', 'elements => elements.map(e => e.href)')
        model_ids = list(set([re.search(r'/blend/(\d+)', h).group(1) for h in hrefs if re.search(r'/blend/(\d+)', h)]))
        print(f"✅ Discovered {len(model_ids)} 3D model pages!\n")

        downloaded_count = 0
        quota_hit = False

        for idx, blend_id in enumerate(model_ids[:max_downloads]):
            if quota_hit:
                print("🛑 Daily/Monthly download quota is active. Halting operations for today.")
                break

            model_url = f"https://blendswap.com/blend/{blend_id}"
            extract_target = assets_dir / f"model_{blend_id}"

            if extract_target.exists():
                print(f"⏭️ [{idx+1}/{len(model_ids)}] Model {blend_id} already exists locally. Skipping...")
                continue

            print(f"🚀 [{idx+1}/{len(model_ids)}] Opening Model page: {blend_id}...")
            try:
                await page.goto(model_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                # Detect if BlendSwap display limit messages on page
                page_text = await page.content()
                if "quota" in page_text.lower() or "limit reached" in page_text.lower():
                    print("   └─ BlendSwap bandwidth/download quota reached on this account.")
                    quota_hit = True
                    break

                dl_button = page.locator('a[href*="/download/"], button:has-text("Download")').first
                if await dl_button.count() > 0 and await dl_button.is_visible():
                    async with page.expect_download(timeout=10000) as download_info:
                        await dl_button.click()

                    download = await download_info.value
                    zip_path = assets_dir / f"blendswap_{blend_id}.zip"
                    await download.save_as(zip_path)
                    print(f"   └─ Download complete: {zip_path.name}")

                    if zip_path.exists() and zipfile.is_zipfile(zip_path):
                        extract_target.mkdir(parents=True, exist_ok=True)
                        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                            zip_ref.extractall(extract_target)
                        os.remove(zip_path)

                        pack_indexed = 0
                        for root, _, files in os.walk(extract_target):
                            for file in files:
                                ext = file.lower().split('.')[-1]
                                if ext in ["blend", "obj", "fbx", "png", "jpg", "mtl"]:
                                    full_path = str(Path(root) / file)
                                    cursor.execute(
                                        "INSERT OR IGNORE INTO reference_index (file_path, file_type, source) VALUES (?, ?, ?)",
                                        (full_path, ext, f"BlendSwap Model: {file}")
                                    )
                                    if cursor.rowcount > 0:
                                        pack_indexed += 1

                        conn.commit()
                        downloaded_count += pack_indexed
                        print(f"✅ Extracted & indexed {pack_indexed} assets into studio_memory.db.")
                else:
                    print("   └─ Download button unclickable (Download limit reached or paid model).")

            except Exception as e:
                print(f"   └─ Download limit reached or timeout: {e}")

        await context.storage_state(path=str(auth_state_path))
        await browser.close()

    conn.close()
    print("\n=========================================================")
    print(f"🎉 Complete! Processed and indexed {downloaded_count} BlendSwap models into studio_memory.db.")

if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    asyncio.run(run_blendswap_ingestion(project_root, max_downloads=10))