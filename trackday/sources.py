"""Źródła (strony organizatorów) przeszukiwane przez agenta.

* ``data/sources.json`` - źródła domyślne (strony przykładowe); przy pierwszym uruchomieniu
  aplikacji trafiają do bazy, a potem użytkownik zarządza listą w zakładce „Źródła”.
* lista zapisana w SQLite (``EventRepository.list_sources``) - stała lista, którą agent
  przeszukuje przy każdym uruchomieniu (tylko pozycje aktywne).
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

from trackday.config import DATA_DIR
from trackday.models import Source


def load_sources() -> list[Source]:
    """Źródła domyślne z pliku (używane też przez testy i skrypt ewaluacji)."""
    raw = json.loads((DATA_DIR / "sources.json").read_text(encoding="utf-8"))
    return [Source.model_validate(s) for s in raw["sources"]]


class SourceError(ValueError):
    pass


FACEBOOK_HOSTS = ("facebook.com", "fb.com", "fb.me")


def is_facebook_url(url: str) -> bool:
    """Strony Facebooka: robots.txt zabrania robotom ich pobierania, a treść wymaga zalogowania -
    terminy z nich użytkownik dodaje ręcznie (zrzut ekranu / wklejony opis wydarzenia)."""
    host = (urlparse(url or "").hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in FACEBOOK_HOSTS)


def make_source(url: str, name: str | None = None, organizer: str | None = None,
                enabled: bool = True, base_url: str | None = None,
                detail_link_pattern: str | None = None, max_detail_pages: int | None = None) -> Source:
    """Tworzy źródło z adresu wpisanego przez użytkownika (z walidacją formatu)."""
    url = (url or "").strip()
    if not url:
        raise SourceError("brak adresu")
    if url.startswith("sample://"):
        kind = "sample"
    else:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise SourceError(f"„{url}” nie jest poprawnym adresem http:// lub https://")
        kind = "web"
    pattern = (detail_link_pattern or "").strip() or None
    if pattern and kind != "web":
        raise SourceError("przechodzenie do podstron działa tylko dla stron http(s)")
    if max_detail_pages is not None and not 1 <= max_detail_pages <= 100:
        raise SourceError("limit podstron musi być liczbą od 1 do 100")
    host = urlparse(url).hostname or url.removeprefix("sample://")
    return Source(
        id=hashlib.sha1(url.encode()).hexdigest()[:10],
        name=(name or "").strip() or host.removeprefix("www."),
        organizer=(organizer or "").strip() or None,
        url=url,
        base_url=base_url or (url if kind == "web" else None),
        kind=kind,
        enabled=enabled,
        detail_link_pattern=pattern,
        **({"max_detail_pages": max_detail_pages} if max_detail_pages else {}),
    )
