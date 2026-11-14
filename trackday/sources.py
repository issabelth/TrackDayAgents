"""Ładowanie listy źródeł (organizatorów) z pliku konfiguracyjnego."""

from __future__ import annotations

import json
import re

from trackday.config import DATA_DIR
from trackday.models import Source


def load_sources() -> list[Source]:
    raw = json.loads((DATA_DIR / "sources.json").read_text(encoding="utf-8"))
    return [Source.model_validate(s) for s in raw["sources"]]


def sources_from_urls(text: str) -> list[Source]:
    """Zamienia adresy wpisane przez użytkownika (po jednym w linii) na źródła."""
    out: list[Source] = []
    for i, line in enumerate(l.strip() for l in text.splitlines()):
        if not line:
            continue
        host = re.sub(r"^https?://", "", line).split("/")[0]
        out.append(Source(id=f"url{i}", name=host or line, url=line, base_url=line, kind="web"))
    return out
