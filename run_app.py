"""Uruchamia aplikację Streamlit jako zwykły skrypt Pythona.

Przydatne w Visual Studio / VS Code: ustaw ten plik jako element startowy i naciśnij F5
(działa też debugowanie - punkty przerwania w app.py i w pakiecie trackday).
Odpowiednik polecenia:  python -m streamlit run app.py

Jeśli skrypt zostanie uruchomiony interpreterem bez zainstalowanego Streamlita
(np. globalnym Pythonem), uruchomi się ponownie przez .venv projektu.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")

if __name__ == "__main__":
    try:
        from streamlit.web import cli
    except ImportError:
        if VENV_PYTHON.is_file() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
            print(f"Brak Streamlita w {sys.executable} - uruchamiam przez {VENV_PYTHON}")
            print("Wskazówka: wybierz środowisko .venv w Visual Studio, by działało debugowanie.")
            sys.exit(subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]]))
        sys.exit("Brak pakietu streamlit. Zainstaluj zależności: python -m pip install -r requirements.txt")

    sys.argv = ["streamlit", "run", str(ROOT / "app.py"), *sys.argv[1:]]
    sys.exit(cli.main())
