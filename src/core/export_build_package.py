import shutil
import sqlite3
from pathlib import Path

def export_source_and_database(project_root: Path, output_dir: Path):
    """Packages studio_memory.db and absorbed add-on python modules for the next build."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    db_source = project_root / "studio_memory.db"
    addons_source = project_root / "src" / "core" / "absorbed_addons"

    # 1. Export Active SQLite Database Memory
    if db_source.exists():
        shutil.copy2(db_source, output_dir / "studio_memory.db")
        print(f"✅ Exported Database Memory -> {output_dir / 'studio_memory.db'}")

    # 2. Export Absorbed Addon Source Code Modules
    if addons_source.exists():
        addons_output = output_dir / "absorbed_addons"
        if addons_output.exists():
            shutil.rmtree(addons_output)
        shutil.copytree(addons_source, addons_output)
        print(f"📦 Exported Absorbed Add-on Python Code -> {addons_output}")

    print("\n🎉 Build package ready! Copy these files into your next PyInstaller / Inno Setup build root.")

if __name__ == "__main__":
    root = Path(__file__).parent.parent.parent
    export_target = root / "dist" / "next_build_package"
    export_source_and_database(root, export_target)