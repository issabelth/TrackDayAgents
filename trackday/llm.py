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
