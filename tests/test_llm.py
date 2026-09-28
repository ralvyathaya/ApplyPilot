import httpx
import pytest
from unittest.mock import patch

from applypilot.llm import LLMClient, _detect_provider


def test_detect_provider_basic(monkeypatch):
    monkeypatch.setenv("LLM_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "model-primary")

    base_url, model, api_key = _detect_provider()
    assert base_url == "https://api.example.com/v1"
    assert model == "model-primary"
    assert api_key == "test-key"


def test_client_fallback_model_parsing(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "fb2, fb3, primary")
    client = LLMClient(
        base_url="https://api.example.com/v1",
        model="primary, fb1",
        api_key="test-key",
        fallback_models=["fb4"],
    )
    assert client.model == "primary"
    # Should contain deduplicated fallbacks, excluding primary
    assert client.fallback_models == ["fb1", "fb4", "fb2", "fb3"]


def test_client_fallback_on_quota_exhausted():
    client = LLMClient(
        base_url="https://api.example.com/v1",
        api_key="mock-key",
        model="primary-model",
        fallback_models=["fallback-model"],
    )

    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    quota_resp = httpx.Response(
        429,
        request=req,
        json={"error": {"message": "The requested model has exhausted its current quota"}},
    )
    success_resp = httpx.Response(
        200,
        request=req,
        json={"choices": [{"message": {"role": "assistant", "content": "Fallback success"}}]},
    )

    with patch.object(client._client, "post", side_effect=[quota_resp, success_resp]) as mock_post:
        result = client.ask("Hello")
        assert result == "Fallback success"
        assert mock_post.call_count == 2
        first_payload = mock_post.call_args_list[0][1]["json"]
        second_payload = mock_post.call_args_list[1][1]["json"]
        assert first_payload["model"] == "primary-model"
        assert second_payload["model"] == "fallback-model"


def test_reasoning_content_fallback():
    client = LLMClient(
        base_url="https://api.example.com/v1",
        api_key="mock-key",
        model="reasoning-model",
    )

    req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    # Content is empty, but reasoning_content has the thought/reply
    reasoning_resp = httpx.Response(
        200,
        request=req,
        json={"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "Chain of thought answer"}}]},
    )

    with patch.object(client._client, "post", return_value=reasoning_resp):
        result = client.ask("Hello")
        assert result == "Chain of thought answer"
