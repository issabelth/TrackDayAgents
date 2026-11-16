"""Narzędzie agenta: pobranie treści źródła (strona WWW lub lokalna strona przykładowa)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests

from trackday.config import SAMPLES_DIR, Settings
from trackday.models import Source
from trackday.tools.security import UnsafeURLError, validate_public_url


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    content: str
    final_url: str
    content_type: str


def _read_sample(url: str) -> FetchResult:
    name = url.removeprefix("sample://")
    # tylko nazwa pliku z katalogu data/samples - bez ścieżek względnych
    if "/" in name or "\\" in name or ".." in name or not name:
        raise FetchError("Nieprawidłowa nazwa pliku przykładowego.")
    path = SAMPLES_DIR / name
    if not path.is_file():
        raise FetchError(f"Brak pliku przykładowego: {name}")
    return FetchResult(content=path.read_text(encoding="utf-8"), final_url=url, content_type="text/html")


def _robots_allows(url: str, user_agent: str, timeout: int, cache: dict | None = None) -> bool:
    p = urlparse(url)
    host = f"{p.scheme}://{p.netloc}"
    store = cache
    if store is not None and host in store:
        rp = store[host]
        return True if rp is None else rp.can_fetch(user_agent, url)
    rp: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
    try:
        resp = requests.get(f"{host}/robots.txt", timeout=timeout, headers={"User-Agent": user_agent})
        if resp.status_code >= 400:
            rp = None  # brak robots.txt -> brak zakazu
        else:
            rp.parse(resp.text.splitlines())
    except requests.RequestException:
        rp = None  # błąd sieci -> brak zakazu
    if store is not None:
        store[host] = rp
    return True if rp is None else rp.can_fetch(user_agent, url)


def fetch_source(source: Source, settings: Settings) -> FetchResult:
    if source.url.startswith("sample://"):
        return _read_sample(source.url)
    return fetch_url(source.url, settings)


def fetch_url(url: str, settings: Settings, robots_cache: dict | None = None) -> FetchResult:
    """Pobiera stronę WWW (walidacja adresu, robots.txt, ręczne przekierowania, limit rozmiaru).

    ``robots_cache`` - słownik host -> reguły robots.txt; przy pobieraniu wielu podstron tej samej witryny
    plik robots.txt jest pobierany raz, ale reguły są sprawdzane dla każdego adresu.
    """
    try:
        url = validate_public_url(url)
    except UnsafeURLError as e:
        raise FetchError(str(e)) from e

    if not _robots_allows(url, settings.user_agent, settings.http_timeout_s, robots_cache):
        raise FetchError("Strona nie zezwala na automatyczne pobieranie (robots.txt).")
    headers = {"User-Agent": settings.user_agent, "Accept": "text/html,text/plain;q=0.9"}
    # Ręczna obsługa przekierowań - każdy kolejny adres też jest walidowany (SSRF).
    for _ in range(4):
        try:
            resp = requests.get(url, headers=headers, timeout=settings.http_timeout_s,
                                allow_redirects=False, stream=True)
        except requests.RequestException as e:
            raise FetchError(f"Błąd pobierania strony: {e.__class__.__name__}") from e
        if resp.is_redirect:
            nxt = urljoin(url, resp.headers.get("Location", ""))
            try:
                url = validate_public_url(nxt)
            except UnsafeURLError as e:
                raise FetchError(f"Niebezpieczne przekierowanie: {e}") from e
            continue
        break
    else:
        raise FetchError("Zbyt wiele przekierowań.")

    if resp.status_code >= 400:
        raise FetchError(f"Serwer zwrócił kod {resp.status_code}.")
    ctype = resp.headers.get("Content-Type", "")
    if ctype and not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
        raise FetchError(f"Nieobsługiwany typ treści: {ctype}")

    data = b""
    for chunk in resp.iter_content(64 * 1024):
        data += chunk
        if len(data) > settings.max_page_bytes:
            raise FetchError("Strona jest zbyt duża.")
    encoding = resp.encoding or resp.apparent_encoding or "utf-8"
    return FetchResult(content=data.decode(encoding, errors="replace"), final_url=url, content_type=ctype)
