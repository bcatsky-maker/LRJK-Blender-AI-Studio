"""
Tests for the desktop app's scene-program construction:
  - _rule_based_program: the no-API-key fallback now builds a real lit,
    framed multi-object scene instead of a single grey primitive.
  - _resolve_library_imports: import_asset_from_library actions get
    resolved against the ingested asset store and rewritten into concrete
    import_mesh_file actions (the ingestion->generation loop).

These call the methods unbound against a lightweight shim so we don't have
to spin up the whole PySide6 MainWindow (bridge server, widgets, etc.).
"""

import types
from unittest.mock import MagicMock

from src.ui.main_window import MainWindow


def _program_shim(library=None):
    """library: {query: [asset, ...]} mapping so search_assets('road') and
    search_assets('tree') can return different things (or nothing). Default
    empty → the road builder uses its procedural road + primitive trees."""
    library = library or {}
    obj = types.SimpleNamespace()
    for m in (
        "parse_colors_from_prompt",
        "_donut_actions",
        "_scene_frame_actions",
        "_road_actions",
        "_library_has_asset",
    ):
        setattr(obj, m, types.MethodType(getattr(MainWindow, m), obj))
    # _parse_length_m is a @staticmethod - attach it unbound, not as a method.
    obj._parse_length_m = MainWindow._parse_length_m
    # _road_actions logs progress; give it a no-op console.
    obj.console_dialog = MagicMock()
    obj.asset_manager = MagicMock()
    obj.asset_manager.search_assets.side_effect = lambda q, *a, **k: library.get(q, [])
    return obj


def test_rule_based_program_builds_lit_framed_scene():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, "a golden sphere")
    names = [a["action"] for a in program]
    # A real scene: geometry + material + light + camera (not a lone primitive).
    assert "add_primitive" in names
    assert "add_material" in names
    assert "add_light" in names
    assert "set_camera" in names


def test_rule_based_gold_maps_to_metallic_material():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, "a shiny golden sphere")
    subject = next(
        a
        for a in program
        if a["action"] == "add_primitive" and a["params"].get("name") == "AI_Subject"
    )
    assert subject["params"]["shape"] == "sphere"
    material = next(a for a in program if a["action"] == "add_material")
    assert material["params"]["metallic"] == 1.0  # "golden" -> metallic


# --- Road / street layout builder (the case that used to collapse into a
#     single failed BlendKit import) ------------------------------------------

ROAD_PROMPT = (
    "build a 1 km road with roads exiting to other roads on the left "
    "every 300m. add sidewalks on both sides and trees every 10m next "
    "to the sidewalk"
)


def test_road_prompt_builds_multi_object_scene():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)
    names = [a["action"] for a in program]
    # It is a real, many-action scene - not a lone import.
    assert len(program) > 50
    assert "add_light" in names and "set_camera" in names
    # Named parts we expect from the parsed prompt.
    made = [a["params"].get("name", "") for a in program if a["action"] == "add_primitive"]
    assert any(n == "AI_Road" for n in made)
    assert any(n.startswith("AI_Sidewalk_") for n in made)  # "sidewalks both sides"
    assert any(n.startswith("AI_Branch_") for n in made)  # "roads exiting ... every 300m"
    assert any(n.startswith("AI_TreeL_") for n in made)  # trees, left row
    assert any(n.startswith("AI_TreeR_") for n in made)  # trees, right row


def test_road_uses_duplicate_object_for_tree_rows():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)
    # 1 km, trees every 10m => ~101 positions per side; instanced via
    # duplicate_object rather than 200 separate primitives+materials.
    dups = [a for a in program if a["action"] == "duplicate_object"]
    assert len(dups) >= 180
    # Every duplicate carries a Y offset (spacing along the road).
    assert all(a["params"]["offset"][1] != 0 for a in dups)


def test_trees_have_trunk_and_canopy():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)
    prims = [a for a in program if a["action"] == "add_primitive"]
    # A real tree = a brown trunk (cylinder) + a green canopy (sphere), both
    # sitting under the AI_Tree{L,R}_ name prefix.
    trunk = next(
        a
        for a in prims
        if a["params"].get("name") == "AI_TreeL_0" and a["params"]["shape"] == "cylinder"
    )
    canopy = next(
        a
        for a in prims
        if a["params"].get("name") == "AI_TreeL_0c" and a["params"]["shape"] == "sphere"
    )
    assert trunk and canopy
    # Both parts are instanced down the row (trunk copy + canopy copy per step).
    dup_names = [a["params"]["name"] for a in program if a["action"] == "duplicate_object"]
    assert "AI_TreeL_1" in dup_names and "AI_TreeL_1c" in dup_names


def test_road_uses_real_tree_models_when_library_has_them():
    # With a real tree model in the library, the road imports it once per side
    # and instances it down the row (linked) instead of building primitive trees.
    tree_asset = [
        {"name": "oak_tree", "type": "glb", "category": "polyhaven", "file_path": "/x/oak.glb"}
    ]
    obj = _program_shim(library={"tree": tree_asset})
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)

    imports = [a for a in program if a["action"] == "import_asset_from_library"]
    assert len(imports) == 2  # one real tree imported per side
    assert all(a["params"]["query"] == "tree" for a in imports)
    assert {a["params"]["name"] for a in imports} == {"AI_TreeL_0", "AI_TreeR_0"}

    # The rest of the row is cheap linked instances of the imported tree.
    tree_dups = [
        a
        for a in program
        if a["action"] == "duplicate_object" and a["params"]["name"].startswith("AI_Tree")
    ]
    assert len(tree_dups) >= 180
    assert all(a["params"].get("linked") is True for a in tree_dups)
    # No primitive trunk/canopy shapes were built for trees in this mode.
    prim_names = [a["params"].get("name", "") for a in program if a["action"] == "add_primitive"]
    assert not any(n.startswith("AI_Tree") for n in prim_names)


def test_road_uses_real_road_model_tiled_when_library_has_it():
    # A real road model → import one segment with tile_length, skip the
    # procedural asphalt cube AND the painted markings (the model has its own).
    road_asset = [{"name": "asphalt_road", "type": "glb", "file_path": "/x/road.glb"}]
    obj = _program_shim(library={"road": road_asset})
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)

    road_import = next(
        a
        for a in program
        if a["action"] == "import_asset_from_library" and a["params"]["query"] == "road"
    )
    assert road_import["params"]["tile_length"] == 1000.0
    assert road_import["params"]["tile_axis"] == "y"
    names = [a["params"].get("name", "") for a in program if a["action"] == "add_primitive"]
    assert "AI_Road" not in names  # no procedural asphalt strip
    assert not any(n.startswith("AI_EdgeLine_") or n.startswith("AI_CenterDash_") for n in names)
    # Sidewalks and branches are still procedural.
    assert any(n.startswith("AI_Sidewalk_") for n in names)
    assert any(n.startswith("AI_Branch_") for n in names)


def test_road_has_lane_markings():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)
    names = [a["params"].get("name", "") for a in program if a["action"] == "add_primitive"]
    # Two solid edge lines + a dashed centre line.
    assert "AI_EdgeLine_L" in names and "AI_EdgeLine_R" in names
    assert "AI_CenterDash_0" in names
    dash_dups = [
        a
        for a in program
        if a["action"] == "duplicate_object" and a["params"]["name"].startswith("AI_CenterDash_")
    ]
    assert len(dash_dups) >= 40  # ~83 dashes over a 1 km road at 12 m spacing


def test_road_length_and_spacing_are_parsed():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, ROAD_PROMPT)
    road = next(
        a
        for a in program
        if a["action"] == "add_primitive" and a["params"].get("name") == "AI_Road"
    )
    # size-2 cube, half-length scale on Y => 1000 m road -> scale.y == 500.
    assert abs(road["params"]["scale"][1] - 500.0) < 1e-6
    # 3 branch roads at 300 m spacing over a 1 km road.
    branches = [
        a
        for a in program
        if a["action"] == "add_primitive" and a["params"].get("name", "").startswith("AI_Branch_")
    ]
    assert len(branches) == 3


def test_parse_length_km_and_m():
    assert MainWindow._parse_length_m("a 1 km road", 0) == 1000.0
    assert MainWindow._parse_length_m("a 750m path", 0) == 750.0
    assert MainWindow._parse_length_m("no number here", 500) == 500.0


def test_street_keyword_also_triggers_road_builder():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, "a short street with trees")
    made = [a["params"].get("name", "") for a in program if a["action"] == "add_primitive"]
    assert any(n == "AI_Road" for n in made)


# --- Precise-layout routing: measured road prompts must go to the exact
#     deterministic builder, NOT the AI's loose approximation ------------------


def test_measured_road_prompt_wants_precise_layout():
    assert MainWindow._wants_precise_layout(ROAD_PROMPT) is True
    assert MainWindow._wants_precise_layout("a 500 m highway") is True
    assert MainWindow._wants_precise_layout("a street with sidewalks") is True
    assert MainWindow._wants_precise_layout("a road, trees every 12m") is True


def test_unmeasured_or_nonroad_prompts_do_not_force_precise_layout():
    # No measurements -> let the AI be creative.
    assert MainWindow._wants_precise_layout("a winding country road at sunset") is False
    # Not a layout at all.
    assert MainWindow._wants_precise_layout("a golden donut with sprinkles") is False
    assert MainWindow._wants_precise_layout("a 1 km wide lake") is False  # no road kw


def test_rule_based_donut_has_dough_icing_and_sprinkles():
    obj = _program_shim()
    program = MainWindow._rule_based_program(obj, "a vibrant blue donut")
    prim_names = [a["params"].get("name") for a in program if a["action"] == "add_primitive"]
    # Dough (AI_Subject torus) + icing torus + multiple sprinkles - a real donut.
    assert "AI_Subject" in prim_names
    assert "AI_Icing" in prim_names
    sprinkles = [n for n in prim_names if n and n.startswith("AI_Sprinkle")]
    assert len(sprinkles) >= 6
    names = [a["action"] for a in program]
    assert "add_light" in names and "set_camera" in names


def test_resolve_library_imports_imports_in_place_when_file_exists(tmp_path):
    # A real gltf/obj must be imported from its ORIGINAL location so its
    # sibling .bin/.mtl/textures resolve - not copied to a flat cache.
    real_asset = tmp_path / "store" / "wooden_chair" / "wooden_chair.obj"
    real_asset.parent.mkdir(parents=True)
    real_asset.write_text("o chair\n")

    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path / "cache"
    obj.console_dialog = MagicMock()
    am = MagicMock()
    am.search_assets.return_value = [
        {
            "name": "wooden_chair",
            "type": "obj",
            "category": "makehuman",
            "file_path": str(real_asset),
        }
    ]
    obj.asset_manager = am

    program = [
        {
            "action": "import_asset_from_library",
            "params": {"query": "wooden chair", "location": [1, 0, 0]},
        }
    ]
    out = MainWindow._resolve_library_imports(obj, program)

    assert len(out) == 1
    assert out[0]["action"] == "import_mesh_file"
    assert out[0]["params"]["file_ext"] == "obj"
    assert out[0]["params"]["location"] == [1, 0, 0]


def test_resolve_library_imports_threads_name_and_scale(tmp_path):
    # name/scale/rotation on import_asset_from_library must survive into the
    # concrete import_mesh_file so the add-on can name + place the asset (and
    # it can then be referenced/duplicated by name, e.g. a tree row).
    real_asset = tmp_path / "store" / "oak" / "oak.glb"
    real_asset.parent.mkdir(parents=True)
    real_asset.write_bytes(b"glTF")

    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path / "cache"
    obj.console_dialog = MagicMock()
    am = MagicMock()
    am.search_assets.return_value = [
        {"name": "oak", "type": "glb", "category": "polyhaven", "file_path": str(real_asset)}
    ]
    obj.asset_manager = am

    program = [
        {
            "action": "import_asset_from_library",
            "params": {
                "query": "tree",
                "name": "AI_TreeL_0",
                "location": [-7, -500, 0],
                "scale": [1.5, 1.5, 1.5],
            },
        }
    ]
    out = MainWindow._resolve_library_imports(obj, program)
    assert out[0]["params"]["name"] == "AI_TreeL_0"
    assert out[0]["params"]["scale"] == [1.5, 1.5, 1.5]
    assert out[0]["params"]["location"] == [-7, -500, 0]


def test_resolve_library_imports_emits_tiled_for_tile_length(tmp_path):
    # tile_length on the library import → a concrete import_mesh_tiled action
    # (the add-on measures + repeats the segment), not a single import_mesh_file.
    road = tmp_path / "store" / "road" / "road.glb"
    road.parent.mkdir(parents=True)
    road.write_bytes(b"glTF")

    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path / "cache"
    obj.console_dialog = MagicMock()
    am = MagicMock()
    am.search_assets.return_value = [
        {"name": "road", "type": "glb", "category": "polyhaven", "file_path": str(road)}
    ]
    obj.asset_manager = am

    program = [
        {
            "action": "import_asset_from_library",
            "params": {
                "query": "road",
                "name": "AI_RoadSeg",
                "tile_length": 1000.0,
                "tile_axis": "y",
            },
        }
    ]
    out = MainWindow._resolve_library_imports(obj, program)
    assert out[0]["action"] == "import_mesh_tiled"
    assert out[0]["params"]["tile_length"] == 1000.0
    assert out[0]["params"]["tile_axis"] == "y"
    assert out[0]["params"]["name"] == "AI_RoadSeg"
    # Imported straight from the store, not a copy in the cache dir.
    assert out[0]["params"]["file_path"] == str(road)
    am.load_asset_to_disk.assert_not_called()


def test_resolve_library_imports_extracts_legacy_blob_row(tmp_path):
    # A legacy row with no on-disk file_path (a self-contained BLOB) is
    # extracted to the runtime cache instead.
    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path / "cache"
    obj.console_dialog = MagicMock()
    am = MagicMock()
    am.search_assets.return_value = [
        {"name": "old_chair", "type": "obj", "category": "legacy", "file_path": None}
    ]
    am.load_asset_to_disk.return_value = True
    obj.asset_manager = am

    program = [{"action": "import_asset_from_library", "params": {"query": "chair"}}]
    out = MainWindow._resolve_library_imports(obj, program)

    assert len(out) == 1
    assert out[0]["action"] == "import_mesh_file"
    assert out[0]["params"]["file_path"].endswith("old_chair.obj")
    am.load_asset_to_disk.assert_called_once()


def test_resolve_library_imports_passes_through_other_actions(tmp_path):
    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path
    obj.console_dialog = MagicMock()
    obj.asset_manager = MagicMock()

    program = [
        {"action": "add_primitive", "params": {"shape": "cube"}},
        {"action": "add_light", "params": {}},
    ]
    out = MainWindow._resolve_library_imports(obj, program)
    assert [a["action"] for a in out] == ["add_primitive", "add_light"]
    obj.asset_manager.search_assets.assert_not_called()


def test_resolve_library_imports_no_match_falls_back_to_placeholder(tmp_path):
    obj = types.SimpleNamespace()
    obj.runtime_cache_dir = tmp_path
    obj.console_dialog = MagicMock()
    am = MagicMock()
    am.search_assets.return_value = []  # nothing in the library matches
    obj.asset_manager = am

    program = [{"action": "import_asset_from_library", "params": {"query": "unicorn"}}]
    out = MainWindow._resolve_library_imports(obj, program)
    # The only action was unresolvable, so we still hand Blender a usable scene.
    names = [a["action"] for a in out]
    assert "add_primitive" in names
    assert "set_camera" in names
