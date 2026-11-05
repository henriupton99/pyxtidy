"""Étapes 2 à 4 : métadonnées, planification, copie.

Le pipeline est volontairement séquentiel par étape : on lit toutes les
métadonnées (en parallèle, car c'est de l'attente disque et des appels
ffprobe), puis on planifie l'intégralité des destinations, puis seulement on
écrit. Rien n'est copié avant de savoir combien de fichiers et d'octets sont
concernés, et si la place disque suffit.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import FREE_SPACE_MARGIN, PART_SUFFIX, UNSORTED_DIRNAME, Settings
from .manifest import Manifest
from .metadata import full_sha256, quick_fingerprint, resolve_datetime
from .models import Record, Status
from .progress import Progress, human_bytes

log = logging.getLogger("pyxtidy")


@dataclass(slots=True)
class PlanResult:
    """Ce que la planification a décidé, avant toute écriture."""

    to_copy: list[Record] = field(default_factory=list)
    bytes_to_copy: int = 0
    free_bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    dest_fingerprints_computed: int = 0
    duration_s: float = 0.0


class NotEnoughSpace(RuntimeError):
    pass


# ------------------------------------------------------------ étape 2 : métadonnées

def read_metadata(records: list[Record], settings: Settings, progress: Progress | None = None) -> float:
    """Résout date et empreinte de chaque candidat, en parallèle."""
    started = time.perf_counter()
    todo = [r for r in records if r.status is Status.PENDING]
    if not todo:
        return time.perf_counter() - started

    def work(record: Record) -> Record:
        src = record.src
        try:
            record.fingerprint = quick_fingerprint(src.path, src.size)
        except OSError as exc:
            record.status = Status.FAILED_UNREADABLE
            record.error = f"lecture impossible: {type(exc).__name__}: {exc}"[:300]
            return record
        try:
            taken_at, source, warning = resolve_datetime(
                src.path, src.ext, src.mtime, settings.ffprobe, settings.min_year
            )
        except Exception as exc:  # garde-fou : un média corrompu ne doit pas tout arrêter
            record.status = Status.FAILED_UNREADABLE
            record.error = f"métadonnées illisibles: {type(exc).__name__}: {exc}"[:300]
            return record
        record.taken_at = taken_at
        record.date_source = source
        record.meta_warning = warning
        return record

    workers = max(1, settings.meta_workers)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="meta") as pool:
        for future in as_completed([pool.submit(work, r) for r in todo]):
            record = future.result()
            if record.is_failure:
                log.warning("métadonnées: %s — %s", record.src.rel_path, record.error)
            elif record.meta_warning:
                log.info("métadonnées dégradées: %s — %s",
                         record.src.rel_path, record.meta_warning)
            if progress:
                progress.advance()
    return time.perf_counter() - started


# ------------------------------------------------------------ étape 3 : planification

def _build_name(record: Record, keep_original_name: bool, suffix: int, ext: str) -> str:
    """'YYYY-MM-DD_HH-MM-SS[_<nom original>][-NN].ext'"""
    assert record.taken_at is not None
    name = record.taken_at.strftime("%Y-%m-%d_%H-%M-%S")
    if keep_original_name:
        name = f"{name}_{record.src.path.stem}"
    if suffix:
        name = f"{name}-{suffix:02d}"
    return f"{name}.{ext}"


def plan(
    records: list[Record],
    settings: Settings,
    manifest: Manifest,
    dest_index: dict[str, tuple[int, float]],
    progress: Progress | None = None,
) -> PlanResult:
    """Attribue une destination à chaque candidat et écarte les doublons."""
    started = time.perf_counter()
    result = PlanResult()

    known = manifest.known_fingerprints()
    dest_fp_cache: dict[str, str] = {rel: fp for fp, rel in known.items()}
    new_dest_fingerprints: list[tuple[str, str]] = []
    seen_in_source: dict[str, Record] = {}
    claimed: set[str] = set()

    def dest_fingerprint(rel: str) -> str | None:
        """Empreinte d'un fichier déjà présent, calculée à la demande."""
        if rel in dest_fp_cache:
            return dest_fp_cache[rel]
        size, _mtime = dest_index[rel]
        try:
            fingerprint = quick_fingerprint(settings.dest / rel, size)
        except OSError as exc:
            log.warning("destination illisible: %s — %s", rel, exc)
            return None
        dest_fp_cache[rel] = fingerprint
        new_dest_fingerprints.append((fingerprint, rel))
        result.dest_fingerprints_computed += 1
        return fingerprint

    # Ordre stable : la numérotation des collisions ne change pas d'un run à l'autre.
    candidates = sorted(
        (r for r in records if r.status is Status.PENDING),
        key=lambda r: (r.taken_at or datetime.min, r.src.rel_path),
    )

    for record in candidates:
        src = record.src
        if progress:
            progress.advance()

        if record.fingerprint and record.fingerprint in seen_in_source:
            twin = seen_in_source[record.fingerprint]
            record.status = Status.DUPLICATE_IN_SOURCE
            record.reason = f"identique à {twin.src.rel_path}"
            record.dest_rel = twin.dest_rel
            continue

        if record.fingerprint and record.fingerprint in known:
            record.status = Status.DUPLICATE_IN_DEST
            record.dest_rel = known[record.fingerprint]
            record.reason = f"déjà importé sous {record.dest_rel}"
            continue

        if record.taken_at is None:  # garde-fou : ne devrait pas arriver
            record.status = Status.FAILED_UNREADABLE
            record.error = "aucune date exploitable"
            continue

        folder = Path(record.taken_at.strftime("%Y")) / record.taken_at.strftime("%m")
        placed = False
        for suffix in range(0, 1000):
            name = _build_name(record, settings.keep_original_name, suffix, src.ext)
            rel = str(folder / name)
            if rel in claimed:
                continue
            if rel in dest_index:
                size, _ = dest_index[rel]
                if size == src.size and dest_fingerprint(rel) == record.fingerprint:
                    record.status = Status.ALREADY_PRESENT
                    record.dest_rel = rel
                    record.reason = "même nom, même contenu"
                    placed = True
                    break
                continue  # même nom mais contenu différent -> on numérote
            claimed.add(rel)
            record.dest_rel = rel
            record.dest = settings.dest / rel
            result.to_copy.append(record)
            result.bytes_to_copy += src.size
            if record.fingerprint:
                seen_in_source[record.fingerprint] = record
            placed = True
            break

        if not placed:
            record.status = Status.FAILED_COPY
            record.error = "plus de 1000 collisions de nom sur la même seconde"

    manifest.set_fingerprints(new_dest_fingerprints)
    result.duration_s = time.perf_counter() - started
    return result


def check_space(settings: Settings, plan_result: PlanResult) -> None:
    """Vérifie la place disponible. À appeler quand le plan est complet."""
    try:
        usage = shutil.disk_usage(settings.dest if settings.dest.exists() else settings.dest.parent)
        plan_result.free_bytes = usage.free
    except OSError as exc:
        plan_result.warnings.append(f"espace disque indéterminable: {exc}")
        return

    if plan_result.bytes_to_copy + FREE_SPACE_MARGIN > plan_result.free_bytes:
        raise NotEnoughSpace(
            f"{human_bytes(plan_result.bytes_to_copy)} à copier mais seulement "
            f"{human_bytes(plan_result.free_bytes)} disponibles sur {settings.dest}"
        )


def plan_unsupported(
    records: list[Record],
    settings: Settings,
    dest_index: dict[str, tuple[int, float]],
    plan_result: PlanResult,
) -> None:
    """Ajoute au plan les formats non gérés, à l'identique sous ``_non_tries/``.

    Aucune date n'est devinée : l'arborescence relative de la source est
    conservée, ce qui rend le repêchage manuel évident.
    """
    for record in records:
        if record.status is not Status.SKIPPED_UNSUPPORTED:
            continue
        rel = str(Path(UNSORTED_DIRNAME) / record.src.rel_path)
        if rel in dest_index and dest_index[rel][0] == record.src.size:
            record.status = Status.ALREADY_PRESENT
            record.dest_rel = rel
            record.reason = "déjà recopié dans _non_tries"
            continue
        record.status = Status.PENDING
        record.reason = f"format .{record.src.ext or '(aucun)'} non géré, recopié tel quel"
        record.dest_rel = rel
        record.dest = settings.dest / rel
        plan_result.to_copy.append(record)
        plan_result.bytes_to_copy += record.src.size


# ------------------------------------------------------------ étape 4 : copie

def _copy_atomic(src: Path, dest: Path, verify: bool) -> int:
    """Copie via un fichier temporaire puis renomme : jamais de média tronqué.

    Renvoie la taille écrite.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + PART_SUFFIX)
    try:
        shutil.copyfile(src, part)
        shutil.copystat(src, part, follow_symlinks=False)
        written = part.stat().st_size
        expected = src.stat().st_size
        if written != expected:
            raise OSError(f"taille copiée {written} != source {expected}")
        if verify and full_sha256(src) != full_sha256(part):
            raise OSError("empreinte SHA-256 différente après copie")
        os.replace(part, dest)
        return written
    except BaseException:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        raise


def execute(
    plan_result: PlanResult,
    settings: Settings,
    manifest: Manifest,
    progress: Progress | None = None,
) -> tuple[int, int, bool]:
    """Réalise les copies. Renvoie (copiés, octets, interrompu)."""
    copied = 0
    total_bytes = 0
    interrupted = False

    def do(record: Record) -> tuple[Record, int, Exception | None]:
        try:
            if record.dest is not None and record.dest.exists():
                # Apparu entre la planification et maintenant : on ne l'écrase pas.
                return record, 0, FileExistsError(f"{record.dest_rel} est apparu entre-temps")
            written = _copy_atomic(record.src.path, record.dest, settings.verify)
            return record, written, None
        except Exception as exc:
            return record, 0, exc

    def finish(record: Record, written: int, error: Exception | None) -> None:
        nonlocal copied, total_bytes
        if error is None:
            record.status = Status.COPIED
            copied += 1
            total_bytes += written
            manifest.record_import(
                record.dest_rel, written, record.src.mtime, record.fingerprint,
                record.taken_at, record.date_source, record.src.rel_path,
            )
            log.info("copié %s -> %s", record.src.rel_path, record.dest_rel)
        elif isinstance(error, FileExistsError):
            record.status = Status.ALREADY_PRESENT
            record.reason = str(error)
            log.warning("conflit tardif: %s", error)
        else:
            record.status = (
                Status.FAILED_VERIFY if "SHA-256" in str(error) else Status.FAILED_COPY
            )
            record.error = f"{type(error).__name__}: {error}"[:300]
            log.error("échec %s -> %s : %s", record.src.rel_path, record.dest_rel, record.error)
        if progress:
            progress.advance(size=written)

    workers = max(1, settings.copy_workers)
    try:
        if workers == 1:
            for record in plan_result.to_copy:
                finish(*do(record))
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="copy") as pool:
                futures = {pool.submit(do, r): r for r in plan_result.to_copy}
                for future in as_completed(futures):
                    finish(*future.result())
    except KeyboardInterrupt:
        interrupted = True
        log.warning("interruption demandée : arrêt après %d fichier(s) copié(s)", copied)

    for record in plan_result.to_copy:
        if record.status is Status.PENDING:
            record.status = Status.INTERRUPTED if interrupted else Status.PENDING

    return copied, total_bytes, interrupted


def mark_dry_run(plan_result: PlanResult) -> None:
    for record in plan_result.to_copy:
        record.status = Status.PLANNED
