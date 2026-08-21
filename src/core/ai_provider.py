"""
LLM client that turns a natural-language prompt into a whitelisted
Blender SCENE PROGRAM - an ordered list of composable primitive actions
(add a mesh, give it a material, add a light, place the camera, ...) that
together build the requested scene.

Design choice - read this before changing the schema: the model is asked
to return STRUCTURED JSON picking actions + params from ACTION_SCHEMA
below, never free-form Python. This is deliberate. The Blender add-on
(src/blender_addon/blender_rag_addon.py) executes whatever the desktop
app tells it to; if that were raw LLM-generated code, anything that can
influence the prompt (a compromised API response, a prompt-injection
payload embedded in ingested reference material, a bug in this client)
would translate directly into arbitrary code execution inside Blender.
By constraining the model to a CLOSED action vocabulary and validating
every action server-side (validate_scene_program / validate_scene_action),
the worst a bad response can do is name an unknown action, which is
dropped - it can never make Blender run something outside the fixed set
of pre-written handler functions. Widening the vocabulary (as was done
to turn this from a 3-action toy into a real generator) does NOT weaken
that guarantee: each new action is still a hand-written, parameter-clamped
Blender operation, and model output is still validated against this list.

Supports OpenAI-compatible chat-completions endpoints (OpenAI, Groq,
OpenRouter, and Ollama's /v1 compatibility layer all speak this same
shape) and Anthropic's Messages API. No third-party HTTP dependency -
uses urllib, consistent with the rest of this codebase.
"""

import json
import urllib.error
import urllib.request
from typing import Any

# Every action the AI (or the rule-based fallback) is allowed to emit.
# Each maps to a hand-written, parameter-clamped handler in the Blender
# add-on's ACTION_HANDLERS dict - keep the two in sync when adding an
# action. `params` values are human/LLM-readable descriptions, not code.
ACTION_SCHEMA: dict[str, dict[str, Any]] = {
    # --- Primitive geometry -------------------------------------------
    "add_primitive": {
        "description": "Add a primitive mesh object to the scene.",
        "params": {
            "shape": "one of: cube, sphere, cylinder, cone, torus, plane, circle, monkey",
            "name": "optional object name",
            "location": "optional [x, y, z] floats",
            "rotation": "optional [x, y, z] euler radians",
            "scale": "optional [x, y, z] floats (default [1,1,1])",
            "size": "optional float overall size (default 2.0)",
            "major_radius": "optional float, torus only: ring center radius",
            "minor_radius": "optional float, torus only: tube thickness (small = thin coat)",
        },
    },
    "add_material": {
        "description": "Create and assign a Principled BSDF material to an object.",
        "params": {
            "target": "optional object name (defaults to the most recently created object)",
            "base_color": "[r, g, b, a] floats 0-1",
            "metallic": "optional float 0-1",
            "roughness": "optional float 0-1",
            "emission_color": "optional [r, g, b, a] floats 0-1",
            "emission_strength": "optional float 0-50 (0 = no glow)",
        },
    },
    "apply_modifier": {
        "description": "Add a geometry modifier to an object.",
        "params": {
            "target": "optional object name (defaults to most recent object)",
            "modifier": "one of: SUBSURF, BEVEL, DISPLACE, SOLIDIFY, ARRAY, MIRROR, WIREFRAME",
            "levels": "optional int 0-4 (SUBSURF)",
            "width": "optional float (BEVEL width / SOLIDIFY thickness)",
            "strength": "optional float (DISPLACE strength)",
            "count": "optional int (ARRAY copies)",
            "axis": "optional one of x,y,z (MIRROR axis)",
        },
    },
    "transform_object": {
        "description": "Move, rotate, and/or scale an existing object.",
        "params": {
            "target": "optional object name (defaults to most recent object)",
            "location": "optional [x, y, z] absolute floats",
            "rotation": "optional [x, y, z] euler radians",
            "scale": "optional [x, y, z] floats",
        },
    },
    "duplicate_object": {
        "description": "Duplicate an object, optionally offset, to build repeated elements.",
        "params": {
            "target": "optional object name (defaults to most recent object)",
            "offset": "optional [x, y, z] floats to move the copy by",
            "name": "optional name for the copy",
            "linked": "optional bool: share the source mesh (a light instance) "
            "instead of a full copy - use when placing many copies of a "
            "heavy imported asset",
        },
    },
    "boolean_op": {
        "description": "Combine two objects with a boolean operation (the cutter is removed).",
        "params": {
            "target": "object name to modify",
            "cutter": "object name to use as the boolean tool",
            "operation": "one of: DIFFERENCE, UNION, INTERSECT",
        },
    },
    "add_text": {
        "description": "Add an extruded 3D text object.",
        "params": {
            "body": "the text string",
            "location": "optional [x, y, z] floats",
            "extrude": "optional float depth (default 0.1)",
            "size": "optional float (default 1.0)",
        },
    },
    # --- Lighting / world / camera ------------------------------------
    "add_light": {
        "description": "Add a light to the scene.",
        "params": {
            "light_type": "one of: POINT, SUN, AREA, SPOT",
            "location": "optional [x, y, z] floats",
            "energy": "optional float power (W)",
            "color": "optional [r, g, b] floats 0-1",
        },
    },
    "set_world_background": {
        "description": "Set the world background color and strength (ambient lighting).",
        "params": {
            "color": "[r, g, b] floats 0-1",
            "strength": "optional float 0-10 (default 1.0)",
        },
    },
    "set_camera": {
        "description": "Place a camera and aim it at a point (creates one if none exists).",
        "params": {
            "location": "optional [x, y, z] floats",
            "look_at": "optional [x, y, z] point the camera faces (default origin)",
            "lens": "optional float focal length mm (default 50)",
        },
    },
    # --- Asset library / external generation ---------------------------
    "import_asset_from_library": {
        "description": (
            "Search the user's ingested asset library (Poly Haven, Sketchfab, "
            "MakeHuman, etc.) for a 3D model matching a query and import the best "
            "match. Use this for real-world objects that primitives can't approximate "
            "(a chair, a car, a tree)."
        ),
        "params": {
            "query": "search terms describing the object to find, e.g. 'wooden chair'",
            "location": "optional [x, y, z] floats to place the imported asset",
            "name": "optional name to give the imported object (so it can be "
            "referenced later, e.g. duplicated down a row)",
            "scale": "optional [x, y, z] floats to scale the imported asset",
            "rotation": "optional [x, y, z] euler radians",
            "tile_length": "optional float: repeat the imported model end-to-end "
            "to fill this many metres (e.g. tile a road segment to 1 km) instead "
            "of importing a single copy",
            "tile_axis": "optional 'x'/'y'/'z' axis to tile along (default 'y')",
        },
    },
    # --- Legacy high-level actions (kept for back-compat) --------------
    "generate_terrain": {
        "description": "Create a procedurally displaced terrain grid with a colored material.",
        "params": {
            "primary_color": "array of 4 floats 0-1, [r, g, b, a]",
            "displace_strength": "float 0-10",
        },
    },
    "generate_character": {
        "description": "Create a placeholder humanoid character rig/mesh (uses MPFB/MB-Lab if installed).",
        "params": {"gender": "float, 0.0 = female .. 1.0 = male"},
    },
    "import_blendkit_asset": {
        "description": "Import a specific free BlendKit asset by its asset_base_id.",
        "params": {"asset_id": "BlendKit asset_base_id string"},
    },
}

# Actions the desktop app resolves/handles itself before anything reaches
# Blender (import_asset_from_library becomes a concrete file import). Still
# whitelisted like everything else - listed here only for documentation.
_SERVER_RESOLVED_ACTIONS = frozenset({"import_asset_from_library"})

_ACTION_NAMES = ", ".join(json.dumps(k) for k in ACTION_SCHEMA)

# Legacy single-action prompt (kept for request_scene_action / older callers).
SYSTEM_PROMPT = (
    "You control a Blender scene generator. Given the user's prompt, respond with ONLY a single JSON "
    'object of the form {"action": <action name>, "params": {...}}. The action MUST be exactly one of: '
    + _ACTION_NAMES
    + ". Do not include any explanation, markdown formatting, or code fences - JSON only, nothing else. "
    "Available actions and their parameters:\n" + json.dumps(ACTION_SCHEMA, indent=2)
)

# Primary prompt: ask for a full scene PROGRAM (ordered list of actions).
SYSTEM_PROMPT_PROGRAM = (
    "You are a Blender scene director. Turn the user's prompt into a SCENE PROGRAM: an ordered list of "
    "primitive actions that together build the described scene. Respond with ONLY a single JSON object of "
    'the form {"actions": [ {"action": <name>, "params": {...}}, ... ]}. Every action name MUST be '
    "exactly one of: " + _ACTION_NAMES + ".\n\n"
    "Guidelines:\n"
    "- Build the subject from MULTIPLE primitives so it's recognizable, not just one bare shape. Add the "
    "defining details a person would expect: a donut = a brown torus (dough) + a colored torus on top "
    "(icing) + several small bright cylinders/spheres scattered on top (sprinkles); a mug = a cylinder + a "
    "small torus handle; a snowman = stacked spheres + a cone nose. Give each part its own add_material.\n"
    '- Name parts you\'ll refer to again (e.g. "AI_Dough") and target them explicitly; add_material with no '
    "'target' applies to the object you just created.\n"
    "- Use import_asset_from_library only for complex real-world objects primitives can't approximate "
    "(a chair, a car, a tree).\n"
    "- Add at least one light and a camera framing the scene so a render isn't black.\n"
    "- Use as many actions as the scene needs (a detailed object is often 10-30 actions). Colors are RGBA "
    "floats 0-1. Place small detail parts at sensible offsets so they sit ON the main object, not inside it.\n"
    "- For organic / food / hand-made things, add realism: SUBSURF (levels 3) to smooth, a low-strength "
    "DISPLACE (0.04-0.08) so it isn't perfectly geometric, and coats/toppings should be THIN and hug the "
    "surface (for a torus, a small minor_radius) rather than looking like a second solid object.\n"
    "- For scenes / layouts (a road with branches and rows of trees, a fence, a street), lay the elements out "
    "along an axis at regular intervals using each primitive's 'location'. A long road is a thin, long-scaled "
    "cube; sidewalks are thinner cubes beside it; trees are a cone (foliage) over a thin cylinder (trunk) "
    "repeated every N metres. Emit a representative stretch (roughly 10-40 objects) rather than hundreds.\n"
    "- Output JSON only. No explanation, no markdown, no code fences.\n\n"
    'Worked example - prompt "a pink frosted donut with sprinkles" (dough tan; icing a thin coat that drips; '
    "many sprinkles laid flat, rotated ~pi/2 on X so they lie down):\n"
    '{"actions": ['
    '{"action": "add_primitive", "params": {"shape": "torus", "name": "AI_Dough", "major_radius": 1.0, "minor_radius": 0.46}}, '
    '{"action": "apply_modifier", "params": {"target": "AI_Dough", "modifier": "SUBSURF", "levels": 3}}, '
    '{"action": "apply_modifier", "params": {"target": "AI_Dough", "modifier": "DISPLACE", "strength": 0.06}}, '
    '{"action": "add_material", "params": {"target": "AI_Dough", "base_color": [0.72, 0.5, 0.3, 1]}}, '
    '{"action": "add_primitive", "params": {"shape": "torus", "name": "AI_Icing", "major_radius": 1.0, '
    '"minor_radius": 0.5, "location": [0, 0, 0.16], "scale": [1, 1, 0.8]}}, '
    '{"action": "apply_modifier", "params": {"target": "AI_Icing", "modifier": "DISPLACE", "strength": 0.07}}, '
    '{"action": "add_material", "params": {"target": "AI_Icing", "base_color": [0.95, 0.45, 0.7, 1], "roughness": 0.2}}, '
    '{"action": "add_primitive", "params": {"shape": "cylinder", "name": "AI_Sprinkle_1", '
    '"size": 0.14, "location": [1, 0, 0.5], "rotation": [1.57, 0, 0.5], "scale": [0.22, 0.22, 1]}}, '
    '{"action": "add_material", "params": {"target": "AI_Sprinkle_1", "base_color": [0.95, 0.85, 0.1, 1]}}, '
    '{"action": "add_light", "params": {"light_type": "AREA", "location": [4, -4, 6], "energy": 900}}, '
    '{"action": "set_camera", "params": {"location": [6, -6, 4.5], "look_at": [0, 0, 0]}}'
    "]}  (add ~20 more sprinkles at varied angles, radii and colors).\n\n"
    "Available actions and their parameters:\n" + json.dumps(ACTION_SCHEMA, indent=2)
)


class AIProviderError(RuntimeError):
    """Raised for any provider/network/parsing failure. Callers should catch this and fall back."""


# Python's urllib sends "Python-urllib/x.y" as its default User-Agent, which
# several providers' Cloudflare-fronted endpoints (Groq's included - see
# their community forum thread "Cloudflare Blocking Urllib.request without
# User-Agent") flag as a bot and reject outright with HTTP 403 / Cloudflare
# error 1010, before the request ever reaches the actual API. Always send a
# normal, app-identifying User-Agent so legitimate requests aren't mistaken
# for scraper traffic. Callers can still override it via their own headers
# dict, but nothing here does.
_DEFAULT_USER_AGENT = "LRJK-Blender-AI-Studio/1.0"


def _http_post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    headers = {"User-Agent": _DEFAULT_USER_AGENT, **headers}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise AIProviderError(f"HTTP {e.code} from provider: {body[:300]}") from e
    except urllib.error.URLError as e:
        raise AIProviderError(f"Could not reach provider endpoint: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise AIProviderError(f"Provider request timed out or failed: {e}") from e


def _is_reasoning_model(model: str) -> bool:
    """
    True for chat models that emit a hidden chain-of-thought before the
    visible answer, so we can tell them to reason less and budget more tokens.
    Matched by name substring since there's no capability flag in the API.
    """
    m = (model or "").lower()
    markers = (
        "gpt-oss",
        "-oss",
        "o1",
        "o3",
        "o4-mini",
        "o4mini",
        "reasoning",
        "deepseek-r",
        "deepseek-reasoner",
        "qwq",
        "-r1",
        "r1-",
    )
    return any(mk in m for mk in markers)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise AIProviderError(f"Provider did not return a JSON object: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise AIProviderError(f"Provider response was not valid JSON: {e}") from e


def validate_scene_action(candidate: dict[str, Any]) -> dict[str, Any]:
    """Ensures an action dict (AI-generated or rule-based) is well-formed and whitelisted."""
    if not isinstance(candidate, dict):
        raise AIProviderError("Action payload was not a JSON object.")
    action = candidate.get("action")
    params = candidate.get("params", {})
    if action not in ACTION_SCHEMA:
        raise AIProviderError(f"Unknown/disallowed action: {action!r}")
    if not isinstance(params, dict):
        raise AIProviderError("Action 'params' must be an object.")
    return {"action": action, "params": params}


def validate_scene_program(candidate: Any) -> list[dict[str, Any]]:
    """
    Normalizes and validates a whole scene program into a list of
    whitelisted {"action", "params"} dicts. Accepts any of:
      - {"actions": [ {...}, ... ]}   (the model's expected shape)
      - a bare list [ {...}, ... ]
      - a single {"action", "params"} dict (back-compat)
    Individual actions that fail validation (unknown name, bad params) are
    DROPPED rather than failing the whole program - a good scene shouldn't
    be discarded because the model tacked on one stray action. Raises
    AIProviderError only if nothing valid remains.
    """
    if isinstance(candidate, dict) and "actions" in candidate:
        items = candidate.get("actions")
    elif isinstance(candidate, dict) and "action" in candidate:
        items = [candidate]
    elif isinstance(candidate, list):
        items = candidate
    else:
        raise AIProviderError("Scene program was not a list of actions.")

    if not isinstance(items, list) or not items:
        raise AIProviderError("Scene program contained no actions.")

    validated: list[dict[str, Any]] = []
    for item in items:
        try:
            validated.append(validate_scene_action(item))
        except AIProviderError:
            continue  # drop the bad action, keep building the scene

    if not validated:
        raise AIProviderError("Scene program contained no valid whitelisted actions.")
    return validated


def _call_llm_for_json(
    prompt: str,
    system_prompt: str,
    provider: str,
    endpoint: str,
    api_key: str,
    model: str,
    timeout: float,
    max_tokens: int,
) -> dict[str, Any]:
    """
    Shared provider call used by both the single-action and full-program
    requests. Returns the parsed JSON object from the model's reply.
    Raises AIProviderError on any network/parse failure.
    """
    provider_lc = (provider or "").lower()
    endpoint = (endpoint or "").strip().rstrip("/")

    if not endpoint:
        raise AIProviderError("No API endpoint configured.")
    if not prompt or not prompt.strip():
        raise AIProviderError("Empty prompt.")

    if "claude" in provider_lc or "anthropic" in provider_lc:
        url = f"{endpoint}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model or "claude-3-5-sonnet-20240620",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = _http_post_json(url, headers, payload, timeout=timeout)
        content_blocks = data.get("content", [])
        text = "".join(block.get("text", "") for block in content_blocks if isinstance(block, dict))
    else:
        # OpenAI-compatible chat completions: OpenAI, Groq, OpenRouter, and
        # Ollama's /v1 compatibility layer, plus any custom endpoint that
        # follows the same request/response shape.
        url = f"{endpoint}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        base_payload = {
            "model": model or "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": max_tokens,
        }

        # --- Reasoning models (Groq's gpt-oss, OpenAI o1/o3/o4, DeepSeek-R1,
        #     Qwen QwQ, etc.) spend tokens on a hidden chain-of-thought BEFORE
        #     the visible answer. With a modest max_tokens a COMPLEX prompt can
        #     burn the whole budget on reasoning and come back with an EMPTY
        #     message.content (finish_reason "length") - which is exactly why a
        #     trivial "Test Connection" prompt succeeded while a real, detailed
        #     scene prompt returned ''. Two mitigations, both best-effort and
        #     safely stripped on any endpoint that rejects them (see retry):
        #       * reasoning_effort=low  -> curbs how much it "thinks".
        #       * response_format=json_object -> forces a clean JSON body.
        extras: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if _is_reasoning_model(model):
            extras["reasoning_effort"] = "low"

        def _do_post(with_extras: bool) -> dict[str, Any]:
            payload = dict(base_payload)
            if with_extras:
                payload.update(extras)
            return _http_post_json(url, headers, payload, timeout=timeout)

        try:
            data = _do_post(with_extras=True)
        except AIProviderError as e:
            # A picky/older endpoint may 400 on response_format or
            # reasoning_effort. Retry once with a plain request rather than
            # failing the whole generation over an optional field.
            msg = str(e).lower()
            if "http 4" in msg and (
                "response_format" in msg
                or "reasoning" in msg
                or "unsupported" in msg
                or "unknown" in msg
                or "invalid" in msg
                or "json" in msg
            ):
                data = _do_post(with_extras=False)
            else:
                raise

        choices = data.get("choices", [])
        if not choices:
            raise AIProviderError(f"Provider returned no choices: {json.dumps(data)[:300]}")
        message = choices[0].get("message", {}) or {}
        text = message.get("content", "") or ""
        # Some reasoning backends surface the answer in a side field when
        # content is blank; fall back to those before giving up.
        if not text.strip():
            text = message.get("reasoning_content") or message.get("reasoning") or ""
        if not (text or "").strip():
            finish = choices[0].get("finish_reason", "")
            if finish == "length":
                raise AIProviderError(
                    "Provider hit the token limit before returning any answer "
                    "(likely a reasoning model spending the whole budget on "
                    "hidden reasoning). Try a larger max_tokens or a lighter model."
                )
            raise AIProviderError(f"Provider returned an empty message (finish_reason={finish!r}).")

    return _extract_json_object(text)


def request_scene_action(
    prompt: str,
    provider: str,
    endpoint: str,
    api_key: str,
    model: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """
    Legacy single-action request. Returns one validated
    {"action": ..., "params": {...}} dict. Kept for back-compat; new code
    should prefer request_scene_program. Raises AIProviderError on failure.
    """
    result = _call_llm_for_json(
        prompt,
        SYSTEM_PROMPT,
        provider,
        endpoint,
        api_key,
        model,
        timeout=timeout,
        max_tokens=300,
    )
    return validate_scene_action(result)


def request_scene_program(
    prompt: str,
    provider: str,
    endpoint: str,
    api_key: str,
    model: str,
    timeout: float = 45.0,
) -> list[dict[str, Any]]:
    """
    Primary generation entrypoint. Asks the provider to turn the prompt
    into a full scene program and returns a validated, non-empty list of
    {"action": ..., "params": {...}} dicts. Raises AIProviderError on any
    failure - callers should catch this and fall back to the rule-based
    program generator rather than let the request crash the app.
    """
    result = _call_llm_for_json(
        prompt,
        SYSTEM_PROMPT_PROGRAM,
        provider,
        endpoint,
        api_key,
        model,
        timeout=timeout,
        max_tokens=4000,
    )
    return validate_scene_program(result)


def test_provider_connection(
    provider: str, endpoint: str, api_key: str, model: str
) -> tuple[bool, str]:
    """Makes one small real request end-to-end to verify endpoint/key/model actually work."""
    try:
        program = request_scene_program(
            "a single small blue cube, for verifying the studio's AI connection",
            provider,
            endpoint,
            api_key,
            model,
            timeout=15.0,
        )
        actions = ", ".join(a["action"] for a in program[:4])
        return (
            True,
            f"Connected successfully. Sample program: [{actions}] ({len(program)} action(s)).",
        )
    except AIProviderError as e:
        return False, str(e)
