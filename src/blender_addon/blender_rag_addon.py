bl_info = {
    "name": "LRJK AI Studio Bridge",
    "author": "LRJK / RK Offisium",
    "version": (3, 8, 0),
    "blender": (3, 0, 0),
    "location": "3D View > Sidebar > LRJK AI Studio",
    "description": "Blender Client Bridge with authenticated AI scene-program generation (composable primitives + asset-library import), Text-to-3D mesh generation, Auto-Render, and Auto-Rigging.",
    "category": "3D View",
}

import json
import os
import re
import sqlite3
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import bpy


# =====================================================================
# HELPER: DATABASE BINARY ASSET LOADER
# =====================================================================
def load_binary_asset_from_db(db_path: str, asset_name: str, file_ext: str) -> bool:
    """Extracts a binary asset from studio_memory.db directly into Blender scene."""
    if not os.path.exists(db_path):
        print(f"Database path missing: {db_path}")
        return False

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT file_data FROM binary_assets WHERE asset_name = ?", (asset_name,))
        row = cursor.fetchone()
        conn.close()

        if not row or not row[0]:
            return False

        temp_dir = Path(tempfile.gettempdir()) / "LRJK_Blender_Cache"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / f"{asset_name}.{file_ext}"

        with open(temp_file, "wb") as f:
            f.write(row[0])

        ext = file_ext.lower()
        if ext in ["gltf", "glb"]:
            bpy.ops.import_scene.gltf(filepath=str(temp_file))
        elif ext == "obj":
            bpy.ops.wm.obj_import(filepath=str(temp_file))
        elif ext == "fbx":
            bpy.ops.import_scene.fbx(filepath=str(temp_file))

        return True
    except Exception as e:
        print(f"Error loading asset from DB: {e}")
        return False


# =====================================================================
# HELPER: HTTP NETWORK BRIDGE SENDER
# =====================================================================
def send_to_studio_bridge(
    port: int, endpoint: str, payload: dict, token: str = "", timeout: float = 20.0
):
    """
    POSTs to the desktop app's local bridge. Sends the X-LRJK-Token header
    (required since the bridge now rejects unauthenticated/mismatched-token
    requests - see BridgeHTTPRequestHandler in src/ui/main_window.py) and
    an explicit application/json Content-Type.

    timeout defaults to 20s for normal fast round-trips (handshake, the
    rule-based/AI-provider scene actions). Pass a much longer value for
    slow operations - text-to-3D generation via Tripo3D can take up to
    ~180s, and the desktop app itself waits up to 240s server-side for
    that payload type (see main_window.py's do_POST), so a short client
    timeout here would abandon a job that's still succeeding server-side.
    """
    url = f"http://127.0.0.1:{port}/{endpoint}"
    data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={
            "Content-Type": "application/json",
            "X-LRJK-Token": token or "",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            return True, res_data.get("message", "Success"), res_data
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return (
                False,
                "Studio App rejected the request: Bridge Token missing/incorrect. Copy it from the desktop app's AI Settings dialog.",
                {},
            )
        return False, f"Studio App returned HTTP {e.code}: {e.reason}", {}
    except urllib.error.URLError as e:
        return False, f"Could not connect to Studio App on port {port}: {e.reason}", {}
    except Exception as e:
        return False, f"Bridge Error: {str(e)}", {}


# =====================================================================
# SCENE-GENERATION ACTION HANDLERS
#
# The desktop app used to send back a raw Python string that this add-on
# ran with exec(python_code, {"bpy": bpy}) - full Blender API access,
# driven by a response to an unauthenticated local HTTP request. That's a
# latent remote-code-execution pattern (see src/ui/main_window.py's
# BridgeHTTPRequestHandler docstring for the full reasoning), and it only
# gets riskier once the desktop app starts calling out to a real LLM
# (src/core/ai_provider.py) - model output should never be exec()'d
# directly. Instead, the desktop app now returns a small whitelisted
# {"action": <name>, "params": {...}} descriptor, and this add-on only
# ever runs one of the fixed handler functions below - it can never run
# code it didn't ship with. Keep ACTION_HANDLERS in sync with
# src/core/ai_provider.py's ACTION_SCHEMA if you add a new action.
# =====================================================================


def _clamp(value, lo, hi, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _clamp_rgba(value):
    default = (0.1, 0.4, 0.9, 1.0)
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return default
    r = _clamp(value[0], 0.0, 1.0, default[0])
    g = _clamp(value[1], 0.0, 1.0, default[1])
    b = _clamp(value[2], 0.0, 1.0, default[2])
    a = _clamp(value[3], 0.0, 1.0, default[3]) if len(value) > 3 else 1.0
    return (r, g, b, a)


def _clamp_rgb(value, default=(0.8, 0.8, 0.8)):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return tuple(default)
    return (
        _clamp(value[0], 0.0, 1.0, default[0]),
        _clamp(value[1], 0.0, 1.0, default[1]),
        _clamp(value[2], 0.0, 1.0, default[2]),
    )


def _vec3(value, default=(0.0, 0.0, 0.0), lo=-10000.0, hi=10000.0):
    """Coerces a param into a clamped 3-float tuple; falls back to default."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return tuple(default)
    return (
        _clamp(value[0], lo, hi, default[0]),
        _clamp(value[1], lo, hi, default[1]),
        _clamp(value[2], lo, hi, default[2]),
    )


def _resolve_object(name):
    """
    Finds a scene object by name; if no (or an unknown) name is given,
    falls back to the active object. This is what lets a multi-action
    scene program say add_material with no 'target' and have it apply to
    the primitive the previous action just created (add_primitive sets the
    active object), without the desktop app having to invent object names.
    """
    if isinstance(name, str) and name:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            return obj
    return bpy.context.view_layer.objects.active


def _new_material(
    name, base_color, metallic=0.0, roughness=0.5, emission_color=None, emission_strength=0.0
):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = base_color
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = metallic
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = roughness
        # Emission input names differ across Blender versions
        # ('Emission' vs 'Emission Color'); set whichever exists.
        if emission_color is not None and emission_strength > 0.0:
            for key in ("Emission Color", "Emission"):
                if key in bsdf.inputs:
                    bsdf.inputs[key].default_value = emission_color
                    break
            if "Emission Strength" in bsdf.inputs:
                bsdf.inputs["Emission Strength"].default_value = emission_strength
    return mat


# --- Composable primitive handlers -----------------------------------
# Each returns (bool_success, message). Params are clamped/validated; an
# unknown enum value falls back to a safe default rather than erroring.

_PRIMITIVE_OPS = {
    "cube": lambda size, loc: bpy.ops.mesh.primitive_cube_add(size=size, location=loc),
    "sphere": lambda size, loc: bpy.ops.mesh.primitive_uv_sphere_add(
        radius=size / 2.0, location=loc
    ),
    "cylinder": lambda size, loc: bpy.ops.mesh.primitive_cylinder_add(
        radius=size / 2.0, depth=size, location=loc
    ),
    "cone": lambda size, loc: bpy.ops.mesh.primitive_cone_add(
        radius1=size / 2.0, depth=size, location=loc
    ),
    "torus": lambda size, loc: bpy.ops.mesh.primitive_torus_add(
        major_radius=size / 2.0, minor_radius=size / 6.0, location=loc
    ),
    "plane": lambda size, loc: bpy.ops.mesh.primitive_plane_add(size=size, location=loc),
    "circle": lambda size, loc: bpy.ops.mesh.primitive_circle_add(
        radius=size / 2.0, fill_type="NGON", location=loc
    ),
    "monkey": lambda size, loc: bpy.ops.mesh.primitive_monkey_add(size=size, location=loc),
}


def action_add_primitive(params: dict):
    shape = str(params.get("shape", "cube")).lower().strip()
    size = _clamp(params.get("size"), 0.01, 100.0, 2.0)
    location = _vec3(params.get("location"), (0.0, 0.0, 0.0))

    bpy.ops.object.select_all(action="DESELECT")
    if shape == "torus":
        # Explicit tube control lets callers make e.g. a thin icing coat
        # (small minor_radius) vs a fat ring. Defaults reproduce the old
        # fixed size/2, size/6 proportions. minor is kept below major so
        # Blender doesn't reject a self-intersecting torus.
        major = _clamp(params.get("major_radius"), 0.01, 100.0, size / 2.0)
        minor = _clamp(params.get("minor_radius"), 0.005, 100.0, size / 6.0)
        minor = min(minor, major * 0.98)
        bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, location=location)
    else:
        _PRIMITIVE_OPS.get(shape, _PRIMITIVE_OPS["cube"])(size, location)
    obj = bpy.context.active_object
    if obj is None:
        return False, f"Failed to create primitive '{shape}'."

    name = params.get("name")
    if isinstance(name, str) and name.strip():
        obj.name = name.strip()[:60]

    rotation = params.get("rotation")
    if isinstance(rotation, (list, tuple)) and len(rotation) >= 3:
        obj.rotation_euler = _vec3(rotation, (0.0, 0.0, 0.0), lo=-100.0, hi=100.0)

    scale = params.get("scale")
    if isinstance(scale, (list, tuple)) and len(scale) >= 3:
        obj.scale = _vec3(scale, (1.0, 1.0, 1.0), lo=0.001, hi=1000.0)

    return True, f"Added {shape} '{obj.name}'."


def action_add_material(params: dict):
    obj = _resolve_object(params.get("target"))
    if obj is None or not hasattr(obj.data, "materials"):
        return False, "add_material: no target mesh object available."

    base_color = _clamp_rgba(params.get("base_color"))
    metallic = _clamp(params.get("metallic"), 0.0, 1.0, 0.0)
    roughness = _clamp(params.get("roughness"), 0.0, 1.0, 0.5)
    emission_color = (
        _clamp_rgba(params.get("emission_color")) if params.get("emission_color") else None
    )
    emission_strength = _clamp(params.get("emission_strength"), 0.0, 50.0, 0.0)

    mat = _new_material(
        f"AI_Mat_{obj.name}",
        base_color,
        metallic,
        roughness,
        emission_color,
        emission_strength,
    )
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return True, f"Applied material to '{obj.name}'."


def action_apply_modifier(params: dict):
    obj = _resolve_object(params.get("target"))
    if obj is None or not hasattr(obj, "modifiers"):
        return False, "apply_modifier: no target object available."

    mod_type = str(params.get("modifier", "SUBSURF")).upper().strip()
    valid = {"SUBSURF", "BEVEL", "DISPLACE", "SOLIDIFY", "ARRAY", "MIRROR", "WIREFRAME"}
    if mod_type not in valid:
        mod_type = "SUBSURF"

    try:
        mod = obj.modifiers.new(name=f"AI_{mod_type}", type=mod_type)
    except (TypeError, RuntimeError) as e:
        return False, f"apply_modifier: could not add {mod_type}: {e}"

    if mod_type == "SUBSURF":
        mod.levels = int(_clamp(params.get("levels"), 0, 4, 2))
        mod.render_levels = mod.levels
    elif mod_type == "BEVEL":
        mod.width = _clamp(params.get("width"), 0.0, 10.0, 0.1)
    elif mod_type == "DISPLACE":
        mod.strength = _clamp(params.get("strength"), -10.0, 10.0, 1.0)
        tex = bpy.data.textures.new(f"AI_Disp_{obj.name}", type="CLOUDS")
        mod.texture = tex
    elif mod_type == "SOLIDIFY":
        mod.thickness = _clamp(params.get("width"), 0.0, 10.0, 0.1)
    elif mod_type == "ARRAY":
        mod.count = int(_clamp(params.get("count"), 1, 100, 3))
    elif mod_type == "MIRROR":
        axis = str(params.get("axis", "x")).lower()
        mod.use_axis = (axis == "x", axis == "y", axis == "z")

    return True, f"Applied {mod_type} to '{obj.name}'."


def action_transform_object(params: dict):
    obj = _resolve_object(params.get("target"))
    if obj is None:
        return False, "transform_object: no target object available."

    if isinstance(params.get("location"), (list, tuple)):
        obj.location = _vec3(params.get("location"), tuple(obj.location))
    if isinstance(params.get("rotation"), (list, tuple)):
        obj.rotation_euler = _vec3(
            params.get("rotation"), tuple(obj.rotation_euler), lo=-100.0, hi=100.0
        )
    if isinstance(params.get("scale"), (list, tuple)):
        obj.scale = _vec3(params.get("scale"), tuple(obj.scale), lo=0.001, hi=1000.0)

    return True, f"Transformed '{obj.name}'."


def action_duplicate_object(params: dict):
    obj = _resolve_object(params.get("target"))
    if obj is None:
        return False, "duplicate_object: no target object available."

    copy = obj.copy()
    # linked=True shares the source mesh data (a lightweight instance) instead
    # of deep-copying it - essential when instancing a real imported asset (a
    # tree) hundreds of times down a road, so the scene stays light.
    linked = bool(params.get("linked"))
    if obj.data is not None and not linked:
        copy.data = obj.data.copy()
    bpy.context.collection.objects.link(copy)

    offset = _vec3(params.get("offset"), (2.0, 0.0, 0.0))
    copy.location = (
        obj.location[0] + offset[0],
        obj.location[1] + offset[1],
        obj.location[2] + offset[2],
    )

    name = params.get("name")
    if isinstance(name, str) and name.strip():
        copy.name = name.strip()[:60]

    bpy.ops.object.select_all(action="DESELECT")
    copy.select_set(True)
    bpy.context.view_layer.objects.active = copy
    return True, f"Duplicated '{obj.name}' -> '{copy.name}'."


def action_boolean_op(params: dict):
    target = _resolve_object(params.get("target"))
    cutter = (
        bpy.data.objects.get(params.get("cutter", ""))
        if isinstance(params.get("cutter"), str)
        else None
    )
    if target is None or cutter is None:
        return False, "boolean_op: needs both a valid 'target' and 'cutter' object name."

    operation = str(params.get("operation", "DIFFERENCE")).upper()
    if operation not in {"DIFFERENCE", "UNION", "INTERSECT"}:
        operation = "DIFFERENCE"

    mod = target.modifiers.new(name="AI_Boolean", type="BOOLEAN")
    mod.operation = operation
    mod.object = cutter
    bpy.context.view_layer.objects.active = target
    try:
        bpy.ops.object.modifier_apply(modifier=mod.name)
    except RuntimeError as e:
        return False, f"boolean_op failed to apply: {e}"

    # Remove the cutter now that it's been consumed.
    bpy.data.objects.remove(cutter, do_unlink=True)
    return True, f"Applied {operation} boolean to '{target.name}'."


def action_add_text(params: dict):
    body = params.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return False, "add_text: 'body' text is required."

    location = _vec3(params.get("location"), (0.0, 0.0, 0.0))
    bpy.ops.object.text_add(location=location)
    obj = bpy.context.active_object
    obj.data.body = body[:200]
    obj.data.extrude = _clamp(params.get("extrude"), 0.0, 5.0, 0.1)
    obj.data.size = _clamp(params.get("size"), 0.01, 100.0, 1.0)
    return True, f"Added 3D text '{body[:30]}'."


def action_add_light(params: dict):
    light_type = str(params.get("light_type", "POINT")).upper()
    if light_type not in {"POINT", "SUN", "AREA", "SPOT"}:
        light_type = "POINT"
    location = _vec3(params.get("location"), (4.0, -4.0, 6.0))

    bpy.ops.object.light_add(type=light_type, location=location)
    light_obj = bpy.context.active_object
    light_obj.data.energy = _clamp(
        params.get("energy"), 0.0, 1000000.0, 1000.0 if light_type == "POINT" else 5.0
    )
    light_obj.data.color = _clamp_rgb(params.get("color"), (1.0, 1.0, 1.0))
    return True, f"Added {light_type} light."


def action_set_world_background(params: dict):
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("AI_World")
        bpy.context.scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is None:
        return False, "set_world_background: no Background node available."
    color = _clamp_rgb(params.get("color"), (0.05, 0.05, 0.05))
    bg.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
    bg.inputs["Strength"].default_value = _clamp(params.get("strength"), 0.0, 10.0, 1.0)
    return True, "Set world background."


def action_set_camera(params: dict):
    from mathutils import Vector

    location = _vec3(params.get("location"), (7.0, -7.0, 5.0))
    look_at = _vec3(params.get("look_at"), (0.0, 0.0, 0.0))

    cam_obj = bpy.context.scene.camera
    if cam_obj is None:
        cam_data = bpy.data.cameras.new("AI_Camera")
        cam_obj = bpy.data.objects.new("AI_Camera", cam_data)
        bpy.context.collection.objects.link(cam_obj)
        bpy.context.scene.camera = cam_obj

    cam_obj.location = location
    cam_obj.data.lens = _clamp(params.get("lens"), 1.0, 250.0, 50.0)

    # Point the camera at look_at.
    direction = Vector(look_at) - Vector(location)
    if direction.length > 0:
        cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()

    return True, "Placed camera."


def action_generate_terrain(params: dict):
    primary_color = _clamp_rgba(params.get("primary_color"))
    displace_strength = _clamp(params.get("displace_strength"), 0.0, 10.0, 1.5)

    bpy.ops.object.select_all(action="DESELECT")
    bpy.ops.mesh.primitive_grid_add(
        x_subdivisions=64, y_subdivisions=64, size=10, location=(0, 0, 0)
    )
    terrain = bpy.context.active_object
    terrain.name = "AI_Generated_Terrain"

    mat = bpy.data.materials.new(name="Prompt_Terrain_Material")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = primary_color
    terrain.data.materials.append(mat)

    mod = terrain.modifiers.new(name="ProceduralDisplace", type="DISPLACE")
    texture = bpy.data.textures.new("ProceduralClouds", type="CLOUDS")
    mod.texture = texture
    mod.strength = displace_strength

    return True, "Generated procedural terrain."


def action_generate_character(params: dict):
    gender_raw = _clamp(params.get("gender"), 0.0, 1.0, 0.0)
    gender = 0.0 if gender_raw < 0.5 else 1.0

    if hasattr(bpy.ops, "mpfb") and hasattr(bpy.ops.mpfb, "create_human"):
        bpy.ops.mpfb.create_human(gender=gender, age=0.25)
    elif hasattr(bpy.ops, "mb_lab"):
        bpy.ops.mb_lab.init_character()
    else:
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.75, location=(0, 0, 1.4))
        head = bpy.context.active_object
        head.name = "Female_Head" if gender == 0.0 else "Male_Head"

    return True, "Generated character placeholder."


def action_import_blendkit_asset(params: dict):
    asset_id = params.get("asset_id", "")
    if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9\-]{8,64}", asset_id):
        return False, f"Rejected invalid BlendKit asset id: {asset_id!r}"

    # NOTE: hasattr(bpy.ops.blendkit, 'download_asset') is NOT a reliable
    # "is the add-on installed" check - bpy.ops.<category> attribute access
    # always succeeds in Blender regardless of whether anything is actually
    # registered there, so the old hasattr()-only check here always passed
    # even with BlendKit not installed, and only failed at call time with
    # an opaque "Calling operator ... error, could not be found" RuntimeError.
    # Check the add-on is actually enabled first, and still guard the call
    # itself as a backstop against other BlendKit-side failures.
    if "blendkit" not in bpy.context.preferences.addons.keys():
        return False, (
            "The BlendKit add-on is not installed/enabled in this Blender "
            '(Edit > Preferences > Add-ons > search "BlendKit").'
        )

    try:
        bpy.ops.blendkit.download_asset(asset_base_id=asset_id)
    except RuntimeError as e:
        return False, f"BlendKit failed to download asset {asset_id}: {e}"

    return True, f"Imported BlendKit asset {asset_id}."


_MESH_IMPORT_EXTENSIONS = {"glb", "gltf", "obj", "fbx"}


def action_import_mesh_file(params: dict):
    """
    Imports a mesh file the desktop app already downloaded to local disk
    (see src/core/text_to_3d.py - the Tripo3D result lands in the
    desktop app's runtime cache before this action ever runs). This
    add-on still only ever opens a local file path and only for a
    whitelisted set of extensions - it never fetches a URL or runs
    anything - keeping the same "never exec() what the network sends"
    guarantee as the other action handlers above.
    """
    file_path = params.get("file_path", "")
    file_ext = str(params.get("file_ext", "")).lower().lstrip(".")

    if not isinstance(file_path, str) or not file_path:
        return False, "No file_path provided for the generated mesh."
    if file_ext not in _MESH_IMPORT_EXTENSIONS:
        return False, f"Rejected unsupported mesh file type: '.{file_ext}'"
    if not os.path.isfile(file_path):
        return False, f"Generated mesh file not found on disk: {file_path}"

    ok, result = _import_and_merge(file_path, file_ext)
    if not ok:
        return False, result
    target = result

    if target is not None:
        name = params.get("name")
        if isinstance(name, str) and name.strip():
            target.name = name.strip()[:60]
        if isinstance(params.get("location"), (list, tuple)):
            target.location = _vec3(params.get("location"), tuple(target.location))
        if isinstance(params.get("rotation"), (list, tuple)):
            target.rotation_euler = _vec3(
                params.get("rotation"), tuple(target.rotation_euler), lo=-100.0, hi=100.0
            )
        if isinstance(params.get("scale"), (list, tuple)):
            target.scale = _vec3(params.get("scale"), tuple(target.scale), lo=0.001, hi=1000.0)
        bpy.ops.object.select_all(action="DESELECT")
        target.select_set(True)
        bpy.context.view_layer.objects.active = target

    return True, f"Imported mesh: {os.path.basename(file_path)}"


def _import_and_merge(file_path: str, file_ext: str):
    """
    Import a mesh file and collapse whatever the importer produced (often
    several mesh parts, plus empties for a glTF hierarchy) into ONE mesh
    object, returned as (True, obj). On failure returns (False, message).

    Merging matters because an imported asset otherwise lands at the file's
    own origin as a loose pile of parts - impossible to name, place, or
    cheaply instance (a tree down a road, a road segment tiled to length).
    """
    before = set(bpy.data.objects)
    try:
        if file_ext in ("glb", "gltf"):
            bpy.ops.import_scene.gltf(filepath=file_path)
        elif file_ext == "obj":
            bpy.ops.wm.obj_import(filepath=file_path)
        elif file_ext == "fbx":
            bpy.ops.import_scene.fbx(filepath=file_path)
    except Exception as e:
        return False, f"Blender failed to import the mesh: {e}"

    new_objs = [o for o in bpy.data.objects if o not in before]
    mesh_objs = [o for o in new_objs if o.type == "MESH"]
    if mesh_objs:
        bpy.ops.object.select_all(action="DESELECT")
        for o in mesh_objs:
            o.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objs[0]
        if len(mesh_objs) > 1:
            try:
                bpy.ops.object.join()
            except Exception:
                pass
        return True, bpy.context.view_layer.objects.active
    if new_objs:
        return True, new_objs[0]
    return False, "Import produced no objects."


def action_import_mesh_tiled(params: dict):
    """
    Import a mesh (e.g. a real road segment from the asset library) ONCE, then
    lay linked copies of it end-to-end along an axis to fill a target length -
    the dimension-agnostic way to build a 1 km road out of a 20 m model without
    stretching (which would smear its texture / lane lines). The add-on does
    the tiling because only it can measure the imported segment's true size
    after import.
    """
    file_path = params.get("file_path", "")
    file_ext = str(params.get("file_ext", "")).lower().lstrip(".")
    if not isinstance(file_path, str) or not file_path:
        return False, "import_mesh_tiled: no file_path provided."
    if file_ext not in _MESH_IMPORT_EXTENSIONS:
        return False, f"import_mesh_tiled: unsupported type '.{file_ext}'"
    if not os.path.isfile(file_path):
        return False, f"import_mesh_tiled: file not found: {file_path}"

    ok, result = _import_and_merge(file_path, file_ext)
    if not ok:
        return False, result
    base = result

    name = params.get("name")
    base_name = name.strip()[:56] if isinstance(name, str) and name.strip() else "AI_TiledMesh"
    base.name = f"{base_name}_0"

    if isinstance(params.get("scale"), (list, tuple)):
        base.scale = _vec3(params.get("scale"), tuple(base.scale), lo=0.001, hi=1000.0)
        bpy.context.view_layer.update()  # so .dimensions reflects the new scale

    axis = str(params.get("tile_axis", "y")).lower().strip()
    ai = {"x": 0, "y": 1, "z": 2}.get(axis, 1)
    target_len = _clamp(params.get("tile_length"), 0.0, 100000.0, 0.0)
    seg_len = float(base.dimensions[ai])

    loc = _vec3(params.get("location"), (0.0, 0.0, 0.0))

    # Not enough info to tile (no length, or a degenerate segment) -> place one.
    if target_len < 0.01 or seg_len < 0.01:
        base.location = loc
        bpy.ops.object.select_all(action="DESELECT")
        base.select_set(True)
        bpy.context.view_layer.objects.active = base
        return True, f"Imported road model '{base.name}' (not tiled)."

    n = max(1, int(round(target_len / seg_len)))
    n = min(n, 600)  # safety cap on runaway counts

    # Lay the row centred on `loc` along the axis: segment i sits at
    # start + i*seg_len, where start puts the whole run symmetric about loc.
    start = loc[ai] - (n * seg_len) / 2.0 + seg_len / 2.0
    first = list(loc)
    first[ai] = start
    base.location = first

    for i in range(1, n):
        copy = base.copy()  # linked (shared mesh) - cheap
        bpy.context.collection.objects.link(copy)
        cloc = list(first)
        cloc[ai] = start + i * seg_len
        copy.location = cloc
        copy.name = f"{base_name}_{i}"

    return True, f"Tiled road model into {n} segment(s) of {seg_len:.1f} m."


ACTION_HANDLERS = {
    # Composable primitives (the real generator - keep in sync with
    # src/core/ai_provider.py's ACTION_SCHEMA).
    "add_primitive": action_add_primitive,
    "add_material": action_add_material,
    "apply_modifier": action_apply_modifier,
    "transform_object": action_transform_object,
    "duplicate_object": action_duplicate_object,
    "boolean_op": action_boolean_op,
    "add_text": action_add_text,
    "add_light": action_add_light,
    "set_world_background": action_set_world_background,
    "set_camera": action_set_camera,
    # High-level / import actions.
    "generate_terrain": action_generate_terrain,
    "generate_character": action_generate_character,
    "import_blendkit_asset": action_import_blendkit_asset,
    "import_mesh_file": action_import_mesh_file,
    "import_mesh_tiled": action_import_mesh_tiled,
    # NOTE: import_asset_from_library is resolved by the desktop app into a
    # concrete import_mesh_file / import_mesh_tiled before it ever reaches this
    # add-on, so it has no handler here by design.
}


def _normalize_actions(res: dict):
    """
    Accepts the bridge response and returns an ordered list of
    {"action", "params"} dicts, supporting both the new multi-action
    scene-program shape ({"actions": [...]}) and the legacy single-action
    shape ({"action": ..., "params": ...}).
    """
    actions = res.get("actions")
    if isinstance(actions, list) and actions:
        out = []
        for item in actions:
            if isinstance(item, dict) and item.get("action"):
                out.append({"action": item.get("action"), "params": item.get("params") or {}})
        return out
    if res.get("action"):
        return [{"action": res.get("action"), "params": res.get("params") or {}}]
    return []


def run_scene_program(res: dict):
    """
    Runs a whole scene program (list of whitelisted actions) in order.
    Each handler is a pre-written function keyed by action name - unknown
    names are skipped, never executed. Returns
    (ok_count, total_count, messages) so the operator can report a summary.
    """
    actions = _normalize_actions(res)
    ok_count = 0
    messages = []
    for entry in actions:
        name = entry["action"]
        handler = ACTION_HANDLERS.get(name)
        if handler is None:
            messages.append(f"skipped unknown action '{name}'")
            continue
        try:
            ok, action_msg = handler(entry["params"])
        except Exception as e:  # a bad single action must not abort the rest
            messages.append(f"'{name}' errored: {e}")
            continue
        if ok:
            ok_count += 1
        messages.append(action_msg)
    return ok_count, len(actions), messages


# =====================================================================
# PERSISTENT ADD-ON PREFERENCES
#
# The Bridge Token + Port live here (AddonPreferences) rather than only on
# the Scene, so they PERSIST across .blend files, Blender restarts, and
# add-on updates/re-enables - you paste the token once and it's remembered.
# (Scene properties reset every time the add-on is reloaded, which is why
# the token kept disappearing after each update.)
# =====================================================================
class LRJKAddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    bridge_token: bpy.props.StringProperty(
        name="Bridge Token",
        description="Pairing token from the desktop app's AI Settings dialog. Saved globally.",
        default="",
        subtype="PASSWORD",
    )
    studio_port: bpy.props.IntProperty(
        name="Bridge Port",
        description="Port the LRJK Blender AI Studio desktop app listens on",
        default=8081,
        min=1024,
        max=65535,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="These are saved globally and survive add-on updates.")
        layout.prop(self, "bridge_token")
        layout.prop(self, "studio_port")


def _get_prefs():
    """The persistent add-on preferences, or None if unavailable (e.g. run
    as a loose script rather than an installed add-on)."""
    try:
        return bpy.context.preferences.addons[__name__].preferences
    except (KeyError, AttributeError):
        return None


def _conn(context):
    """Resolve (port, token) for talking to the desktop app, preferring the
    persistent preferences and falling back to the per-scene fields."""
    props = context.scene.lrjk_studio_props
    prefs = _get_prefs()
    token = ""
    port = props.studio_port
    if prefs is not None:
        token = (prefs.bridge_token or "").strip()
        port = prefs.studio_port
    if not token:
        token = (props.bridge_token or "").strip()
    return port, token


# =====================================================================
# PROPERTIES
# =====================================================================
class LRJKStudioProperties(bpy.types.PropertyGroup):
    prompt_input: bpy.props.StringProperty(
        name="Prompt",
        description="Describe what you want the AI Studio to generate",
        default="Create a vibrant blue and golden donut",
    )

    studio_port: bpy.props.IntProperty(
        name="Bridge Port",
        description="Port running LRJK Blender AI Studio desktop app",
        default=8081,
        min=1024,
        max=65535,
    )

    bridge_token: bpy.props.StringProperty(
        name="Bridge Token",
        description=(
            "Pairing token from the desktop app's AI Settings dialog "
            "(Blender Bridge Pairing Token box). Required - requests without "
            "a matching token are rejected."
        ),
        default="",
    )

    mesh_prompt: bpy.props.StringProperty(
        name="Mesh Prompt",
        description="Describe the 3D object to generate as an actual mesh via Tripo3D text-to-3D",
        default="a low-poly treasure chest",
    )

    auto_render: bpy.props.BoolProperty(
        name="Auto-Render Output",
        description="Automatically trigger camera render after applying materials and geometry",
        default=True,
    )

    render_output_path: bpy.props.StringProperty(
        name="Render Directory",
        description="Path to save rendered image files",
        subtype="DIR_PATH",
        default="//renders/",
    )

    color_brightness: bpy.props.FloatProperty(
        name="Brightness",
        description="Adjust image brightness post-render",
        default=0.0,
        min=-1.0,
        max=1.0,
        update=lambda self, context: LRJK_OT_ApplyColorAdjustments.update_colors(context),
    )

    color_contrast: bpy.props.FloatProperty(
        name="Contrast",
        description="Adjust image contrast post-render",
        default=1.0,
        min=0.0,
        max=3.0,
        update=lambda self, context: LRJK_OT_ApplyColorAdjustments.update_colors(context),
    )


# =====================================================================
# OPERATORS
# =====================================================================
class LRJK_OT_OpenStudio(bpy.types.Operator):
    bl_idname = "lrjk.open_studio"
    bl_label = "Connect Studio Bridge"

    def execute(self, context):
        port, token = _conn(context)
        success, msg, _ = send_to_studio_bridge(
            port,
            "ping",
            {"status": "handshake", "client": "Blender"},
            token=token,
        )
        if success:
            self.report({"INFO"}, f"Connected to Studio App on Port {port}!")
        else:
            self.report({"WARNING"}, msg)
        return {"FINISHED"}


class LRJK_OT_GenerateAsset(bpy.types.Operator):
    bl_idname = "lrjk.generate_asset"
    bl_label = "🚀 Generate AI Asset"

    def execute(self, context):
        props = context.scene.lrjk_studio_props
        prompt = props.prompt_input.strip()
        port, token = _conn(context)

        if not prompt:
            self.report({"WARNING"}, "Please enter a prompt before generating.")
            return {"CANCELLED"}

        if not token:
            self.report(
                {"WARNING"},
                "Set the Bridge Token first (copy it from the desktop app's AI Settings dialog).",
            )
            return {"CANCELLED"}

        payload = {
            "type": "generate_prompt",
            "prompt": prompt,
            "blend_version": bpy.app.version_string,
        }

        self.report({"INFO"}, f"Sending Prompt to AI Engine: '{prompt}'...")
        success, msg, res = send_to_studio_bridge(port, "generate", payload, token=token)

        if not success:
            self.report({"ERROR"}, msg)
            return {"FINISHED"}

        # The bridge always answers with HTTP 200 for a request it actually
        # processed, even if that processing itself failed (e.g. the AI
        # provider errored) - "success" above only means the HTTP round
        # trip worked. Check the JSON body's own status before assuming
        # there's an action to run, or a genuine failure gets misreported
        # as "unknown/unsupported action (None)" instead of its real cause.
        if res.get("status") == "error":
            self.report({"ERROR"}, msg)
            return {"FINISHED"}

        # The studio now returns a whole scene PROGRAM (an ordered list of
        # whitelisted actions) rather than a single action - build it all.
        ok_count, total, messages = run_scene_program(res)

        if total == 0:
            self.report({"WARNING"}, f"Studio returned no actions to run. Message: {msg}")
            return {"FINISHED"}

        summary = f"Built scene: {ok_count}/{total} actions succeeded."
        if ok_count == 0:
            self.report({"WARNING"}, f"{summary} Details: {'; '.join(messages)[:400]}")
            return {"FINISHED"}
        self.report({"INFO"}, summary)

        if props.auto_render:
            try:
                out_dir = bpy.path.abspath(props.render_output_path)
                os.makedirs(out_dir, exist_ok=True)
                save_path = os.path.join(out_dir, "AI_Render_Output.png")

                context.scene.render.filepath = save_path
                bpy.ops.render.render(write_still=True)
                self.report({"INFO"}, f"Auto-Render Saved to: {save_path}")
            except Exception as e:
                self.report({"WARNING"}, f"Generated the asset, but auto-render failed: {e}")

        return {"FINISHED"}


class LRJK_OT_GenerateMeshFromText(bpy.types.Operator):
    """
    Separate, dedicated button for actual text-to-3D mesh generation via
    Tripo3D - deliberately kept apart from "Generate AI Asset" above.
    That button asks an AI provider (or the rule-based fallback) to pick
    one of a few whitelisted scene actions; this one triggers a real,
    potentially credit-consuming external generation job, so it should
    only ever run when the user explicitly clicks this button.
    """

    bl_idname = "lrjk.generate_mesh_from_text"
    bl_label = "🧊 Generate 3D Mesh from Text"

    def execute(self, context):
        props = context.scene.lrjk_studio_props
        prompt = props.mesh_prompt.strip()
        port, token = _conn(context)

        if not prompt:
            self.report(
                {"WARNING"}, "Please describe the object to generate before clicking this button."
            )
            return {"CANCELLED"}

        if not token:
            self.report(
                {"WARNING"},
                "Set the Bridge Token first (copy it from the desktop app's AI Settings dialog).",
            )
            return {"CANCELLED"}

        payload = {
            "type": "generate_mesh_from_text",
            "prompt": prompt,
            "blend_version": bpy.app.version_string,
        }

        self.report(
            {"INFO"},
            f"Requesting 3D mesh from Tripo3D: '{prompt}' - this can take up to ~3 minutes...",
        )

        # Longer client-side timeout than the default 20s: Tripo3D
        # generation can take up to ~180s, and the desktop app itself
        # waits up to 240s server-side for this specific payload type
        # (see main_window.py's do_POST wait_timeout).
        success, msg, res = send_to_studio_bridge(
            port, "generate", payload, token=token, timeout=250.0
        )

        if not success:
            self.report({"ERROR"}, msg)
            return {"FINISHED"}

        # See the matching comment in LRJK_OT_GenerateAsset - HTTP 200 only
        # means the bridge round trip worked, not that generation itself
        # succeeded (e.g. Tripo3D can reject the job for insufficient
        # credit). Check the JSON body's status before assuming there's an
        # action to run.
        if res.get("status") == "error":
            self.report({"ERROR"}, msg)
            return {"FINISHED"}

        action = res.get("action")
        params = res.get("params", {}) or {}
        handler = ACTION_HANDLERS.get(action)

        if not handler:
            self.report(
                {"WARNING"},
                f"Studio returned an unknown/unsupported action ({action!r}). Message: {msg}",
            )
            return {"FINISHED"}

        try:
            ok, action_msg = handler(params)
        except Exception as e:
            self.report({"ERROR"}, f"Importing the generated mesh failed: {e}")
            return {"FINISHED"}

        if ok:
            self.report({"INFO"}, action_msg)
        else:
            self.report({"WARNING"}, action_msg)

        return {"FINISHED"}


class LRJK_OT_ApplyColorAdjustments(bpy.types.Operator):
    bl_idname = "lrjk.apply_color_adjustments"
    bl_label = "🎨 Apply Color Adjustments"

    @staticmethod
    def update_colors(context):
        props = context.scene.lrjk_studio_props
        scene = context.scene
        scene.view_settings.use_curve_mapping = True
        scene.view_settings.exposure = props.color_brightness
        scene.view_settings.gamma = max(0.1, 2.0 - props.color_contrast)

    def execute(self, context):
        self.update_colors(context)
        self.report({"INFO"}, "Applied Post-Render Color Adjustments.")
        return {"FINISHED"}


class LRJK_OT_AutoRigSelected(bpy.types.Operator):
    bl_idname = "lrjk.auto_rig_selected"
    bl_label = "🦴 Auto-Rig Selected Object"

    def execute(self, context):
        selected_objs = [obj for obj in context.selected_objects if obj.type == "MESH"]

        if not selected_objs:
            self.report({"WARNING"}, "Please select a 3D Mesh object to rig.")
            return {"CANCELLED"}

        mesh_obj = selected_objs[0]
        context.view_layer.objects.active = mesh_obj

        bbox = [
            mesh_obj.matrix_world @ bpy.mathutils.Vector(corner) for corner in mesh_obj.bound_box
        ]
        min_z = min(v.z for v in bbox)
        max_z = max(v.z for v in bbox)
        center_x = sum(v.x for v in bbox) / 8.0
        center_y = sum(v.y for v in bbox) / 8.0
        height = max_z - min_z

        arm_data = bpy.data.armatures.new(f"{mesh_obj.name}_RigData")
        arm_obj = bpy.data.objects.new(f"{mesh_obj.name}_Rig", arm_data)
        context.collection.objects.link(arm_obj)

        context.view_layer.objects.active = arm_obj
        bpy.ops.object.mode_set(mode="EDIT")

        bone_root = arm_data.edit_bones.new("Root")
        bone_root.head = (center_x, center_y, min_z)
        bone_root.tail = (center_x, center_y, min_z + (height * 0.5))

        bone_top = arm_data.edit_bones.new("Spine_Top")
        bone_top.head = (center_x, center_y, min_z + (height * 0.5))
        bone_top.tail = (center_x, center_y, max_z)
        bone_top.parent = bone_root

        bpy.ops.object.mode_set(mode="OBJECT")

        bpy.ops.object.select_all(action="DESELECT")
        mesh_obj.select_set(True)
        arm_obj.select_set(True)
        context.view_layer.objects.active = arm_obj

        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        self.report(
            {"INFO"}, f"Successfully rigged '{mesh_obj.name}' with Armature '{arm_obj.name}'!"
        )

        return {"FINISHED"}


# =====================================================================
# UI PANEL
# =====================================================================
class LRJK_PT_StudioPanel(bpy.types.Panel):
    bl_label = "LRJK AI Studio"
    bl_idname = "LRJK_PT_studio_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "LRJK AI Studio"

    def draw(self, context):
        layout = self.layout
        props = context.scene.lrjk_studio_props

        box = layout.box()
        box.label(text="Studio Bridge Connection", icon="LINKED")
        prefs = _get_prefs()
        if prefs is not None:
            # Persistent fields (survive add-on updates / restarts).
            row = box.row()
            row.prop(prefs, "studio_port", text="Port")
            box.prop(prefs, "bridge_token", text="Bridge Token")
            box.label(text="Saved globally - set once.", icon="CHECKMARK")
        else:
            row = box.row()
            row.prop(props, "studio_port", text="Port")
            box.prop(props, "bridge_token", text="Bridge Token")
        box.operator("lrjk.open_studio", text="Connect", icon="URL")

        layout.separator()

        gen_box = layout.box()
        gen_box.label(text="AI Generator & Auto-Render", icon="CONSOLE")
        gen_box.prop(props, "prompt_input", text="")
        gen_box.prop(props, "auto_render", text="Auto-Render Output")
        if props.auto_render:
            gen_box.prop(props, "render_output_path", text="Out Path")
        gen_box.operator("lrjk.generate_asset", icon="PLAY")

        layout.separator()

        mesh_box = layout.box()
        mesh_box.label(text="🧊 Text-to-3D Mesh Generation (Tripo3D)", icon="MESH_ICOSPHERE")
        mesh_box.prop(props, "mesh_prompt", text="")
        mesh_box.label(text="Can take up to ~3 minutes to generate.", icon="INFO")
        mesh_box.operator("lrjk.generate_mesh_from_text", icon="PLAY")

        layout.separator()

        color_box = layout.box()
        color_box.label(text="🎨 Post-Render Color Tuning", icon="COLOR")
        color_box.prop(props, "color_brightness", slider=True)
        color_box.prop(props, "color_contrast", slider=True)

        layout.separator()

        rig_box = layout.box()
        rig_box.label(text="🦴 Auto-Rigging Engine", icon="ARMATURE_DATA")
        rig_box.operator("lrjk.auto_rig_selected", icon="BONE_DATA")


# =====================================================================
# REGISTRATION
# =====================================================================
classes = (
    LRJKAddonPreferences,
    LRJKStudioProperties,
    LRJK_OT_OpenStudio,
    LRJK_OT_GenerateAsset,
    LRJK_OT_GenerateMeshFromText,
    LRJK_OT_ApplyColorAdjustments,
    LRJK_OT_AutoRigSelected,
    LRJK_PT_StudioPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.lrjk_studio_props = bpy.props.PointerProperty(type=LRJKStudioProperties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.lrjk_studio_props


if __name__ == "__main__":
    register()
