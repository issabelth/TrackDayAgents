"""Zamiana HTML na zwarty tekst dla modelu językowego.

* usuwa skrypty, style, iframe'y i elementy UKRYTE (częsty nośnik prompt injection),
* zachowuje linki w postaci ``tekst [link: https://...]`` (rozwinięte do adresów bezwzględnych),
* tabele zamienia na wiersze ``komórka | komórka``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

_DROP_TAGS = ["script", "style", "noscript", "template", "iframe", "svg", "canvas", "object", "embed", "head"]
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0|opacity\s*:\s*0(?![.\d])", re.I)


@dataclass
class CleanResult:
    text: str
    hidden_text: str
    hidden_elements: int
    title: str | None


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden"):
        return True
    if str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    style = tag.get("style")
    return bool(style and _HIDDEN_STYLE.search(str(style)))


def _strip(soup: BeautifulSoup) -> tuple[list[str], int]:
    """Usuwa skrypty, komentarze i elementy ukryte; zwraca (tekst ukrytych elementów, ich liczba)."""
    for t in soup(_DROP_TAGS):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    hidden_parts: list[str] = []
    count = 0
    for t in [t for t in soup.find_all(True) if _is_hidden(t)]:
        if t.decomposed:
            continue
        hidden_parts.append(t.get_text(" ", strip=True))
        t.decompose()
        count += 1
    return hidden_parts, count


def extract_links(html: str, base_url: str) -> list[str]:
    """Adresy bezwzględne wszystkich WIDOCZNYCH linków strony (bez powtórzeń, w kolejności występowania).

    Linki z ukrytych elementów są pomijane - ukryty link mógłby prowadzić agenta na stronę atakującego.
    """
    soup = BeautifulSoup(html, "html.parser")
    _strip(soup)
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href).split("#", 1)[0]
        seen.setdefault(full, None)
    return list(seen)


def html_to_text(html: str, base_url: str | None = None) -> CleanResult:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    hidden_parts, count = _strip(soup)

    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        label = a.get_text(" ", strip=True)
        if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            full = urljoin(base_url, href) if base_url else href
            a.replace_with(f"{label} [link: {full}]")
        else:
            a.replace_with(label)

    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with("\n" + " | ".join(cells) + "\n")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return CleanResult(text=text, hidden_text="\n".join(p for p in hidden_parts if p),
                       hidden_elements=count, title=title)


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Dzieli długi tekst na fragmenty po liniach (żeby nie ciąć wydarzeń w połowie wiersza)."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line[:max_chars])
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
