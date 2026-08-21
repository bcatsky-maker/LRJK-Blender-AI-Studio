import json
from unittest.mock import MagicMock, patch

import pytest

from src.core.text_to_3d import (
    DEFAULT_MODEL_VERSION,
    Tripo3DError,
    _guess_extension,
    _safe_filename_stem,
    download_model,
    generate_mesh_from_text,
    poll_task_until_done,
    submit_text_to_model_task,
)


def _fake_response(body: dict | None = None, raw_bytes: bytes | None = None):
    mock_resp = MagicMock()
    if raw_bytes is not None:
        mock_resp.read.return_value = raw_bytes
    else:
        mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def test_submit_requires_api_key():
    with pytest.raises(Tripo3DError):
        submit_text_to_model_task("a chair", "")


def test_submit_requires_prompt():
    with pytest.raises(Tripo3DError):
        submit_text_to_model_task("   ", "fake-key")


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_submit_text_to_model_task_returns_task_id(mock_urlopen):
    mock_urlopen.return_value = _fake_response({"code": 0, "data": {"task_id": "task_abc123"}})
    task_id = submit_text_to_model_task("a cute cat", "fake-key")
    assert task_id == "task_abc123"

    # Confirm the request shape matches Tripo3D's documented V3 endpoint.
    sent_req = mock_urlopen.call_args[0][0]
    assert sent_req.full_url == "https://openapi.tripo3d.ai/v3/generation/text-to-model"
    assert sent_req.get_header("Authorization") == "Bearer fake-key"
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body == {"prompt": "a cute cat", "model": DEFAULT_MODEL_VERSION}


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_submit_text_to_model_task_sends_model_version_when_given(mock_urlopen):
    """
    Tripo3D's V3 API requires "model" on every text-to-model request (HTTP
    400 / code 1004 if omitted) - unlike the older V2 API, which silently
    defaulted it server-side. Confirm we never send a request without it,
    and that an explicit override is respected.
    """
    mock_urlopen.return_value = _fake_response({"code": 0, "data": {"task_id": "task_abc123"}})
    submit_text_to_model_task("a cute cat", "fake-key", model_version="P1-20260311")
    sent_req = mock_urlopen.call_args[0][0]
    body = json.loads(sent_req.data.decode("utf-8"))
    assert body["model"] == "P1-20260311"


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_submit_raises_on_nonzero_code(mock_urlopen):
    mock_urlopen.return_value = _fake_response({"code": 2000, "message": "insufficient credits"})
    with pytest.raises(Tripo3DError):
        submit_text_to_model_task("a chair", "fake-key")


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_poll_task_until_done_returns_on_success(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {
            "code": 0,
            "data": {
                "task_id": "task_abc123",
                "status": "success",
                "output": {"model_url": "https://cdn.tripo3d.ai/model.glb"},
            },
        }
    )
    data = poll_task_until_done("task_abc123", "fake-key", timeout_total=10.0, poll_interval=0.01)
    assert data["status"] == "success"
    assert data["output"]["model_url"] == "https://cdn.tripo3d.ai/model.glb"


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_poll_task_until_done_raises_on_failed_status(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {
            "code": 0,
            "data": {"task_id": "task_abc123", "status": "failed"},
        }
    )
    with pytest.raises(Tripo3DError):
        poll_task_until_done("task_abc123", "fake-key", timeout_total=10.0, poll_interval=0.01)


@patch("src.core.text_to_3d.time.sleep", return_value=None)
@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_poll_task_until_done_keeps_polling_while_running(mock_urlopen, mock_sleep):
    mock_urlopen.side_effect = [
        _fake_response({"code": 0, "data": {"task_id": "t1", "status": "queued"}}),
        _fake_response({"code": 0, "data": {"task_id": "t1", "status": "running"}}),
        _fake_response(
            {
                "code": 0,
                "data": {
                    "task_id": "t1",
                    "status": "success",
                    "output": {"model_url": "https://cdn.tripo3d.ai/x.glb"},
                },
            }
        ),
    ]
    data = poll_task_until_done("t1", "fake-key", timeout_total=10.0, poll_interval=0.01)
    assert data["status"] == "success"
    assert mock_urlopen.call_count == 3


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_poll_task_until_done_times_out(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {"code": 0, "data": {"task_id": "t1", "status": "running"}}
    )
    with pytest.raises(Tripo3DError):
        poll_task_until_done("t1", "fake-key", timeout_total=0.02, poll_interval=0.01)


def test_guess_extension_from_url():
    assert _guess_extension("https://cdn.tripo3d.ai/x/y/model.glb?sig=abc") == ".glb"
    assert _guess_extension("https://cdn.tripo3d.ai/model.obj") == ".obj"
    assert _guess_extension("https://cdn.tripo3d.ai/model.fbx") == ".fbx"
    assert _guess_extension("https://cdn.tripo3d.ai/unknown") == ".glb"


def test_safe_filename_stem_strips_unsafe_chars():
    stem = _safe_filename_stem('a red/blue "dragon"!!', "task_abcdef123456")
    assert "/" not in stem and '"' not in stem
    assert stem.endswith("task_abc")


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_download_model_writes_bytes_to_disk(mock_urlopen, tmp_path):
    mock_urlopen.return_value = _fake_response(raw_bytes=b"FAKE_GLB_BYTES")
    dest = tmp_path / "sub" / "model.glb"
    result = download_model("https://cdn.tripo3d.ai/model.glb", dest)
    assert result == dest
    assert dest.read_bytes() == b"FAKE_GLB_BYTES"


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_generate_mesh_from_text_end_to_end(mock_urlopen, tmp_path):
    mock_urlopen.side_effect = [
        _fake_response({"code": 0, "data": {"task_id": "task_xyz789"}}),
        _fake_response(
            {
                "code": 0,
                "data": {
                    "task_id": "task_xyz789",
                    "status": "success",
                    "output": {"model_url": "https://cdn.tripo3d.ai/result.glb"},
                },
            }
        ),
        _fake_response(raw_bytes=b"FAKE_MESH_BYTES"),
    ]
    result_path = generate_mesh_from_text(
        "a treasure chest", "fake-key", tmp_path, poll_interval=0.01
    )
    assert result_path.exists()
    assert result_path.read_bytes() == b"FAKE_MESH_BYTES"
    assert result_path.suffix == ".glb"
    assert result_path.parent == tmp_path


@patch("src.core.text_to_3d.urllib.request.urlopen")
def test_generate_mesh_from_text_raises_when_no_model_url(mock_urlopen, tmp_path):
    mock_urlopen.side_effect = [
        _fake_response({"code": 0, "data": {"task_id": "task_xyz789"}}),
        _fake_response(
            {"code": 0, "data": {"task_id": "task_xyz789", "status": "success", "output": {}}}
        ),
    ]
    with pytest.raises(Tripo3DError):
        generate_mesh_from_text("a treasure chest", "fake-key", tmp_path, poll_interval=0.01)
