"""
Tripo3D text-to-3D client.

This is a deliberately separate, narrow module from src/core/ai_provider.py.
That file asks an LLM to pick from a small whitelisted *scene action*
(generate_terrain / generate_character / import_blendkit_asset) - the
model never returns a mesh, just a descriptor the add-on already knows
how to run.

This module is different: it calls Tripo3D's REST API
(https://developers.tripo3d.ai) to actually generate a new 3D mesh from
a text prompt, poll until the job finishes, and download the result to
a local file. It is triggered by its own dedicated "Generate 3D Mesh"
button in the Blender panel (see blender_rag_addon.py) rather than being
folded into the AI-provider-driven prompt flow, so a user always knows
exactly when they're spending Tripo3D generation credits.

No third-party HTTP dependency - uses urllib, consistent with the rest
of this codebase.
"""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://openapi.tripo3d.ai/v3"

# As of Tripo3D's V3 API, "model_version" (aka "model") is a REQUIRED field
# for text-to-model generation - omitting it returns HTTP 400 / code 1004,
# unlike the old V2 API where it silently fell back to a server default.
# v3.1-20260211 is Tripo3D's current highest-quality model; callers can
# still override via generate_mesh_from_text(..., model_version=...).
DEFAULT_MODEL_VERSION = "v3.1-20260211"

# Tripo3D says generated model download URLs expire ~5 minutes after the
# task succeeds - download immediately, don't cache the URL itself.
_MODEL_URL_EXPIRY_HINT_SECONDS = 300

# Terminal task states. Anything else ("queued", "running", etc.) means
# keep polling.
_TERMINAL_STATES = {"success", "failed", "cancelled", "banned", "expired"}


class Tripo3DError(RuntimeError):
    """Raised for any Tripo3D network/API/timeout failure. Callers should
    surface this to the user rather than let it crash the bridge."""


def _request(
    method: str,
    path: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    if not api_key or not api_key.strip():
        raise Tripo3DError(
            "No Tripo3D API key configured. Get a free key at "
            "https://platform.tripo3d.ai/ and set TRIPO3D_API_KEY (env var) "
            "or paste it into AI Settings in the desktop app."
        )

    url = f"{BASE_URL}{path}"
    headers = {
        # A default urllib User-Agent ("Python-urllib/x.y") gets blocked by
        # some Cloudflare-fronted APIs as bot traffic (HTTP 403 / error
        # 1010) - see the same note in src/core/ai_provider.py. Send a
        # normal, app-identifying one instead.
        "User-Agent": "LRJK-Blender-AI-Studio/1.0",
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        if e.code == 401:
            raise Tripo3DError(
                "Tripo3D rejected the API key (HTTP 401). Check TRIPO3D_API_KEY."
            ) from e
        raise Tripo3DError(f"Tripo3D HTTP {e.code}: {detail[:300]}") from e
    except urllib.error.URLError as e:
        raise Tripo3DError(f"Could not reach Tripo3D: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise Tripo3DError(f"Tripo3D request timed out or failed: {e}") from e
    except json.JSONDecodeError as e:
        raise Tripo3DError(f"Tripo3D returned an unparseable response: {e}") from e

    if body.get("code", 0) != 0:
        raise Tripo3DError(f"Tripo3D API error: {body.get('message', body)}")

    return body.get("data", {})


def submit_text_to_model_task(
    prompt: str, api_key: str, model_version: str = "", timeout: float = 20.0
) -> str:
    """
    Submits a text-to-model generation job. Returns the task_id.

    model_version is always sent - Tripo3D's V3 API requires it (unlike the
    older V2 API, which defaulted it server-side when omitted). Pass "" or
    leave it out to use DEFAULT_MODEL_VERSION.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise Tripo3DError("Empty prompt.")

    payload: dict[str, Any] = {
        "prompt": prompt,
        "model": model_version.strip() or DEFAULT_MODEL_VERSION,
    }

    data = _request("POST", "/generation/text-to-model", api_key, payload, timeout=timeout)
    task_id = data.get("task_id")
    if not task_id:
        raise Tripo3DError(f"Tripo3D did not return a task_id: {data}")
    return task_id


def poll_task_until_done(
    task_id: str, api_key: str, timeout_total: float = 180.0, poll_interval: float = 2.0
) -> dict[str, Any]:
    """
    Polls GET /tasks/{task_id} until it reaches a terminal state or
    timeout_total seconds elapse. Returns the task's "data" dict.
    Raises Tripo3DError on failure/cancellation/timeout.
    """
    deadline = time.monotonic() + timeout_total
    last_status = "unknown"

    while time.monotonic() < deadline:
        data = _request("GET", f"/tasks/{task_id}", api_key, timeout=20.0)
        last_status = data.get("status", "unknown")

        if last_status == "success":
            return data
        if last_status in _TERMINAL_STATES:
            raise Tripo3DError(f"Tripo3D task ended with status '{last_status}': {data}")

        time.sleep(poll_interval)

    raise Tripo3DError(
        f"Tripo3D task did not finish within {timeout_total:.0f}s (last status: '{last_status}')."
    )


def download_model(model_url: str, dest_path: Path, timeout: float = 60.0) -> Path:
    """
    Downloads the generated mesh to dest_path. Tripo3D model URLs expire
    ~5 minutes after success, so this should be called immediately after
    poll_task_until_done returns.
    """
    req = urllib.request.Request(model_url, headers={"User-Agent": "LRJK-Blender-AI-Studio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(resp.read())
    except urllib.error.HTTPError as e:
        raise Tripo3DError(
            f"Downloading the generated model failed (HTTP {e.code}). "
            "The model URL may have expired (5-minute limit) - try generating again."
        ) from e
    except urllib.error.URLError as e:
        raise Tripo3DError(f"Downloading the generated model failed: {e.reason}") from e
    except OSError as e:
        raise Tripo3DError(f"Could not write downloaded model to disk: {e}") from e

    return dest_path


def _guess_extension(model_url: str) -> str:
    lower = model_url.split("?", 1)[0].lower()
    for ext in (".glb", ".gltf", ".obj", ".fbx"):
        if lower.endswith(ext):
            return ext
    return ".glb"  # Tripo3D's default export format


def _safe_filename_stem(prompt: str, task_id: str) -> str:
    keep = [c if (c.isalnum() or c in ("-", "_")) else "_" for c in prompt.strip()[:40]]
    stem = "".join(keep).strip("_") or "mesh"
    return f"{stem}_{task_id[:8]}"


def generate_mesh_from_text(
    prompt: str,
    api_key: str,
    dest_dir: Path,
    model_version: str = "",
    timeout_total: float = 180.0,
    poll_interval: float = 2.0,
) -> Path:
    """
    End-to-end helper: submit -> poll -> download. Returns the local Path
    of the downloaded mesh file. Raises Tripo3DError on any failure - the
    caller (main_window.py) should catch this and report it back to
    Blender rather than let it propagate into the HTTP bridge handler.
    """
    task_id = submit_text_to_model_task(prompt, api_key, model_version=model_version, timeout=20.0)
    data = poll_task_until_done(
        task_id, api_key, timeout_total=timeout_total, poll_interval=poll_interval
    )

    output = data.get("output", {}) or {}
    model_url = output.get("model_url") or output.get("pbr_model") or output.get("model")
    if not model_url:
        raise Tripo3DError(f"Tripo3D task succeeded but returned no model URL: {data}")

    ext = _guess_extension(model_url)
    dest_path = Path(dest_dir) / f"{_safe_filename_stem(prompt, task_id)}{ext}"
    return download_model(model_url, dest_path, timeout=60.0)
