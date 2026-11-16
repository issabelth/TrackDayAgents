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

# Modele Claude (płatne API). Modele lokalne Ollamy (darmowe) mają identyfikator "ollama:<nazwa>"
# i są wykrywane w czasie działania (trackday.llm.list_ollama_models).
CLAUDE_MODELS = {
    "claude-haiku-5-5": "Claude Haiku 5.5 (API, tani)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (API, dokładniejszy)",
}
OLLAMA_PREFIX = "ollama:"
DEFAULT_OLLAMA_MODEL = "ollama:qwen2.5:7b"


def _default_model() -> str:
    if os.getenv("TRACKDAY_MODEL"):
        return os.environ["TRACKDAY_MODEL"]
    return "claude-haiku-5-5" if os.getenv("ANTHROPIC_API_KEY") else DEFAULT_OLLAMA_MODEL


@dataclass
class Settings:
    api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY") or None)
    model: str = field(default_factory=_default_model)
    effort: str = field(default_factory=lambda: os.getenv("TRACKDAY_EFFORT", "low"))
    db_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / os.getenv("TRACKDAY_DB_PATH", "data/trackday.db")
    )
    ollama_url: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    ollama_num_ctx: int = field(default_factory=lambda: int(os.getenv("TRACKDAY_OLLAMA_NUM_CTX", "16384")))
    # Limity bezpieczeństwa / kosztów
    max_page_bytes: int = 2_000_000
    max_chunk_chars: int = 40_000
    max_pasted_chars: int = 60_000
    http_timeout_s: int = 15
    crawl_delay_s: float = 0.5  # przerwa między pobraniami kolejnych podstron (uprzejmość wobec serwera)
    user_agent: str = "TrackDayAgents/0.1 (PoC; projekt studencki)"


def get_settings() -> Settings:
    return Settings()
