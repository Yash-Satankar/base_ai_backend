"""
Tests for the local Ollama provider in app/services/ai_service.py.
No network: httpx.post is monkeypatched.
"""

import httpx
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services import ai_service


class _FakeResp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


@pytest.fixture
def ollama_provider(monkeypatch):
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "llama3.1")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434")


def _capture_post(monkeypatch, payload=None, **resp_kw):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        sent["timeout"] = timeout
        return _FakeResp(payload or {"message": {"content": '{"ok": true}'},
                                     "prompt_eval_count": 11, "eval_count": 7}, **resp_kw)

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_generate_schema_dispatches_to_ollama(ollama_provider, monkeypatch):
    sent = _capture_post(monkeypatch)
    out = ai_service.generate_schema(
        system_prompt="Return ONLY valid JSON matching this schema: {}",
        user_prompt="hello",
        max_tokens=1234,
    )
    assert out["provider"] == "ollama"
    assert out["model"] == "llama3.1"
    assert out["content"] == '{"ok": true}'
    assert out["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["json"]["stream"] is False
    assert sent["json"]["options"]["num_predict"] == 1234
    assert sent["json"]["options"]["num_ctx"] == settings.OLLAMA_NUM_CTX


def test_json_format_set_only_when_prompt_wants_json(ollama_provider, monkeypatch):
    sent = _capture_post(monkeypatch)
    ai_service._generate_with_ollama("...Return ONLY valid JSON...", "u")
    assert sent["json"].get("format") == "json"

    sent2 = _capture_post(monkeypatch)
    ai_service._generate_with_ollama(
        "Raw MySQL CREATE TABLE statements ONLY. No explanation.", "u"
    )
    assert "format" not in sent2["json"]


def test_reasoning_think_block_is_stripped(ollama_provider, monkeypatch):
    _capture_post(
        monkeypatch,
        payload={"message": {"content": "<think>lots of planning</think>\n{\"a\":1}"},
                 "prompt_eval_count": 1, "eval_count": 2},
    )
    out = ai_service._generate_with_ollama("Return ONLY valid JSON", "u")
    assert out["content"] == '{"a":1}'


def test_ollama_http_error_raises_502(ollama_provider, monkeypatch):
    _capture_post(monkeypatch, payload={}, status_code=404, text="model 'x' not found")
    with pytest.raises(HTTPException) as ei:
        ai_service._generate_with_ollama("Return ONLY valid JSON", "u")
    assert ei.value.status_code == 502
    assert "not found" in ei.value.detail


def test_ollama_connection_error_raises_502(ollama_provider, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(HTTPException) as ei:
        ai_service._generate_with_ollama("Return ONLY valid JSON", "u")
    assert ei.value.status_code == 502
    assert "ollama serve" in ei.value.detail.lower()


def test_call_llm_prices_ollama_responses_at_zero(monkeypatch):
    """An Ollama response must cost 0 so it never trips the cost-degrade path."""
    from app.conversation import llm_client

    def fake_generate_schema(system_prompt, user_prompt, max_tokens=None, model=None, temperature=None):
        return {
            "content": "{}",
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "usage": {"input_tokens": 5000, "output_tokens": 9000},
        }

    monkeypatch.setattr(llm_client, "generate_schema", fake_generate_schema)
    out = llm_client.call_llm(operation="clarify_turn", system_prompt="s", user_prompt="u",
                              session_id="conv-ollama-price")
    assert out["cost_usd"] == 0.0
    assert llm_client.conversation_cost("conv-ollama-price") == 0.0


def test_ollama_works_without_a_groq_key(ollama_provider, monkeypatch):
    """The whole point: AI_PROVIDER=ollama must not require GROQ_API_KEY."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    _capture_post(monkeypatch)
    out = ai_service.generate_schema("Return ONLY valid JSON", "hi")
    assert out["provider"] == "ollama"


def test_groq_provider_without_key_raises_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "AI_PROVIDER", "groq")
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    monkeypatch.setattr(ai_service, "_groq_client", None)
    with pytest.raises(HTTPException) as ei:
        ai_service.get_groq_client()
    assert ei.value.status_code == 500
    assert "GROQ_API_KEY" in ei.value.detail
