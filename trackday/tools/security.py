"""Podstawowe mechanizmy bezpieczeństwa.

Treść stron jest NIEZAUFANA - może zawierać ukryte instrukcje (pośredni prompt injection).
Warstwy ochrony w PoC:
1. Ukryte elementy HTML są usuwane przed wysłaniem do modelu (``html_clean``).
2. Heurystyczny detektor podejrzanych poleceń (ten moduł) - tylko ostrzega.
3. Treść jest opakowana w znaczniki i opisana w instrukcji systemowej jako dane.
4. Wyjście modelu jest walidowane schematem Pydantic + reguły biznesowe (linki, ceny).
5. Adresy URL są walidowane (tylko http/https, brak adresów prywatnych - ochrona SSRF).
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from urllib.parse import urlparse

INJECTION_PATTERNS: list[str] = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (all |the )?(previous|prior|above)",
    r"zignoruj (wszystkie |wcześniejsze |poprzednie )+(instrukcje|polecenia)",
    r"(nie|nigdy nie) (stosuj|wykonuj) (się do )?(poprzednich|wcześniejszych) (instrukcji|poleceń)",
    r"you are now",
    r"jesteś teraz",
    r"system\s*prompt",
    r"^\s*(system|assistant)\s*:",
    r"(dla|do) (asystent(ów|a)|modeli|agent(ów|a)) (ai|sztucznej inteligencji|językow)",
    r"(for|to) (ai|llm) (assistants?|agents?|models?)",
    r"new instructions",
    r"nowe instrukcje",
    r"</?dokument_niezaufany",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.IGNORECASE | re.MULTILINE)

_CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏‪-‮⁠-⁤]")


def detect_injection(text: str, context_chars: int = 60) -> list[str]:
    """Zwraca fragmenty tekstu wyglądające na polecenia skierowane do AI."""
    hits: list[str] = []
    for m in _INJECTION_RE.finditer(text):
        start = max(0, m.start() - context_chars // 2)
        end = min(len(text), m.end() + context_chars)
        snippet = " ".join(text[start:end].split())
        if snippet not in hits:
            hits.append(snippet)
    return hits


def sanitize_text(text: str, max_chars: int) -> tuple[str, list[str]]:
    """Filtr danych wejściowych: normalizacja Unicode, usunięcie znaków sterujących
    i niewidocznych, limit długości. Zwraca (tekst, ostrzeżenia)."""
    warnings: list[str] = []
    text = unicodedata.normalize("NFKC", text)
    cleaned = _CONTROL_CHARS.sub("", text)
    if len(cleaned) != len(text):
        warnings.append("Usunięto niewidoczne znaki sterujące z treści.")
    if len(cleaned) > max_chars:
        warnings.append(f"Treść skrócona do {max_chars} znaków.")
        cleaned = cleaned[:max_chars]
    return cleaned, warnings


def wrap_untrusted(text: str, source_label: str) -> str:
    """Opakowuje niezaufaną treść w znaczniki; neutralizuje próby ich 'zamknięcia'."""
    safe = re.sub(r"</?\s*dokument_niezaufany[^>]*>", "[usunięty znacznik]", text, flags=re.IGNORECASE)
    label = re.sub(r"[^\w .:/\-]", "", source_label)[:200]
    return f'<dokument_niezaufany zrodlo="{label}">\n{safe}\n</dokument_niezaufany>'


class UnsafeURLError(ValueError):
    pass


def _is_public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_public_url(url: str, resolve: bool = True) -> str:
    """Akceptuje tylko http(s) do publicznych adresów (ochrona przed SSRF)."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError("Dozwolone są tylko adresy http:// i https://")
    if not parsed.hostname:
        raise UnsafeURLError("Brak nazwy hosta w adresie.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Adres nie może zawierać danych logowania.")
    host = parsed.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeURLError("Adresy lokalne są zablokowane.")
    if resolve:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as e:
            raise UnsafeURLError(f"Nie można rozwiązać nazwy hosta: {host}") from e
        for info in infos:
            if not _is_public_ip(info[4][0]):
                raise UnsafeURLError("Adres wskazuje na sieć prywatną/lokalną - zablokowano.")
    return url.strip()


def same_site(url_a: str, url_b: str) -> bool:
    """Czy dwa adresy należą do tej samej domeny (z subdomenami)."""
    a = (urlparse(url_a).hostname or "").removeprefix("www.")
    b = (urlparse(url_b).hostname or "").removeprefix("www.")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)
