"""Narzędzie agenta: odczyt wariantów wydarzenia (sesja × grupa) ze strony sklepu.

Wiele stron organizatorów to sklepy WooCommerce: jedno wydarzenie = produkt z wariantami
(np. „Godziny wydarzenia” × „Poziom zaawansowania”), a liczba wolnych miejsc pojawia się dopiero
po wybraniu wariantu. Sklep zapisuje jednak WSZYSTKIE warianty w kodzie strony
(``<form class="variations_form" data-product_variations="[...]">``), więc odczytujemy je
bezpośrednio - deterministycznie, bez modelu językowego (pewniej i bez dodatkowych tokenów).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from trackday.models import EventVariant

MAX_VARIANTS = 100
MAX_TEXT = 120

# Rozpoznawanie znaczenia atrybutu po jego nazwie (etykiecie lub identyfikatorze)
_GROUP_RX = re.compile(r"poziom|grup|group|level|klas", re.I)
_SESSION_RX = re.compile(r"godzin|sesj|session|time|hour|termin|dzie[nń]|day", re.I)
_STOCK_RX = re.compile(r"(\d+)")


@dataclass
class VariantsResult:
    variants: list[EventVariant] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _clip(text: str) -> str:
    return " ".join(text.split())[:MAX_TEXT]


def _stock(availability_html: str, is_in_stock: bool) -> tuple[int | None, str | None]:
    text = _clip(BeautifulSoup(availability_html or "", "html.parser").get_text(" ", strip=True))
    if not is_in_stock:
        return 0, text or "brak miejsc"
    m = _STOCK_RX.search(text)
    return (int(m.group(1)) if m else None), (text or None)


def extract_variants(html: str, currency: str = "PLN") -> VariantsResult:
    res = VariantsResult()
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.select("form.variations_form")
    if not forms:
        return res
    if len(forms) > 1:
        res.warnings.append("Na stronie jest kilka formularzy wariantów - odczytano tylko pierwszy.")
    form = forms[0]
    raw = form.get("data-product_variations")
    if not raw or raw.strip() == "false":
        res.warnings.append("Sklep wczytuje warianty dopiero po wyborze (JavaScript) - nie odczytano liczby miejsc.")
        return res
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        res.warnings.append("Nie udało się odczytać danych wariantów ze strony.")
        return res
    if not isinstance(data, list):
        return res

    # Nazwy atrybutów i wartości do wyświetlenia: z etykiet <label> i opcji <select> formularza
    attr_label: dict[str, str] = {}
    value_label: dict[str, dict[str, str]] = {}
    for sel in form.find_all("select"):
        name = sel.get("name") or ""
        if not name.startswith("attribute_"):
            continue
        lab = form.find("label", attrs={"for": sel.get("id")}) if sel.get("id") else None
        attr_label[name] = _clip(lab.get_text(" ", strip=True)) if lab else \
            name.removeprefix("attribute_").removeprefix("pa_").replace("-", " ").capitalize()
        value_label[name] = {o.get("value"): _clip(o.get_text(" ", strip=True))
                             for o in sel.find_all("option") if o.get("value")}

    if len(data) > MAX_VARIANTS:
        res.warnings.append(f"Wariantów jest {len(data)} - odczytano pierwsze {MAX_VARIANTS}.")
    for item in data[:MAX_VARIANTS]:
        if not isinstance(item, dict) or item.get("variation_is_visible") is False:
            continue
        attrs = item.get("attributes") or {}
        options: dict[str, str] = {}
        session = group = None
        for key, slug in attrs.items():
            label = attr_label.get(key) or key.removeprefix("attribute_").removeprefix("pa_").replace("-", " ")
            value = value_label.get(key, {}).get(slug) or (_clip(str(slug)) if slug else "dowolna")
            options[label] = value
            if group is None and _GROUP_RX.search(f"{key} {label}"):
                group = value
            elif session is None and _SESSION_RX.search(f"{key} {label}"):
                session = value
        seats, note = _stock(item.get("availability_html", ""), bool(item.get("is_in_stock", True)))
        price = item.get("display_price")
        res.variants.append(EventVariant(
            options=options, session=session, group=group,
            price=float(price) if isinstance(price, (int, float)) and price >= 0 else None,
            currency=currency if isinstance(price, (int, float)) else None,
            seats_left=seats, availability_note=note,
            purchasable=bool(item.get("is_purchasable", True)) and bool(item.get("variation_is_active", True)),
        ))
    return res
