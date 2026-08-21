import os
from pathlib import Path
from PIL import Image

def generate_inno_assets(assets_dir: str = "assets"):
    """
    Checks for app_banner.png and app_icon.png in the assets directory,
    then generates Inno Setup wizard BMP assets and the Windows ICO file.
    """
    assets_path = Path(assets_dir)
    banner_file = assets_path / "app_banner.png"
    icon_source_file = assets_path / "app_icon.png"
    
    os.makedirs(assets_path, exist_ok=True)

    # 1. Process Banner (`app_banner.png`) for Inno Setup Wizard assets
    if banner_file.exists():
        print(f"🎨 Found banner source '{banner_file}'. Processing wizard assets...")
        img = Image.open(banner_file).convert("RGB")
        width, height = img.size

        # Wizard Large Banner (164x314 pixels)
        crop_banner_box = (0, 0, int(width * 0.45), height)
        left_section = img.crop(crop_banner_box)
        banner_img = left_section.resize((164, 314), Image.Resampling.LANCZOS)
        banner_path = assets_path / "wizard_banner.bmp"
        banner_img.save(banner_path, "BMP")
        print(f"  [x] Saved: {banner_path}")

        # Wizard Small Header Icon (55x58 pixels)
        crop_small_box = (int(width * 0.35), int(height * 0.05), int(width * 0.55), int(height * 0.95))
        emblem_section = img.crop(crop_small_box)
        small_img = emblem_section.resize((55, 58), Image.Resampling.LANCZOS)
        small_path = assets_path / "wizard_small.bmp"
        small_img.save(small_path, "BMP")
        print(f"  [x] Saved: {small_path}")
    else:
        print(f"⚠️ Warning: '{banner_file}' not found. Skipping wizard banner generation.")

    # 2. Process App Icon (`app_icon.png`) into Windows `.ico` format
    if icon_source_file.exists():
        print(f"⚙️ Found icon source '{icon_source_file}'. Generating app_icon.ico...")
        icon_img = Image.open(icon_source_file)
        ico_path = assets_path / "app_icon.ico"
        icon_img.save(
            ico_path, 
            format="ICO", 
            sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)]
        )
        print(f"  [x] Saved: {ico_path}")
    else:
        print(f"⚠️ Warning: '{icon_source_file}' not found. Skipping .ico generation.")

    print("\n✨ Asset generation complete!")

if __name__ == "__main__":
    generate_inno_assets()