"""Narzędzie agenta: przygotowanie zrzutów ekranu (np. kalendarza z Facebooka) do wysłania modelowi.

* sprawdza, że plik naprawdę jest obrazem (Pillow), a nie np. skryptem z rozszerzeniem .png,
* odrzuca za duże pliki i obrazy-„bomby” (gigantyczna liczba pikseli),
* zmniejsza obraz do rozsądnej wielkości (mniej tokenów, szybciej w modelu lokalnym) i zapisuje jako JPEG/PNG.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 60_000_000          # ok. 7700 × 7700 - większe obrazy odrzucamy przed dekodowaniem
MAX_SIDE = 2000                  # dłuższy bok po zmniejszeniu (czytelny tekst kalendarza, rozsądna liczba tokenów)
MAX_IMAGES = 6
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP"}


class ImageError(ValueError):
    pass


@dataclass
class PreparedImage:
    data: bytes
    media_type: str
    width: int
    height: int


def prepare_image(raw: bytes, name: str = "obraz") -> PreparedImage:
    if not raw:
        raise ImageError(f"{name}: pusty plik.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageError(f"{name}: plik ma {len(raw) // (1024 * 1024)} MB - maksymalnie "
                         f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            w, h = probe.size
            if fmt not in ALLOWED_FORMATS:
                raise ImageError(f"{name}: nieobsługiwany format {fmt} - użyj PNG lub JPG.")
            if w * h > MAX_PIXELS:
                raise ImageError(f"{name}: obraz jest za duży ({w}×{h} px).")
            probe.verify()  # wykrywa uszkodzone / podrobione pliki
        img = Image.open(io.BytesIO(raw))
        img.load()
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as e:
        raise ImageError(f"{name}: to nie jest poprawny plik obrazu.") from e

    if getattr(img, "n_frames", 1) > 1:  # animowany GIF/WebP - tylko pierwsza klatka
        img.seek(0)
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if fmt == "PNG":  # zrzuty ekranu: PNG zachowuje ostry tekst
        img.save(out, format="PNG", optimize=True)
        media = "image/png"
        if out.tell() > 4 * 1024 * 1024:  # limit API na obraz - wtedy JPEG
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=90)
            media = "image/jpeg"
    else:
        img.save(out, format="JPEG", quality=90)
        media = "image/jpeg"
    return PreparedImage(out.getvalue(), media, img.width, img.height)
