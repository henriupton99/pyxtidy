"""Point d'entrée en ligne de commande et orchestration des étapes."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from .config import VERSION, Settings, find_ffprobe, register_heif
from .importer import (
    NotEnoughSpace,
    check_space,
    execute,
    mark_dry_run,
    plan,
    plan_unsupported,
    read_metadata,
)
from .manifest import Manifest
from .metadata import quick_fingerprint
from .models import FAILURE_STATUSES, Kind, Status
from .progress import Progress, human_bytes, human_duration
from .report import RunContext, console_summary, report_stem, write_reports
from .scanner import cleanup_partials, scan_dest, scan_source

log = logging.getLogger("pyxtidy")

EXIT_OK = 0
EXIT_WITH_FAILURES = 1
EXIT_ABORTED = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyxtidy",
        description=(
            "Importe des photos et vidéos vers une destination triée par AAAA/MM, "
            "avec renommage chronologique, détection de doublons et rapport d'import."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples :\n"
            "  pyxtidy ~/Photos /Volumes/harddisk/Photos/tri --dry-run\n"
            "  pyxtidy ~/Photos /Volumes/harddisk/Photos/tri\n"
            "  pyxtidy ~/Photos /Volumes/harddisk/Photos/tri --index-dest   # 1re adoption d'une destination existante\n"
        ),
    )
    parser.add_argument("source", type=Path, help="dossier source des médias")
    parser.add_argument("dest", type=Path, help="dossier de destination (trié par AAAA/MM)")

    parser.add_argument("--dry-run", action="store_true",
                        help="tout analyser et produire le rapport, sans rien écrire")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="ne pas descendre dans les sous-dossiers de la source")
    parser.add_argument("--live-photos", choices=("skip-video", "keep-both"), default="skip-video",
                        help="composante vidéo d'une Live Photo : ignorée (défaut) ou conservée")
    parser.add_argument("--copy-unsupported", action="store_true",
                        help="recopier les formats non gérés dans _non_tries/ au lieu de les laisser")
    parser.add_argument("--index-dest", action="store_true",
                        help="calculer l'empreinte de tous les fichiers déjà présents "
                             "(une fois, pour adopter une destination créée avant pyxtidy 0.2)")
    parser.add_argument("--require-ffprobe", action="store_true",
                        help="ignorer les vidéos si ffprobe est absent, au lieu de les dater "
                             "d'après leur nom ou leur date système")
    parser.add_argument("--verify", action="store_true",
                        help="comparer les SHA-256 source/destination après chaque copie (plus lent)")
    parser.add_argument("--keep-original-name", action="store_true",
                        help="conserver le nom d'origine dans le nom final")
    parser.add_argument("--meta-workers", type=int, default=min(8, (os.cpu_count() or 4) * 2),
                        help="threads de lecture des métadonnées (défaut : %(default)s)")
    parser.add_argument("--copy-workers", type=int, default=1,
                        help="copies en parallèle ; 1 est le plus rapide sur disque externe "
                             "(défaut : %(default)s)")
    parser.add_argument("--report-dir", type=Path, default=None,
                        help="dossier des rapports (défaut : <DEST>/_pyxtidy/reports)")
    parser.add_argument("-v", "--verbose", action="store_true", help="journal détaillé en console")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="masquer la progression et n'afficher que le rapport final")
    parser.add_argument("--version", action="version", version=f"pyxtidy {VERSION}")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        source=args.source.expanduser().resolve(),
        dest=args.dest.expanduser().resolve(),
        recursive=args.recursive,
        dry_run=args.dry_run,
        live_photos=args.live_photos,
        copy_unsupported=args.copy_unsupported,
        index_dest=args.index_dest,
        require_ffprobe=args.require_ffprobe,
        verify=args.verify,
        keep_original_name=args.keep_original_name,
        meta_workers=max(1, args.meta_workers),
        copy_workers=max(1, args.copy_workers),
        report_dir=args.report_dir.expanduser().resolve() if args.report_dir else None,
        verbose=args.verbose,
        quiet=args.quiet,
    )


def validate(settings: Settings) -> list[str]:
    """Contrôles bloquants effectués avant toute lecture de média."""
    errors: list[str] = []
    if not settings.source.exists():
        errors.append(f"source introuvable : {settings.source}")
    elif not settings.source.is_dir():
        errors.append(f"la source n'est pas un dossier : {settings.source}")
    if settings.source == settings.dest:
        errors.append("source et destination sont identiques")
    elif settings.dest.is_relative_to(settings.source):
        errors.append(
            f"la destination est à l'intérieur de la source ({settings.dest}) : "
            "l'import se lirait lui-même"
        )
    elif settings.source.is_relative_to(settings.dest):
        errors.append(
            f"la source est à l'intérieur de la destination ({settings.source}) : "
            "risque de retraiter les fichiers déjà triés"
        )
    if settings.dest.exists() and not settings.dest.is_dir():
        errors.append(f"la destination existe et n'est pas un dossier : {settings.dest}")
    return errors


def setup_logging(settings: Settings, stem: str) -> Path | None:
    logger = logging.getLogger("pyxtidy")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if settings.verbose else logging.ERROR if settings.quiet else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(console)

    try:
        settings.reports_dir.mkdir(parents=True, exist_ok=True)
        log_path = settings.reports_dir / f"{stem}.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        logger.addHandler(handler)
        return log_path
    except OSError as exc:
        print(f"[AVERTISSEMENT] journal désactivé ({exc})", file=sys.stderr)
        return None


def say(settings: Settings, message: str = "") -> None:
    if not settings.quiet:
        print(message)


def run(settings: Settings) -> int:
    started_at = datetime.now()
    run_id = started_at.strftime("%Y-%m-%d_%H-%M-%S") + "_" + uuid.uuid4().hex[:6]

    errors = validate(settings)
    if errors:
        for message in errors:
            print(f"[ERREUR] {message}", file=sys.stderr)
        return EXIT_ABORTED

    settings.dest.mkdir(parents=True, exist_ok=True)
    stem = report_stem(started_at, settings.dry_run)
    if (settings.reports_dir / f"{stem}.md").exists():
        # Deux exécutions dans la même seconde : on ne masque pas le rapport précédent.
        stem = f"{stem}_{run_id.rsplit('_', 1)[-1]}"
    log_path = setup_logging(settings, stem)

    settings.heif = register_heif()
    settings.ffprobe = find_ffprobe()

    say(settings, f"pyxtidy {VERSION}")
    say(settings, f"  source      : {settings.source}")
    say(settings, f"  destination : {settings.dest}")
    if settings.dry_run:
        say(settings, "  mode        : SIMULATION (aucune écriture de média)")
    say(settings, f"  HEIC/HEIF   : {'pillow-heif actif' if settings.heif else 'pillow-heif ABSENT (dates HEIC dégradées)'}")
    say(settings, f"  vidéos      : {settings.ffprobe or 'ffprobe ABSENT (dates vidéo dégradées)'}")
    say(settings)

    notes: list[str] = []
    if not settings.heif:
        notes.append("pillow-heif absent : `uv add pillow-heif` pour des dates HEIC fiables.")
    if not settings.ffprobe:
        notes.append("ffprobe absent : `brew install ffmpeg` pour des dates vidéo fiables.")

    # ---------------------------------------------------------------- étape 1
    partials = 0 if settings.dry_run else cleanup_partials(settings.dest)
    if partials:
        message = f"{partials} fichier(s) temporaire(s) d'une exécution précédente supprimé(s)."
        notes.append(message)
        say(settings, f"  nettoyage   : {message}")
        say(settings)

    bar = Progress("1/5 scan de la source", enabled=not settings.quiet)
    scan = scan_source(
        settings.source, settings.recursive, settings.live_photos,
        progress=lambda n: bar.advance(n - bar.done),
    )
    bar.close()
    records = scan.records
    by_kind = {kind: sum(1 for r in records if r.src.kind is kind) for kind in Kind}
    say(settings,
        f"1/5 scan terminé  : {len(records)} fichiers, {human_bytes(scan.total_bytes)}, "
        f"{scan.dirs_scanned} dossiers, en {human_duration(scan.duration_s)}")
    say(settings,
        f"                    {by_kind[Kind.IMAGE]} photos, {by_kind[Kind.VIDEO]} vidéos, "
        f"{by_kind[Kind.SIDECAR]} annexes, {by_kind[Kind.UNSUPPORTED]} formats non gérés")

    ctx = RunContext(run_id=run_id, started_at=started_at, settings=settings, scan=scan,
                     notes=notes, report_stem=stem, partials_removed=partials)

    if not records:
        ctx.finished_at = datetime.now()
        paths = write_reports(ctx, records)
        print(console_summary(ctx, records, paths))
        return EXIT_OK

    if settings.require_ffprobe and not settings.ffprobe:
        for record in records:
            if record.status is Status.PENDING and record.src.kind is Kind.VIDEO:
                record.status = Status.SKIPPED_NO_FFPROBE
                record.reason = "ffprobe absent et --require-ffprobe demandé"

    # ---------------------------------------------------------------- étape 2
    bar = Progress("2/5 index de la destination", enabled=not settings.quiet)
    dest_index = scan_dest(settings.dest, progress=lambda n: bar.advance(n - bar.done))
    bar.close()

    with Manifest(settings.manifest_path) as manifest:
        manifest.start_run(run_id, str(settings.source), str(settings.dest), VERSION)
        added, changed, removed = manifest.sync_dest_index(dest_index)
        say(settings,
            f"2/5 destination   : {len(dest_index)} fichiers indexés "
            f"(+{added} nouveaux, {changed} modifiés, -{removed} disparus)")
        ctx.dest_indexed = len(dest_index)

        if settings.index_dest:
            pending = manifest.rows_without_fingerprint()
            if pending:
                bar = Progress("    empreintes destination", total=len(pending),
                               enabled=not settings.quiet)
                computed: list[tuple[str, str]] = []
                for rel, size in pending:
                    try:
                        computed.append((quick_fingerprint(settings.dest / rel, size), rel))
                    except OSError as exc:
                        log.warning("empreinte impossible: %s — %s", rel, exc)
                    bar.advance()
                    if len(computed) >= 500:
                        manifest.set_fingerprints(computed)
                        computed.clear()
                manifest.set_fingerprints(computed)
                bar.close()
                say(settings, f"    {len(pending)} empreintes de destination calculées")

        # ------------------------------------------------------------ étape 3
        todo = sum(1 for r in records if r.status is Status.PENDING)
        bar = Progress("3/5 lecture des métadonnées", total=todo, enabled=not settings.quiet)
        ctx.meta_duration_s = read_metadata(records, settings, progress=bar)
        bar.close()
        say(settings,
            f"3/5 métadonnées   : {todo} fichiers analysés en {human_duration(ctx.meta_duration_s)}")

        # ------------------------------------------------------------ étape 4
        bar = Progress("4/5 planification", total=todo, enabled=not settings.quiet)
        try:
            plan_result = plan(records, settings, manifest, dest_index, progress=bar)
        except NotEnoughSpace as exc:
            bar.close()
            print(f"[ERREUR] espace disque insuffisant : {exc}", file=sys.stderr)
            log.error("espace disque insuffisant : %s", exc)
            ctx.notes.append(f"Interrompu avant copie : {exc}")
            ctx.finished_at = datetime.now()
            paths = write_reports(ctx, records)
            print(console_summary(ctx, records, paths))
            return EXIT_ABORTED
        bar.close()
        if settings.copy_unsupported:
            plan_unsupported(records, settings, dest_index, plan_result)
        ctx.plan = plan_result

        say(settings,
            f"4/5 plan          : {len(plan_result.to_copy)} fichiers à copier "
            f"({human_bytes(plan_result.bytes_to_copy)}), "
            f"{human_bytes(plan_result.free_bytes)} libres sur la destination")

        # ------------------------------------------------------------ étape 5
        if settings.dry_run:
            mark_dry_run(plan_result)
            say(settings, "5/5 simulation    : aucune copie effectuée")
        else:
            bar = Progress("5/5 copie", total=len(plan_result.to_copy),
                           total_bytes=plan_result.bytes_to_copy, enabled=not settings.quiet)
            copy_started = time.perf_counter()
            copied, copied_bytes, interrupted = execute(plan_result, settings, manifest, progress=bar)
            ctx.copy_duration_s = time.perf_counter() - copy_started
            bar.close()
            ctx.copied, ctx.copied_bytes, ctx.interrupted = copied, copied_bytes, interrupted
            say(settings,
                f"5/5 copie         : {copied} fichiers, {human_bytes(copied_bytes)}, "
                f"en {human_duration(ctx.copy_duration_s)}")

        ctx.finished_at = datetime.now()
        if log_path:
            ctx.notes.append(f"Journal détaillé : `{log_path}`")
        paths = write_reports(ctx, records)
        failures = sum(1 for r in records if r.status in FAILURE_STATUSES)
        manifest.finish_run(
            run_id, ctx.copied,
            sum(1 for r in records if r.status not in FAILURE_STATUSES and r.status is not Status.COPIED),
            failures, ctx.copied_bytes, str(paths["markdown"]),
        )

    print(console_summary(ctx, records, paths))

    if ctx.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_WITH_FAILURES if failures else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    try:
        return run(settings)
    except KeyboardInterrupt:
        print("\n[INTERROMPU] aucune donnée perdue ; relancer la même commande pour reprendre.",
              file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
