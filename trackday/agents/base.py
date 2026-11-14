"""Wspólna baza agentów.

Każdy agent ma nazwę, rolę i metodę ``run(input) -> output`` z jawnie typowanymi
danymi wejściowymi i wyjściowymi (Pydantic). Dzięki temu w kolejnych etapach agenci
mogą zostać podpięci jako węzły grafu LangGraph bez zmiany ich logiki.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


class BaseAgent(ABC, Generic[InT, OutT]):
    name: str = "agent"
    role: str = ""

    @abstractmethod
    def run(self, data: InT) -> OutT:  # pragma: no cover - interfejs
        ...

    def describe(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role}
