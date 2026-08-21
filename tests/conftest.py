"""
Shared pytest fixtures / import shims.

`src.core.generator` (the heavy AIGenerator: terrain synthesis, MakeHuman
rigging, etc.) is not part of this test tree - it lives on the build machine
and is imported by src/ui/main_window.py at module load. So that the pure
UI-logic tests (scene-program construction, the silent auto-updater's
version/URL resolution) can import MainWindow without dragging in that whole
subsystem, we install a lightweight stub module *only when the real one is
absent*. On a full checkout/build machine the real generator.py is importable,
so this shim does nothing and the genuine module is used.
"""

import importlib
import sys
import types


def _ensure_generator_stub():
    try:
        importlib.import_module("src.core.generator")
        return  # real module present - use it, do nothing
    except Exception:
        pass

    stub = types.ModuleType("src.core.generator")

    class AIGenerator:  # minimal stand-in used only for import + display strings
        def generate_terrain(self, *args, **kwargs):
            return "terrain (stub)"

        def generate_character(self, *args, **kwargs):
            return "character (stub)"

    stub.AIGenerator = AIGenerator
    sys.modules["src.core.generator"] = stub


_ensure_generator_stub()
