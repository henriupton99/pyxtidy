"""Lanceur direct : ``python3 ./src/pyxtidy/main.py <SRC_DIR> <DEST_DIR>``.

Conservé pour compatibilité avec la commande documentée dans le README. La
logique vit dans :mod:`pyxtidy.cli`.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # exécution du fichier par son chemin
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyxtidy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
