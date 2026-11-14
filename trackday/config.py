"""Konfiguracja aplikacji (zmienne środowiskowe + ścieżki)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"

load_dotenv(PROJECT_ROOT / ".env")

AVAILABLE_MODELS = {
    "claude-haiku-5-5": "Claude Haiku 5.5 (tani, do ekstrakcji)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (dokładniejszy)",
}


@dataclass
class Settings:
    api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    model: str = field(default_factory=lambda: os.getenv("TRACKDAY_MODEL", "claude-haiku-5-5"))
    effort: str = field(default_factory=lambda: os.getenv("TRACKDAY_EFFORT", "low"))
    db_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / os.getenv("TRACKDAY_DB_PATH", "data/trackday.db")
    )
    # Limity bezpieczeństwa / kosztów
    max_page_bytes: int = 2_000_000
    max_chunk_chars: int = 40_000
    max_pasted_chars: int = 60_000
    http_timeout_s: int = 15
    user_agent: str = "TrackDayAgents/0.1 (PoC; projekt studencki)"


def get_settings() -> Settings:
    return Settings()
