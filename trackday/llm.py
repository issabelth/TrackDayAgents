"""Warstwa komunikacji z modelem językowym.

Agent nie woła SDK bezpośrednio - używa interfejsu ``StructuredLLM``. Dzięki temu:
* w testach podstawiamy ``FakeLLM`` (bez kosztów i bez sieci),
* w kolejnych etapach można dodać innego dostawcę lub węzeł LangGraph.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Błąd wywołania modelu (komunikat po polsku, gotowy do pokazania w UI)."""


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class StructuredLLM(Protocol):
    model: str

    def extract(self, system: str, user: str, schema: type[T]) -> LLMResult[T]: ...


class AnthropicLLM:
    """Claude przez oficjalne SDK ``anthropic`` + structured outputs (schemat Pydantic)."""

    def __init__(self, model: str, api_key: str | None = None, effort: str = "low"):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        # Bez api_key SDK samo szuka poświadczeń (ANTHROPIC_API_KEY, profil `ant`).
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def extract(self, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        a = self._anthropic
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
                output_config={"effort": self.effort},
            )
        except a.AuthenticationError as e:
            raise LLMError("Nieprawidłowy klucz API (ANTHROPIC_API_KEY).") from e
        except a.PermissionDeniedError as e:
            raise LLMError(f"Brak uprawnień do modelu {self.model}.") from e
        except a.NotFoundError as e:
            raise LLMError(f"Model {self.model} nie został znaleziony.") from e
        except a.RateLimitError as e:
            raise LLMError("Przekroczono limit zapytań do API - spróbuj za chwilę.") from e
        except a.BadRequestError as e:
            raise LLMError(f"Nieprawidłowe zapytanie do API: {e.message}") from e
        except a.APIStatusError as e:
            raise LLMError(f"Błąd API ({e.status_code}): {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError("Brak połączenia z API modelu.") from e

        if response.stop_reason == "refusal":
            raise LLMError("Model odmówił przetworzenia tej treści.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Odpowiedź modelu przekroczyła limit długości (za duży fragment strony).")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Model nie zwrócił danych zgodnych ze schematem.")
        return LLMResult(
            parsed=parsed,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


def _ollama_base(url: str) -> str:
    """OLLAMA_HOST bywa podany bez schematu albo jako 0.0.0.0 - normalizujemy do adresu klienta."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url.replace("://0.0.0.0", "://localhost")


class OllamaLLM:
    """Lokalny model przez Ollamę (darmowy, bez klucza API, dane nie opuszczają komputera).

    Używa endpointu /api/chat z parametrem ``format`` = JSON Schema - Ollama ogranicza
    generowanie do JSON zgodnego ze schematem, a wynik i tak jest walidowany Pydantic.
    """

    # Mniejsze fragmenty niż dla Claude: okno kontekstu lokalnego modelu jest ograniczone (num_ctx).
    max_chunk_chars = 15_000

    def __init__(self, model: str, base_url: str = "http://localhost:11434", num_ctx: int = 16384,
                 timeout_s: int = 600):
        self.model = model
        self.base_url = _ollama_base(base_url)
        self.num_ctx = num_ctx
        self.timeout_s = timeout_s

    def extract(self, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        import requests
        from pydantic import ValidationError

        body = {
            "model": self.model,
            "stream": False,
            "format": schema.model_json_schema(),
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout_s)
        except requests.ConnectionError as e:
            raise LLMError("Brak połączenia z Ollamą - uruchom aplikację Ollama i spróbuj ponownie.") from e
        except requests.Timeout as e:
            raise LLMError(f"Model lokalny nie odpowiedział w {self.timeout_s} s.") from e
        if resp.status_code == 404:
            raise LLMError(f"Model {self.model} nie jest pobrany w Ollamie (ollama pull {self.model}).")
        if resp.status_code >= 400:
            raise LLMError(f"Błąd Ollamy ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        if data.get("done_reason") == "length":
            raise LLMError("Odpowiedź modelu lokalnego przekroczyła limit długości (zwiększ num_ctx).")
        try:
            parsed = schema.model_validate_json(data["message"]["content"])
        except (KeyError, ValidationError) as e:
            raise LLMError("Model lokalny nie zwrócił danych zgodnych ze schematem.") from e
        return LLMResult(parsed=parsed, model=f"ollama:{self.model}",
                         input_tokens=data.get("prompt_eval_count", 0),
                         output_tokens=data.get("eval_count", 0))


def list_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Nazwy pobranych modeli czatu w Ollamie (bez modeli embeddingowych). Pusta lista, gdy Ollama nie działa."""
    import requests

    try:
        tags = requests.get(f"{_ollama_base(base_url)}/api/tags", timeout=3).json()
    except (requests.RequestException, ValueError):
        return []
    names = [m["name"] for m in tags.get("models", [])]
    return [n for n in names if not any(x in n.lower() for x in ("embed", "bge-", "bge:", "nomic"))]


def create_llm(model_id: str, settings, api_key: str | None = None) -> StructuredLLM:
    """Tworzy klienta modelu na podstawie identyfikatora: 'ollama:<nazwa>' albo model Claude."""
    from trackday.config import OLLAMA_PREFIX

    if model_id.startswith(OLLAMA_PREFIX):
        return OllamaLLM(model_id.removeprefix(OLLAMA_PREFIX), base_url=settings.ollama_url,
                         num_ctx=settings.ollama_num_ctx)
    return AnthropicLLM(model=model_id, api_key=api_key or settings.api_key, effort=settings.effort)


def is_local(model_id: str) -> bool:
    from trackday.config import OLLAMA_PREFIX

    return model_id.startswith(OLLAMA_PREFIX)


class FakeLLM:
    """Atrapa modelu do testów: zwraca odpowiedź z podanej funkcji."""

    def __init__(self, responder: Callable[[str, str], BaseModel], model: str = "fake-llm"):
        self.responder = responder
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def extract(self, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        self.calls.append((system, user))
        out = self.responder(system, user)
        return LLMResult(parsed=schema.model_validate(out.model_dump()), model=self.model)
