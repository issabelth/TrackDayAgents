"""Uruchamia aplikację Streamlit jako zwykły skrypt Pythona.

Przydatne w Visual Studio / VS Code: ustaw ten plik jako element startowy i naciśnij F5
(działa też debugowanie - punkty przerwania w app.py i w pakiecie trackday).
Odpowiednik polecenia:  python -m streamlit run app.py
"""

import sys
from pathlib import Path

from streamlit.web import cli

if __name__ == "__main__":
    app = Path(__file__).resolve().parent / "app.py"
    sys.argv = ["streamlit", "run", str(app), *sys.argv[1:]]
    sys.exit(cli.main())
