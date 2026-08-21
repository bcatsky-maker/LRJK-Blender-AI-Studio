import json
from unittest.mock import MagicMock, patch

import pytest

from src.core.ai_provider import (
    ACTION_SCHEMA,
    AIProviderError,
    _extract_json_object,
    request_scene_action,
    request_scene_program,
    validate_scene_action,
    validate_scene_program,
)


def _fake_response(body: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(body).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False
    return mock_resp


def test_validate_scene_action_rejects_unknown_action():
    with pytest.raises(AIProviderError):
        validate_scene_action({"action": "delete_everything", "params": {}})


def test_validate_scene_action_rejects_non_dict_params():
    with pytest.raises(AIProviderError):
        validate_scene_action({"action": "generate_terrain", "params": "not-a-dict"})


def test_validate_scene_action_accepts_known_action():
    result = validate_scene_action(
        {"action": "generate_terrain", "params": {"displace_strength": 2}}
    )
    assert result == {"action": "generate_terrain", "params": {"displace_strength": 2}}


def test_extract_json_object_handles_markdown_fence():
    text = '```json\n{"action": "generate_terrain", "params": {}}\n```'
    result = _extract_json_object(text)
    assert result["action"] == "generate_terrain"


def test_extract_json_object_raises_on_garbage():
    with pytest.raises(AIProviderError):
        _extract_json_object("not json at all")


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_action_openai_style(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"action": "generate_character", "params": {"gender": 0.0}}'
                    }
                }
            ]
        }
    )
    result = request_scene_action(
        "a woman", "OpenAI (Paid / Tiered)", "https://api.openai.com/v1", "sk-test", "gpt-4o"
    )
    assert result == {"action": "generate_character", "params": {"gender": 0.0}}


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_action_sends_non_default_user_agent(mock_urlopen):
    """
    Some providers (Groq's Cloudflare-fronted endpoint, notably) reject the
    default "Python-urllib/x.y" User-Agent outright as bot traffic (HTTP 403,
    Cloudflare error 1010) before the request ever reaches the actual API.
    Every outbound request must identify itself instead.
    """
    mock_urlopen.return_value = _fake_response(
        {"choices": [{"message": {"content": '{"action": "generate_terrain", "params": {}}'}}]}
    )
    request_scene_action(
        "blue hills",
        "Custom REST API Endpoint",
        "https://api.groq.com/openai/v1",
        "gsk-test",
        "llama3",
    )
    sent_req = mock_urlopen.call_args[0][0]
    user_agent = sent_req.get_header("User-agent")
    assert user_agent and "python-urllib" not in user_agent.lower()


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_action_anthropic_style(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {"content": [{"type": "text", "text": '{"action": "generate_terrain", "params": {}}'}]}
    )
    result = request_scene_action(
        "blue hills",
        "Anthropic Claude (Paid)",
        "https://api.anthropic.com/v1",
        "sk-ant-test",
        "claude-3-5-sonnet-20240620",
    )
    assert result["action"] == "generate_terrain"


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_action_rejects_disallowed_action_from_model(mock_urlopen):
    """
    Even if the model (or a compromised/misbehaving endpoint) returns
    something outside the whitelist, it must never reach the caller as a
    usable action - this is the property that keeps AI-driven generation
    from becoming a code-execution vector once it's wired into Blender.
    """
    mock_urlopen.return_value = _fake_response(
        {
            "choices": [
                {
                    "message": {
                        "content": '{"action": "run_shell_command", "params": {"cmd": "rm -rf /"}}'
                    }
                }
            ]
        }
    )
    with pytest.raises(AIProviderError):
        request_scene_action(
            "do something", "OpenAI", "https://api.openai.com/v1", "sk-test", "gpt-4o"
        )


def test_request_scene_action_requires_endpoint():
    with pytest.raises(AIProviderError):
        request_scene_action("prompt", "OpenAI", "", "sk", "gpt-4o")


def test_request_scene_action_requires_prompt():
    with pytest.raises(AIProviderError):
        request_scene_action("   ", "OpenAI", "https://api.openai.com/v1", "sk", "gpt-4o")


# ---------------------------------------------------------------------------
# Multi-action scene programs (the real generator)
# ---------------------------------------------------------------------------


def test_schema_has_composable_primitives():
    # The whole point of the rework: the AI can compose real primitives,
    # not just pick one of three canned high-level actions.
    for name in (
        "add_primitive",
        "add_material",
        "add_light",
        "set_camera",
        "import_asset_from_library",
    ):
        assert name in ACTION_SCHEMA


def test_validate_scene_program_accepts_actions_wrapper():
    program = validate_scene_program(
        {
            "actions": [
                {"action": "add_primitive", "params": {"shape": "torus"}},
                {"action": "add_light", "params": {}},
            ]
        }
    )
    assert [a["action"] for a in program] == ["add_primitive", "add_light"]


def test_validate_scene_program_accepts_bare_list_and_single_dict():
    assert [
        a["action"] for a in validate_scene_program([{"action": "set_camera", "params": {}}])
    ] == ["set_camera"]
    assert [a["action"] for a in validate_scene_program({"action": "add_light", "params": {}})] == [
        "add_light"
    ]


def test_validate_scene_program_drops_bad_actions_keeps_good_ones():
    program = validate_scene_program(
        {
            "actions": [
                {"action": "add_primitive", "params": {"shape": "cube"}},
                {"action": "run_shell_command", "params": {"cmd": "rm -rf /"}},  # must be dropped
                {"action": "add_material", "params": {"base_color": [1, 0, 0, 1]}},
            ]
        }
    )
    names = [a["action"] for a in program]
    assert "run_shell_command" not in names
    assert names == ["add_primitive", "add_material"]


def test_validate_scene_program_raises_when_nothing_valid():
    with pytest.raises(AIProviderError):
        validate_scene_program({"actions": [{"action": "evil", "params": {}}]})
    with pytest.raises(AIProviderError):
        validate_scene_program({"actions": []})


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_program_openai_style(mock_urlopen):
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "actions": [
                                {"action": "add_primitive", "params": {"shape": "torus"}},
                                {"action": "add_material", "params": {"base_color": [0, 0, 1, 1]}},
                                {"action": "add_light", "params": {"light_type": "AREA"}},
                                {"action": "set_camera", "params": {}},
                            ]
                        }
                    )
                }
            }
        ]
    }
    mock_urlopen.return_value = _fake_response(body)
    program = request_scene_program(
        "a blue donut", "Custom REST API Endpoint", "https://api.groq.com/openai/v1", "gsk", "llama"
    )
    assert [a["action"] for a in program] == [
        "add_primitive",
        "add_material",
        "add_light",
        "set_camera",
    ]


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_request_scene_program_rejects_disallowed_actions(mock_urlopen):
    # Even inside a program, a non-whitelisted action can never survive to
    # reach Blender - it's dropped, and if it's the only one, we raise.
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"actions": [{"action": "os_system", "params": {"cmd": "evil"}}]}
                    )
                }
            }
        ]
    }
    mock_urlopen.return_value = _fake_response(body)
    with pytest.raises(AIProviderError):
        request_scene_program("do evil", "OpenAI", "https://api.openai.com/v1", "sk", "gpt-4o")


# --- Reasoning-model handling (Groq gpt-oss, o1/o3, DeepSeek-R1, ...) ---------
# A reasoning model spends tokens on hidden reasoning before the visible
# answer; with a modest budget a COMPLEX prompt can return an EMPTY
# message.content (finish_reason "length"). These lock in the mitigations.

from src.core.ai_provider import _is_reasoning_model  # noqa: E402


def test_is_reasoning_model_detects_known_families():
    assert _is_reasoning_model("openai/gpt-oss-120b")
    assert _is_reasoning_model("o1-preview")
    assert _is_reasoning_model("deepseek-reasoner")
    assert not _is_reasoning_model("gpt-4o")
    assert not _is_reasoning_model("llama-3.1-70b")


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_reasoning_model_gets_effort_and_json_format(mock_urlopen):
    mock_urlopen.return_value = _fake_response(
        {
            "choices": [
                {"message": {"content": '{"actions": [{"action": "add_light", "params": {}}]}'}}
            ]
        }
    )
    request_scene_program(
        "a lit scene",
        "Custom REST API Endpoint",
        "https://api.groq.com/openai/v1",
        "gsk",
        "openai/gpt-oss-120b",
    )
    sent_req = mock_urlopen.call_args[0][0]
    payload = json.loads(sent_req.data.decode("utf-8"))
    assert payload.get("reasoning_effort") == "low"  # curb hidden reasoning
    assert payload.get("response_format") == {"type": "json_object"}
    assert payload.get("max_tokens", 0) >= 4000  # room for reasoning + answer


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_empty_content_from_length_raises_clear_error(mock_urlopen):
    # The exact production symptom: choices present, content '', cut off.
    mock_urlopen.return_value = _fake_response(
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    )
    with pytest.raises(AIProviderError) as ei:
        request_scene_program(
            "a huge complex city",
            "Custom REST API Endpoint",
            "https://api.groq.com/openai/v1",
            "gsk",
            "openai/gpt-oss-120b",
        )
    assert "token limit" in str(ei.value).lower()


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_reasoning_content_fallback_is_used_when_content_blank(mock_urlopen):
    # Some backends put the answer in message.reasoning_content when
    # content is blank; we read it rather than failing.
    mock_urlopen.return_value = _fake_response(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": '{"actions": [{"action": "add_light", "params": {}}]}',
                    }
                }
            ]
        }
    )
    program = request_scene_program(
        "one light",
        "Custom REST API Endpoint",
        "https://api.groq.com/openai/v1",
        "gsk",
        "openai/gpt-oss-120b",
    )
    assert program[0]["action"] == "add_light"


@patch("src.core.ai_provider.urllib.request.urlopen")
def test_retry_without_extras_when_endpoint_rejects_them(mock_urlopen):
    # A picky endpoint 400s on response_format; the call should retry once
    # WITHOUT the optional fields instead of failing outright.
    import urllib.error

    good = _fake_response(
        {
            "choices": [
                {"message": {"content": '{"actions": [{"action": "add_light", "params": {}}]}'}}
            ]
        }
    )

    calls = {"n": 0}

    def _side_effect(req, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "response_format not supported",
                {},
                MagicMock(read=lambda: b'{"error":"response_format unsupported"}'),
            )
        return good

    mock_urlopen.side_effect = _side_effect
    program = request_scene_program(
        "one light",
        "Custom REST API Endpoint",
        "https://api.groq.com/openai/v1",
        "gsk",
        "gpt-4o",
    )
    assert program[0]["action"] == "add_light"
    assert calls["n"] == 2  # first with extras (rejected), then plain retry
