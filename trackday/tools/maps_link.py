"""Link do obiektu w Mapach Google -> współrzędne toru.

Obsługiwane linki:
* pełne adresy z paska przeglądarki, np. ``https://www.google.com/maps/place/Tor+Pozna%C5%84/@52.41,16.80,15z/data=...!3d52.4176!4d16.8089``
  (pinezka obiektu ``!3d…!4d…`` ma pierwszeństwo przed środkiem widoku ``@szer,dł``),
* linki z parametrem ``q`` / ``query`` / ``ll`` / ``destination`` zawierającym współrzędne,
* krótkie linki z przycisku „Udostępnij” (``maps.app.goo.gl/…``, ``goo.gl/maps/…``) - aplikacja odczytuje,
  dokąd przekierowują (tylko nagłówek przekierowania, wyłącznie w obrębie domen Google).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qs, unquote_plus, urlparse

import requests

_F = r"(-?\d{1,3}(?:\.\d+)?)"
_PIN_RX = re.compile(rf"!3d{_F}!4d{_F}")
_AT_RX = re.compile(rf"@{_F},{_F}(?:,|$|/)")
_PAIR_RX = re.compile(rf"^\s*{_F}\s*,\s*\+?\s*{_F}\s*$")
_PATH_RX = re.compile(r"/maps/(?:place|search|dir)/([^/]+)")
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.co"}
MAX_REDIRECTS = 5


class MapsLinkError(ValueError):
    pass


def _is_google_host(host: str) -> bool:
    host = host.lower()
    return host in _SHORT_HOSTS or re.fullmatch(r"(?:[\w-]+\.)*google\.[a-z.]{2,6}", host) is not None


def is_maps_link(text: str) -> bool:
    p = urlparse((text or "").strip())
    return p.scheme in ("http", "https") and _is_google_host(p.hostname or "")


def _valid(lat: float, lon: float) -> tuple[float, float] | None:
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0) else None


def coords_from_url(url: str) -> tuple[float, float] | None:
    """Współrzędne zapisane w pełnym linku Map Google (bez połączenia z siecią) albo None."""
    p = urlparse(url)
    qs = parse_qs(p.query)
    if (p.hostname or "").startswith("consent.") and "continue" in qs:  # ekran zgody na cookies (UE)
        return coords_from_url(qs["continue"][0])
    path = unquote_plus(p.path)
    candidates = [_PIN_RX.search(url), _AT_RX.search(path)]
    for key in ("q", "query", "ll", "center", "destination", "daddr", "sll"):
        candidates += [_PAIR_RX.match(v) for v in qs.get(key, [])]
    candidates += [_PAIR_RX.match(unquote_plus(m)) for m in _PATH_RX.findall(p.path)]
    for m in candidates:
        if m and (c := _valid(float(m.group(1)), float(m.group(2)))):
            return c
    return None


def _next_location(url: str, timeout: float) -> str | None:
    r = requests.get(url, allow_redirects=False, timeout=timeout,
                     headers={"User-Agent": "Mozilla/5.0 (TrackDayAgents)"})
    return r.headers.get("Location") if r.is_redirect else None


def resolve_maps_link(url: str, get_location: Callable[[str], str | None] | None = None,
                      timeout: float = 10) -> tuple[float, float]:
    """Link do obiektu w Mapach Google -> (szerokość, długość). Zgłasza MapsLinkError."""
    url = (url or "").strip()
    if not is_maps_link(url):
        raise MapsLinkError(f"„{url}” - to nie jest link do Map Google")
    get_location = get_location or (lambda u: _next_location(u, timeout))
    for _ in range(MAX_REDIRECTS + 1):
        if c := coords_from_url(url):
            return c
        if (urlparse(url).hostname or "").lower() not in _SHORT_HOSTS:
            break
        try:
            nxt = get_location(url)
        except requests.RequestException as err:
            raise MapsLinkError(f"nie udało się otworzyć krótkiego linku ({type(err).__name__}) - "
                                "sprawdź internet albo wklej pełny link z paska przeglądarki") from err
        if not nxt or not is_maps_link(nxt):
            break
        url = nxt
    raise MapsLinkError("w linku nie ma współrzędnych (tak bywa z linkami z aplikacji na telefon) - otwórz tor "
                        "w Mapach Google w przeglądarce, kliknij jego pinezkę i skopiuj adres z paska przeglądarki "
                        "albo wpisz współrzędne ręcznie")
