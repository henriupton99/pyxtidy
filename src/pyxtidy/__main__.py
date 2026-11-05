"""Permet ``python -m pyxtidy <SRC_DIR> <DEST_DIR>``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
