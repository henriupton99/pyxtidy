"""Configuration statique et options d'exécution de pyxtidy."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.2.0"

#: Formats photo dont on sait lire (ou tenter de lire) les métadonnées EXIF.
IMAGE_EXTS = frozenset(
    {
        "jpg", "jpeg", "jpe", "png", "heic", "heif", "avif",
        "tif", "tiff", "gif", "webp", "bmp",
        # formats RAW : EXIF lu si Pillow sait les ouvrir, sinon fallback nom/mtime
        "dng", "cr2", "cr3", "nef", "nrw", "arw", "srf", "sr2",
        "raf", "orf", "rw2", "pef", "raw",
    }
)

#: Formats vidéo traités via ffprobe.
VIDEO_EXTS = frozenset({"mov", "mp4", "m4v", "avi", "mkv", "3gp", "mts", "m2ts", "mpg", "mpeg", "webm"})

#: Fichiers annexes générés par iOS / appareils photo : aucune valeur propre, on ne les copie pas.
SIDECAR_EXTS = frozenset({"aae", "xmp", "thm", "lrv", "lrf", "ctg", "sec", "pp3", "on1"})

#: Fichiers système à ignorer silencieusement.
NOISE_NAMES = frozenset({".ds_store", "thumbs.db", "desktop.ini", ".localized"})

#: Dossier de travail créé à la racine de la destination (manifeste + rapports).
WORK_DIRNAME = "_pyxtidy"
MANIFEST_NAME = "manifest.sqlite3"
REPORTS_DIRNAME = "reports"

#: Dossier de repli pour les formats non gérés (option --copy-unsupported).
UNSORTED_DIRNAME = "_non_tries"

#: Suffixe des fichiers en cours de copie (nettoyés au démarrage).
PART_SUFFIX = ".pyxtidy-part"

#: Taille des blocs lus pour l'empreinte rapide (tête + queue du fichier).
FINGERPRINT_CHUNK = 256 * 1024

#: Marge de sécurité exigée sur le disque de destination.
FREE_SPACE_MARGIN = 256 * 1024 * 1024


def find_ffprobe() -> str | None:
    """Localise ffprobe dans le PATH, puis dans les emplacements usuels."""
    found = shutil.which("ffprobe")
    if found:
        return found
    candidates = (
        "/opt/homebrew/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "/usr/bin/ffprobe",
        r"C:\ffmpeg\bin\ffprobe.exe",
    )
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def register_heif() -> bool:
    """Active le support HEIC/HEIF de Pillow si pillow-heif est disponible."""
    try:
        import pillow_heif
    except Exception:
        return False
    try:
        pillow_heif.register_heif_opener()
    except Exception:
        return False
    return True


@dataclass(slots=True)
class Settings:
    """Options d'un import, telles que résolues depuis la ligne de commande."""

    source: Path
    dest: Path
    recursive: bool = True
    dry_run: bool = False
    live_photos: str = "skip-video"  # skip-video | keep-both
    copy_unsupported: bool = False
    index_dest: bool = False
    require_ffprobe: bool = False
    verify: bool = False
    keep_original_name: bool = False
    meta_workers: int = 8
    copy_workers: int = 1
    report_dir: Path | None = None
    verbose: bool = False
    quiet: bool = False
    min_year: int = 1990

    # Renseignés au démarrage par pyxtidy.cli
    ffprobe: str | None = field(default=None)
    heif: bool = field(default=False)

    @property
    def work_dir(self) -> Path:
        return self.dest / WORK_DIRNAME

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / MANIFEST_NAME

    @property
    def reports_dir(self) -> Path:
        return self.report_dir or (self.work_dir / REPORTS_DIRNAME)
