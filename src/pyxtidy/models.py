"""Structures de données partagées par les étapes du pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class Kind(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    SIDECAR = "sidecar"
    UNSUPPORTED = "unsupported"


class Status(str, Enum):
    """État final d'un fichier source à l'issue de l'import."""

    PENDING = "pending"
    COPIED = "copied"
    PLANNED = "planned"  # dry-run uniquement
    DUPLICATE_IN_DEST = "duplicate_in_dest"
    DUPLICATE_IN_SOURCE = "duplicate_in_source"
    ALREADY_PRESENT = "already_present"
    SKIPPED_LIVE_VIDEO = "skipped_live_video"
    SKIPPED_SIDECAR = "skipped_sidecar"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_NO_FFPROBE = "skipped_no_ffprobe"
    FAILED_UNREADABLE = "failed_unreadable"
    FAILED_COPY = "failed_copy"
    FAILED_VERIFY = "failed_verify"
    INTERRUPTED = "interrupted"


#: Statuts considérés comme des échecs à faire remonter en tête de rapport.
FAILURE_STATUSES = frozenset(
    {Status.FAILED_UNREADABLE, Status.FAILED_COPY, Status.FAILED_VERIFY}
)

#: Libellés lisibles pour le rapport.
STATUS_LABELS: dict[Status, str] = {
    Status.PENDING: "non traité",
    Status.COPIED: "copié",
    Status.PLANNED: "à copier (simulation)",
    Status.DUPLICATE_IN_DEST: "déjà importé (empreinte connue)",
    Status.DUPLICATE_IN_SOURCE: "doublon dans la source",
    Status.ALREADY_PRESENT: "déjà présent dans la destination",
    Status.SKIPPED_LIVE_VIDEO: "vidéo Live Photo ignorée",
    Status.SKIPPED_SIDECAR: "fichier annexe ignoré",
    Status.SKIPPED_UNSUPPORTED: "format non supporté",
    Status.SKIPPED_NO_FFPROBE: "vidéo ignorée (ffprobe absent)",
    Status.FAILED_UNREADABLE: "illisible",
    Status.FAILED_COPY: "échec de copie",
    Status.FAILED_VERIFY: "échec de vérification",
    Status.INTERRUPTED: "interrompu avant traitement",
}


@dataclass(slots=True)
class SourceFile:
    """Un fichier repéré pendant l'étape de scan, avant tout calcul."""

    path: Path
    rel_path: str
    size: int
    mtime: float
    ext: str
    kind: Kind


@dataclass(slots=True)
class Record:
    """Un fichier source et tout ce que le pipeline en apprend."""

    src: SourceFile
    status: Status = Status.PENDING
    reason: str = ""
    error: str = ""
    meta_warning: str = ""
    taken_at: datetime | None = None
    date_source: str = ""
    fingerprint: str | None = None
    dest: Path | None = None
    dest_rel: str = ""
    live_partner: str = ""

    @property
    def low_confidence_date(self) -> bool:
        """Vrai quand la date vient de la date système du fichier, donc peu fiable."""
        return self.date_source == "mtime"

    @property
    def is_failure(self) -> bool:
        return self.status in FAILURE_STATUSES


@dataclass(slots=True)
class ScanResult:
    """Inventaire complet produit par l'étape 1."""

    records: list[Record] = field(default_factory=list)
    dirs_scanned: int = 0
    noise_skipped: int = 0
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def total_bytes(self) -> int:
        return sum(r.src.size for r in self.records)
