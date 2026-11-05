"""Étape 5 : rapport d'import.

Trois fichiers sont écrits côte à côte dans ``<DEST>/_pyxtidy/reports/`` :

* ``.md``   : rapport lisible, c'est la trace à garder ;
* ``.csv``  : une ligne par fichier source, pour retrouver n'importe quel média ;
* ``.json`` : les mêmes données, exploitables par un script.
"""

from __future__ import annotations

import csv
import json
import platform
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import VERSION, Settings
from .importer import PlanResult
from .models import FAILURE_STATUSES, STATUS_LABELS, Kind, Record, ScanResult, Status
from .progress import human_bytes, human_duration

MAX_ROWS_IN_MARKDOWN = 50


def report_stem(started_at: datetime, dry_run: bool) -> str:
    """Radical commun au journal et aux trois fichiers de rapport."""
    stem = f"import_{started_at:%Y-%m-%d_%H-%M-%S}"
    return stem + "_simulation" if dry_run else stem


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular}" if count <= 1 else f"{count} {plural_form or singular + 's'}"


@dataclass(slots=True)
class RunContext:
    run_id: str
    started_at: datetime
    settings: Settings
    scan: ScanResult
    plan: PlanResult | None = None
    finished_at: datetime | None = None
    copied: int = 0
    copied_bytes: int = 0
    interrupted: bool = False
    meta_duration_s: float = 0.0
    copy_duration_s: float = 0.0
    partials_removed: int = 0
    dest_indexed: int = 0
    report_stem: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()


def _counts(records: list[Record]) -> Counter[Status]:
    return Counter(r.status for r in records)


def _rows(records: list[Record]) -> list[dict]:
    rows = []
    for record in records:
        rows.append(
            {
                "statut": record.status.value,
                "statut_libelle": STATUS_LABELS.get(record.status, record.status.value),
                "source": record.src.rel_path,
                "type": record.src.kind.value,
                "extension": record.src.ext,
                "taille_octets": record.src.size,
                "date_prise_de_vue": fmt_dt(record.taken_at),
                "origine_date": record.date_source,
                "destination": record.dest_rel,
                "empreinte": record.fingerprint or "",
                "live_photo_de": record.live_partner,
                "raison": record.reason,
                "avertissement_metadonnees": record.meta_warning,
                "erreur": record.error,
            }
        )
    return rows


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def build_markdown(ctx: RunContext, records: list[Record]) -> str:
    settings = ctx.settings
    counts = _counts(records)
    total = len(records)
    failures = [r for r in records if r.status in FAILURE_STATUSES]
    low_confidence = [
        r for r in records
        if r.status in (Status.COPIED, Status.PLANNED) and r.low_confidence_date
    ]
    degraded = [
        r for r in records
        if r.meta_warning and r.status in (Status.COPIED, Status.PLANNED)
    ]

    mode = "SIMULATION (aucune écriture)" if settings.dry_run else "IMPORT"
    lines: list[str] = []
    add = lines.append

    add(f"# Rapport d'import pyxtidy — {ctx.started_at:%Y-%m-%d %H:%M:%S}")
    add("")
    if ctx.interrupted:
        add("> **Exécution interrompue** : les fichiers non traités sont listés plus bas. "
            "Relancer la même commande reprendra là où elle s'est arrêtée.")
        add("")
    add(_table(
        ["Paramètre", "Valeur"],
        [
            ["Mode", mode],
            ["Identifiant du run", ctx.run_id],
            ["Source", str(settings.source)],
            ["Destination", str(settings.dest)],
            ["Durée totale", human_duration(ctx.duration_s)],
            ["Version pyxtidy", VERSION],
        ],
    ))
    add("")

    add("## Environnement")
    add("")
    add(_table(
        ["Composant", "État"],
        [
            ["Python", sys.version.split()[0]],
            ["Système", f"{platform.system()} {platform.release()} ({platform.machine()})"],
            ["pillow-heif (HEIC/HEIF)", "disponible" if settings.heif else "ABSENT → dates HEIC dégradées"],
            ["ffprobe (vidéos)", settings.ffprobe or "ABSENT → dates vidéo dégradées"],
            ["Récursif", "oui" if settings.recursive else "non (dossier source uniquement)"],
            ["Live Photos", settings.live_photos],
            ["Vérification SHA-256", "activée" if settings.verify else "désactivée (taille seulement)"],
            ["Threads métadonnées / copie", f"{settings.meta_workers} / {settings.copy_workers}"],
        ],
    ))
    add("")

    add("## 1. Scan de la source")
    add("")
    by_kind = Counter(r.src.kind for r in records)
    add(_table(
        ["Indicateur", "Valeur"],
        [
            ["Dossiers parcourus", ctx.scan.dirs_scanned],
            ["Fichiers inventoriés", total],
            ["Volume total", human_bytes(ctx.scan.total_bytes)],
            ["Photos", by_kind.get(Kind.IMAGE, 0)],
            ["Vidéos", by_kind.get(Kind.VIDEO, 0)],
            ["Fichiers annexes (.aae, .xmp…)", by_kind.get(Kind.SIDECAR, 0)],
            ["Formats non gérés", by_kind.get(Kind.UNSUPPORTED, 0)],
            ["Fichiers système ignorés", ctx.scan.noise_skipped],
            ["Entrées illisibles au scan", len(ctx.scan.unreadable)],
            ["Durée du scan", human_duration(ctx.scan.duration_s)],
        ],
    ))
    add("")
    ext_rows = [
        [f".{ext}", count, human_bytes(size)]
        for ext, (count, size) in sorted(
            _by_ext(records).items(), key=lambda kv: -kv[1][0]
        )
    ]
    if ext_rows:
        add("<details><summary>Détail par extension</summary>")
        add("")
        add(_table(["Extension", "Fichiers", "Volume"], ext_rows))
        add("")
        add("</details>")
        add("")

    add("## 2. Origine des dates")
    add("")
    add("La date détermine le dossier `AAAA/MM` et le nom du fichier. "
        "`mtime` signifie qu'aucune métadonnée exploitable n'a été trouvée : "
        "ces fichiers méritent une vérification.")
    add("")
    date_counts = Counter(r.date_source or "(aucune)" for r in records if r.taken_at)
    add(_table(
        ["Origine", "Fichiers"],
        [[source, count] for source, count in date_counts.most_common()],
    ))
    add("")

    add("## 3. Résultat de l'import")
    add("")
    if ctx.plan is not None:
        planned_bytes = ctx.plan.bytes_to_copy
        rate = ctx.copied_bytes / ctx.copy_duration_s if ctx.copy_duration_s > 0 else 0
        add(_table(
            ["Indicateur", "Valeur"],
            [
                ["Fichiers à copier (plan)", len(ctx.plan.to_copy)],
                ["Volume à copier (plan)", human_bytes(planned_bytes)],
                ["Fichiers copiés", ctx.copied],
                ["Volume copié", human_bytes(ctx.copied_bytes)],
                ["Débit moyen", f"{human_bytes(rate)}/s" if rate else "—"],
                ["Durée métadonnées", human_duration(ctx.meta_duration_s)],
                ["Durée copie", human_duration(ctx.copy_duration_s)],
                ["Espace libre restant (estimé)", human_bytes(max(0, ctx.plan.free_bytes - ctx.copied_bytes))],
            ],
        ))
        add("")

    add("### Répartition par statut")
    add("")
    add(_table(
        ["Statut", "Fichiers", "Volume"],
        [
            [
                STATUS_LABELS.get(status, status.value),
                count,
                human_bytes(sum(r.src.size for r in records if r.status is status)),
            ]
            for status, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
    ))
    add("")

    per_period = _by_period(records)
    if per_period:
        add("### Fichiers écrits par période")
        add("")
        add(_table(
            ["Période", "Fichiers", "Volume"],
            [[period, count, human_bytes(size)] for period, (count, size) in sorted(per_period.items())],
        ))
        add("")

    add("## 4. Anomalies")
    add("")
    if failures:
        add(f"### Échecs ({len(failures)})")
        add("")
        add("Ces fichiers **n'ont pas été importés**. Ils restent intacts dans la source.")
        add("")
        add(_table(
            ["Fichier source", "Taille", "Statut", "Erreur"],
            [
                [r.src.rel_path, human_bytes(r.src.size),
                 STATUS_LABELS.get(r.status, r.status.value), r.error]
                for r in failures[:MAX_ROWS_IN_MARKDOWN]
            ],
        ))
        if len(failures) > MAX_ROWS_IN_MARKDOWN:
            add("")
            add(f"_… {len(failures) - MAX_ROWS_IN_MARKDOWN} autres, liste complète dans le CSV._")
        add("")
    else:
        add("Aucun échec de copie. ✅")
        add("")

    if ctx.scan.unreadable:
        add(f"### Entrées illisibles pendant le scan ({len(ctx.scan.unreadable)})")
        add("")
        add(_table(
            ["Chemin", "Erreur"],
            [[path, err] for path, err in ctx.scan.unreadable[:MAX_ROWS_IN_MARKDOWN]],
        ))
        add("")

    if degraded:
        add(f"### Métadonnées dégradées ({len(degraded)})")
        add("")
        add("Le média a bien été importé, mais la lecture de ses métadonnées a échoué ; "
            "la date vient d'une source de repli.")
        add("")
        add(_table(
            ["Fichier", "Date retenue", "Origine", "Détail"],
            [
                [r.src.rel_path, fmt_dt(r.taken_at), r.date_source, r.meta_warning]
                for r in degraded[:MAX_ROWS_IN_MARKDOWN]
            ],
        ))
        add("")

    if low_confidence:
        add(f"### Dates peu fiables ({len(low_confidence)})")
        add("")
        add("Datés d'après la date de modification du fichier, qui peut être celle "
            "d'une copie plutôt que de la prise de vue.")
        add("")
        add(_table(
            ["Fichier", "Date retenue", "Destination"],
            [
                [r.src.rel_path, fmt_dt(r.taken_at), r.dest_rel]
                for r in low_confidence[:MAX_ROWS_IN_MARKDOWN]
            ],
        ))
        if len(low_confidence) > MAX_ROWS_IN_MARKDOWN:
            add("")
            add(f"_… {len(low_confidence) - MAX_ROWS_IN_MARKDOWN} autres, liste complète dans le CSV._")
        add("")

    unsupported = sorted(
        Counter(
            r.src.ext or "(sans extension)"
            for r in records
            if r.status is Status.SKIPPED_UNSUPPORTED
        ).items(),
        key=lambda kv: -kv[1],
    )
    if unsupported:
        add(f"### Formats non gérés ({plural(sum(c for _, c in unsupported), 'fichier')})")
        add("")
        add("Ces fichiers ont été laissés dans la source. "
            "Utiliser `--copy-unsupported` pour les recopier tels quels dans `_non_tries/`.")
        add("")
        add(_table(["Extension", "Fichiers"], [[f".{ext}", count] for ext, count in unsupported]))
        add("")

    if ctx.notes:
        add("## 5. Notes d'exécution")
        add("")
        for note in ctx.notes:
            add(f"- {note}")
        add("")

    add("---")
    add("")
    add(f"_Généré par pyxtidy {VERSION}. "
        f"Manifeste d'idempotence : `{settings.manifest_path}`._")
    return "\n".join(lines) + "\n"


def _by_ext(records: list[Record]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for record in records:
        key = record.src.ext or "(sans extension)"
        count, size = out[key]
        out[key] = (count + 1, size + record.src.size)
    return dict(out)


def _by_period(records: list[Record]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for record in records:
        if record.status not in (Status.COPIED, Status.PLANNED) or not record.taken_at:
            continue
        key = record.taken_at.strftime("%Y/%m")
        count, size = out[key]
        out[key] = (count + 1, size + record.src.size)
    return dict(out)


def write_reports(ctx: RunContext, records: list[Record]) -> dict[str, Path]:
    """Écrit les trois formats et renvoie leurs chemins."""
    directory = ctx.settings.reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    stem = ctx.report_stem or report_stem(ctx.started_at, ctx.settings.dry_run)

    paths: dict[str, Path] = {}

    md_path = directory / f"{stem}.md"
    md_path.write_text(build_markdown(ctx, records), encoding="utf-8")
    paths["markdown"] = md_path

    rows = _rows(records)
    csv_path = directory / f"{stem}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["statut"])
        writer.writeheader()
        writer.writerows(rows)
    paths["csv"] = csv_path

    json_path = directory / f"{stem}.json"
    payload = {
        "run_id": ctx.run_id,
        "version": VERSION,
        "mode": "dry-run" if ctx.settings.dry_run else "import",
        "started_at": ctx.started_at.isoformat(timespec="seconds"),
        "finished_at": (ctx.finished_at or datetime.now()).isoformat(timespec="seconds"),
        "duration_s": round(ctx.duration_s, 3),
        "interrupted": ctx.interrupted,
        "source": str(ctx.settings.source),
        "dest": str(ctx.settings.dest),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pillow_heif": ctx.settings.heif,
            "ffprobe": ctx.settings.ffprobe,
        },
        "scan": {
            "dirs": ctx.scan.dirs_scanned,
            "files": len(records),
            "bytes": ctx.scan.total_bytes,
            "noise_skipped": ctx.scan.noise_skipped,
            "unreadable": [{"path": p, "error": e} for p, e in ctx.scan.unreadable],
            "duration_s": round(ctx.scan.duration_s, 3),
        },
        "plan": {
            "to_copy": len(ctx.plan.to_copy) if ctx.plan else 0,
            "bytes_to_copy": ctx.plan.bytes_to_copy if ctx.plan else 0,
            "free_bytes": ctx.plan.free_bytes if ctx.plan else 0,
            "dest_fingerprints_computed": ctx.plan.dest_fingerprints_computed if ctx.plan else 0,
        },
        "result": {
            "copied": ctx.copied,
            "copied_bytes": ctx.copied_bytes,
            "meta_duration_s": round(ctx.meta_duration_s, 3),
            "copy_duration_s": round(ctx.copy_duration_s, 3),
            "by_status": {s.value: c for s, c in _counts(records).items()},
            "by_period": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(_by_period(records).items())},
        },
        "notes": ctx.notes,
        "files": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["json"] = json_path

    return paths


def console_summary(ctx: RunContext, records: list[Record], paths: dict[str, Path]) -> str:
    counts = _counts(records)
    failures = sum(counts.get(s, 0) for s in FAILURE_STATUSES)
    lines = ["", "══ Rapport d'import ══"]
    verb = "À copier" if ctx.settings.dry_run else "Copiés"
    lines += [
        f"  {'Source':32s} : {ctx.settings.source}",
        f"  {'Destination':32s} : {ctx.settings.dest}",
        f"  {'Fichiers inventoriés':32s} : {len(records)}  ({human_bytes(ctx.scan.total_bytes)})",
        f"  {verb:32s} : {ctx.copied if not ctx.settings.dry_run else len(ctx.plan.to_copy) if ctx.plan else 0}"
        f"  ({human_bytes(ctx.copied_bytes if not ctx.settings.dry_run else (ctx.plan.bytes_to_copy if ctx.plan else 0))})",
    ]
    skipped_order = [
        Status.DUPLICATE_IN_DEST, Status.ALREADY_PRESENT, Status.DUPLICATE_IN_SOURCE,
        Status.SKIPPED_LIVE_VIDEO, Status.SKIPPED_SIDECAR, Status.SKIPPED_UNSUPPORTED,
        Status.SKIPPED_NO_FFPROBE, Status.INTERRUPTED,
    ]
    for status in skipped_order:
        count = counts.get(status, 0)
        if count:
            lines.append(f"  {STATUS_LABELS[status]:32.32s} : {count}")
    lines.append(f"  {'Échecs':32s} : {failures}" + ("   ⚠️" if failures else ""))
    lines.append(f"  {'Durée':32s} : {human_duration(ctx.duration_s)}")
    lines.append("")
    lines.append(f"  Rapport  : {paths['markdown']}")
    lines.append(f"  Détail   : {paths['csv']}")
    return "\n".join(lines)
