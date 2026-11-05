"""Étape 1 : inventaire du disque, avant tout calcul.

On parcourt la source une seule fois avec ``os.scandir`` (taille et date
obtenues dans le même appel système que l'entrée de répertoire), on classe
chaque fichier, et on repère les paires Live Photo. Aucune lecture de
métadonnées ni empreinte à ce stade : l'objectif est de savoir exactement ce
qu'il y a à traiter, et combien d'octets, avant de décider quoi que ce soit.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from pathlib import Path

from .config import (
    IMAGE_EXTS,
    NOISE_NAMES,
    PART_SUFFIX,
    SIDECAR_EXTS,
    VIDEO_EXTS,
    WORK_DIRNAME,
)
from .models import Kind, Record, ScanResult, SourceFile, Status


def classify(ext: str) -> Kind:
    if ext in IMAGE_EXTS:
        return Kind.IMAGE
    if ext in VIDEO_EXTS:
        return Kind.VIDEO
    if ext in SIDECAR_EXTS:
        return Kind.SIDECAR
    return Kind.UNSUPPORTED


def _iter_files(root: Path, recursive: bool, on_dir=None):
    """Parcours itératif (sans récursion d'appels) de ``root``."""
    stack = [root]
    while stack:
        current = stack.pop()
        if on_dir:
            on_dir(current)
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            yield None, current, exc
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recursive and entry.name != WORK_DIRNAME:
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                yield entry, current, None
            except OSError as exc:
                yield None, Path(entry.path), exc


def scan_source(
    source: Path,
    recursive: bool = True,
    live_photos: str = "skip-video",
    progress=None,
) -> ScanResult:
    """Construit l'inventaire complet de la source."""
    started = time.perf_counter()
    result = ScanResult()
    dirs = 0

    def count_dir(_path: Path) -> None:
        nonlocal dirs
        dirs += 1

    raw: list[SourceFile] = []
    for entry, where, error in _iter_files(source, recursive, on_dir=count_dir):
        if error is not None:
            result.unreadable.append((str(where), f"{type(error).__name__}: {error}"))
            continue

        name = entry.name
        if name.lower() in NOISE_NAMES or name.endswith(PART_SUFFIX) or name.startswith("._"):
            result.noise_skipped += 1
            continue

        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            result.unreadable.append((entry.path, f"{type(exc).__name__}: {exc}"))
            continue

        ext = os.path.splitext(name)[1].lower().lstrip(".")
        path = Path(entry.path)
        raw.append(
            SourceFile(
                path=path,
                rel_path=os.path.relpath(entry.path, source),
                size=stat.st_size,
                mtime=stat.st_mtime,
                ext=ext,
                kind=classify(ext),
            )
        )
        if progress and len(raw) % 500 == 0:
            progress(len(raw))

    if progress:
        progress(len(raw))

    # Paires Live Photo : même dossier, même radical, une image et une vidéo.
    by_stem: dict[tuple[str, str], dict[Kind, SourceFile]] = defaultdict(dict)
    for item in raw:
        if item.kind in (Kind.IMAGE, Kind.VIDEO):
            key = (str(item.path.parent), item.path.stem.lower())
            by_stem[key].setdefault(item.kind, item)

    live_video_paths: dict[str, str] = {}
    for (_parent, _stem), group in by_stem.items():
        if Kind.IMAGE in group and Kind.VIDEO in group:
            live_video_paths[str(group[Kind.VIDEO].path)] = group[Kind.IMAGE].rel_path

    for item in raw:
        record = Record(src=item)
        if item.kind is Kind.SIDECAR:
            record.status = Status.SKIPPED_SIDECAR
            record.reason = f"fichier annexe .{item.ext}"
        elif item.kind is Kind.UNSUPPORTED:
            record.status = Status.SKIPPED_UNSUPPORTED
            record.reason = f"extension .{item.ext or '(aucune)'} non gérée"
        elif item.size == 0:
            record.status = Status.FAILED_UNREADABLE
            record.error = "fichier vide (0 octet)"
        elif item.kind is Kind.VIDEO and str(item.path) in live_video_paths:
            record.live_partner = live_video_paths[str(item.path)]
            if live_photos == "skip-video":
                record.status = Status.SKIPPED_LIVE_VIDEO
                record.reason = f"composante vidéo de {record.live_partner}"
        result.records.append(record)

    result.dirs_scanned = dirs
    result.duration_s = time.perf_counter() - started
    return result


def scan_dest(dest: Path, progress=None) -> dict[str, tuple[int, float]]:
    """Index des fichiers déjà présents : chemin relatif -> (taille, mtime)."""
    observed: dict[str, tuple[int, float]] = {}
    if not dest.exists():
        return observed
    for entry, where, error in _iter_files(dest, recursive=True):
        if error is not None:
            continue
        if entry.name.lower() in NOISE_NAMES or entry.name.endswith(PART_SUFFIX):
            continue
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        observed[os.path.relpath(entry.path, dest)] = (stat.st_size, stat.st_mtime)
        if progress and len(observed) % 1000 == 0:
            progress(len(observed))
    if progress:
        progress(len(observed))
    return observed


def cleanup_partials(dest: Path) -> int:
    """Supprime les fichiers temporaires laissés par une exécution interrompue."""
    if not dest.exists():
        return 0
    removed = 0
    for entry, _where, error in _iter_files(dest, recursive=True):
        if error is not None or not entry.name.endswith(PART_SUFFIX):
            continue
        try:
            os.unlink(entry.path)
            removed += 1
        except OSError:
            pass
    return removed
