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


def test_resolve_endpoint_routing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-key")

    client = LLMClient(
        base_url="https://api.cosmoshub.tech/v1",
        api_key="cosmo-key",
        model="deepseek-v3.2-free",
        fallback_models=["hy4-preview-free", "gemini-3.8-flash"],
    )

    # 1. Primary model routes to Cosmoshub
    base_url, model, key, is_gemini = client._resolve_endpoint("deepseek-v3.2-free")
    assert base_url == "https://api.cosmoshub.tech/v1"
    assert model == "deepseek-v3.2-free"
    assert key == "cosmo-key"
    assert is_gemini is False

    # 2. Gemini fallback model routes to Google Gemini with GEMINI_API_KEY
    base_url, model, key, is_gemini = client._resolve_endpoint("gemini-3.8-flash")
    assert base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
    assert model == "gemini-3.8-flash"
    assert key == "gemini-secret-key"
    assert is_gemini is True


def test_cross_provider_fallback_execution(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-secret-key")

    client = LLMClient(
        base_url="https://api.cosmoshub.tech/v1",
        api_key="cosmo-key",
        model="deepseek-v3.2-free",
        fallback_models=["gemini-3.8-flash"],
    )

    req_cosmo = httpx.Request("POST", "https://api.cosmoshub.tech/v1/chat/completions")
    req_gemini = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")

    # Cosmoshub fails with 500
    cosmo_fail_resp = httpx.Response(500, request=req_cosmo, text="Cosmoshub overloaded")
    # Gemini succeeds
    gemini_ok_resp = httpx.Response(
        200,
        request=req_gemini,
        json={"choices": [{"message": {"role": "assistant", "content": "Gemini success"}}]},
    )

    with patch.object(client._client, "post", side_effect=[cosmo_fail_resp, gemini_ok_resp]) as mock_post:
        result = client.ask("Tailor my resume")
        assert result == "Gemini success"
        assert mock_post.call_count == 2
        # Check first call went to Cosmoshub
        assert "api.cosmoshub.tech" in str(mock_post.call_args_list[0][0][0])
        # Check second call went to Google Gemini with Bearer gemini-secret-key
        assert "generativelanguage.googleapis.com" in str(mock_post.call_args_list[1][0][0])
        auth_header = mock_post.call_args_list[1][1]["headers"]["Authorization"]
        assert auth_header == "Bearer gemini-secret-key"
