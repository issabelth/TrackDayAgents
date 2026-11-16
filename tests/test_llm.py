"""Testy klienta Ollamy bez uruchomionej Ollamy (odpowiedzi HTTP podmienione)."""

import json

import pytest
import requests

from trackday.config import Settings
from trackday.llm import AnthropicLLM, LLMError, OllamaLLM, _ollama_base, create_llm, is_local
from trackday.models import ExtractionResult

VALID = {"events": [], "warnings": [], "suspicious_instructions": False}


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


def test_ollama_sends_schema_and_parses(monkeypatch):
    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, body=json)
        return FakeResp(payload={"message": {"content": __import__("json").dumps(VALID)},
                                 "prompt_eval_count": 100, "eval_count": 20, "done_reason": "stop"})

    monkeypatch.setattr(requests, "post", fake_post)
    res = OllamaLLM("qwen2.5:7b").extract("sys", "user", ExtractionResult)
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["body"]["format"] == ExtractionResult.model_json_schema()
    assert sent["body"]["options"]["temperature"] == 0
    assert res.parsed.events == [] and res.input_tokens == 100 and res.model == "ollama:qwen2.5:7b"


@pytest.mark.parametrize("resp,msg", [
    (FakeResp(404, text="model not found"), "nie jest pobrany"),
    (FakeResp(200, {"message": {"content": "to nie JSON"}}), "zgodnych ze schematem"),
    (FakeResp(200, {"message": {"content": json.dumps(VALID)}, "done_reason": "length"}), "limit długości"),
])
def test_ollama_errors(monkeypatch, resp, msg):
    monkeypatch.setattr(requests, "post", lambda *a, **k: resp)
    with pytest.raises(LLMError, match=msg):
        OllamaLLM("qwen2.5:7b").extract("s", "u", ExtractionResult)


def test_ollama_not_running(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(LLMError, match="uruchom aplikację Ollama"):
        OllamaLLM("x").extract("s", "u", ExtractionResult)


def test_factory_and_host_normalization():
    s = Settings(api_key="sk-test")
    assert isinstance(create_llm("ollama:qwen2.5:7b", s), OllamaLLM) and is_local("ollama:x")
    assert isinstance(create_llm("claude-haiku-5-5", s), AnthropicLLM) and not is_local("claude-haiku-5-5")
    assert _ollama_base("0.0.0.0:11434") == "http://localhost:11434"
