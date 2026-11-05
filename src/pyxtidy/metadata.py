"""Lecture des dates de prise de vue et empreintes de fichiers.

Chaque fonction renvoie une date *naïve en heure locale* : l'arborescence de
destination est organisée par année/mois locaux, donc on normalise tout ici.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .config import FINGERPRINT_CHUNK, IMAGE_EXTS, VIDEO_EXTS

# ---------------------------------------------------------------- empreintes

def quick_fingerprint(path: Path, size: int, chunk: int = FINGERPRINT_CHUNK) -> str:
    """Empreinte rapide : taille + début + fin du fichier.

    Lire la totalité de plusieurs milliers de médias coûte trop cher ; la
    combinaison taille + 2 x 256 Ko suffit à distinguer des photos ou vidéos
    distinctes tout en restant stable d'une exécution à l'autre.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(chunk))
        if size > 2 * chunk:
            handle.seek(-chunk, os.SEEK_END)
            digest.update(handle.read(chunk))
    return digest.hexdigest()


def full_sha256(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------------- dates

_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(?P<y>19\d{2}|20\d{2})[-_.:]?(?P<mo>0[1-9]|1[0-2])[-_.:]?(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[-_.T\s]{0,3}(?:at[-_.\s]{1,2})?"
    r"(?P<h>[01]\d|2[0-3])[-_.:h]?(?P<mi>[0-5]\d)(?:[-_.:s]?(?P<s>[0-5]\d)(?:[.,]?\d{1,6})?)?)?"
    r"(?!\d)"
)


def _plausible(dt: datetime, min_year: int) -> bool:
    now = datetime.now()
    return min_year <= dt.year <= now.year + 1 and dt <= now.replace(year=now.year + 1)


def datetime_from_name(name: str, min_year: int = 1990) -> datetime | None:
    """Extrait une date d'un nom de fichier (IMG_20230810_142536, PXL_, Screenshot…)."""
    for match in _FILENAME_DATE_RE.finditer(name):
        parts = match.groupdict()
        try:
            dt = datetime(
                int(parts["y"]), int(parts["mo"]), int(parts["d"]),
                int(parts["h"] or 0), int(parts["mi"] or 0), int(parts["s"] or 0),
            )
        except ValueError:
            continue
        if _plausible(dt, min_year):
            return dt
    return None


def _parse_exif_stamp(raw: object, subsec: object = None) -> datetime | None:
    """Convertit un horodatage EXIF 'YYYY:MM:DD HH:MM:SS' en datetime naïf.

    EXIF ``DateTimeOriginal`` est déjà l'heure murale du lieu de prise de vue :
    on la conserve telle quelle, c'est elle qui doit apparaître dans le nom du
    fichier et décider du dossier AAAA/MM.
    """
    if not raw:
        return None
    text = str(raw).strip().replace("/", ":")
    if text.startswith("0000") or text in ("", ":"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return None

    if subsec:
        digits = "".join(ch for ch in str(subsec) if ch.isdigit())
        if digits:
            dt = dt.replace(microsecond=int(digits.ljust(6, "0")[:6]))

    return dt


def image_datetime(path: Path) -> tuple[datetime | None, str]:
    """Date EXIF d'une image. Renvoie (date, source) ou (None, '')."""
    from PIL import Image, ExifTags

    with Image.open(path) as img:
        exif = img.getexif()
        if not exif:
            return None, ""
        try:
            sub = exif.get_ifd(ExifTags.IFD.Exif)
        except Exception:
            sub = {}

        candidates = (
            ("exif:DateTimeOriginal", sub.get(ExifTags.Base.DateTimeOriginal.value),
             sub.get(ExifTags.Base.SubsecTimeOriginal.value)),
            ("exif:DateTimeDigitized", sub.get(ExifTags.Base.DateTimeDigitized.value),
             sub.get(ExifTags.Base.SubsecTimeDigitized.value)),
            ("exif:DateTime", exif.get(ExifTags.Base.DateTime.value), None),
        )
        for source, raw, subsec in candidates:
            dt = _parse_exif_stamp(raw, subsec)
            if dt:
                return dt, source
    return None, ""


_QUICKTIME_TAGS = ("com.apple.quicktime.creationdate", "creation_date", "date")


def video_datetime(path: Path, ffprobe: str, timeout: float = 30.0) -> tuple[datetime | None, str]:
    """Date de création d'une vidéo via ffprobe.

    On privilégie ``com.apple.quicktime.creationdate`` qui porte le décalage
    horaire réel, avant ``creation_time`` qui est en UTC.
    """
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_entries", "format_tags:stream_tags",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe a échoué").strip().splitlines()[-1][:300])
    if not proc.stdout.strip():
        return None, ""

    data = json.loads(proc.stdout)
    tag_sets: list[dict] = []
    fmt_tags = data.get("format", {}).get("tags") or {}
    tag_sets.append(fmt_tags)
    for stream in data.get("streams", []) or []:
        tag_sets.append(stream.get("tags") or {})

    lowered: list[dict[str, str]] = [
        {str(k).lower(): str(v) for k, v in tags.items()} for tags in tag_sets
    ]

    for name in _QUICKTIME_TAGS:
        for tags in lowered:
            dt = _parse_iso(tags.get(name))
            if dt:
                return dt, f"video:{name}"
    for tags in lowered:
        dt = _parse_iso(tags.get("creation_time"))
        if dt:
            return dt, "video:creation_time"
    return None, ""


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse une date ISO 8601 (avec ou sans fuseau) vers un local naïf."""
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    # ffprobe produit parfois "+0200" sans deux-points
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    if dt.year < 1990:
        return None
    return dt


def resolve_datetime(
    path: Path,
    ext: str,
    mtime: float,
    ffprobe: str | None,
    min_year: int = 1990,
) -> tuple[datetime, str, str]:
    """Détermine la date d'un média et sa provenance.

    Ordre de priorité : métadonnées intégrées, puis nom de fichier, puis date
    système. Renvoie (date, source, avertissement).
    """
    warning = ""
    if ext in IMAGE_EXTS:
        try:
            dt, source = image_datetime(path)
            if dt and _plausible(dt, min_year):
                return dt, source, warning
        except Exception as exc:
            warning = f"EXIF illisible: {type(exc).__name__}: {exc}"[:200]
    elif ext in VIDEO_EXTS and ffprobe:
        try:
            dt, source = video_datetime(path, ffprobe)
            if dt and _plausible(dt, min_year):
                return dt, source, warning
        except subprocess.TimeoutExpired:
            warning = "ffprobe: délai dépassé"
        except Exception as exc:
            warning = f"ffprobe: {type(exc).__name__}: {exc}"[:200]

    from_name = datetime_from_name(path.name, min_year)
    if from_name:
        return from_name, "filename", warning

    return datetime.fromtimestamp(mtime), "mtime", warning
