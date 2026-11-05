"""Index persistant de la destination (SQLite).

Sert deux objectifs :

* idempotence : retrouver qu'un média a déjà été importé, même si son nom de
  destination a changé, grâce à son empreinte ;
* reprise : chaque copie est validée en base au fil de l'eau, donc une
  interruption ne fait pas perdre le travail déjà fait.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel_path    TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    fingerprint TEXT,
    taken_at    TEXT,
    date_source TEXT,
    source_name TEXT,
    imported_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_fingerprint ON files(fingerprint);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT,
    dest        TEXT,
    version     TEXT,
    copied      INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0,
    report      TEXT
);
"""


class Manifest:
    """Accès au manifeste. Un seul thread écrit (l'exécuteur de copies)."""

    def __init__(self, path: Path, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- lecture
    def known_fingerprints(self) -> dict[str, str]:
        """empreinte -> chemin relatif, pour les entrées déjà empreintées."""
        rows = self.conn.execute(
            "SELECT fingerprint, rel_path FROM files WHERE fingerprint IS NOT NULL"
        )
        return {row["fingerprint"]: row["rel_path"] for row in rows}

    def rows_without_fingerprint(self) -> list[tuple[str, int]]:
        rows = self.conn.execute(
            "SELECT rel_path, size FROM files WHERE fingerprint IS NULL"
        )
        return [(row["rel_path"], row["size"]) for row in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])

    def last_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        )

    # ------------------------------------------------------------- écriture
    def sync_dest_index(self, observed: dict[str, tuple[int, float]]) -> tuple[int, int, int]:
        """Aligne la table ``files`` sur le contenu réel de la destination.

        ``observed`` associe chaque chemin relatif à (taille, mtime). Une entrée
        dont la taille ou la date a changé voit son empreinte invalidée ; une
        entrée dont le fichier a disparu est supprimée.
        Renvoie (ajoutés, modifiés, supprimés).
        """
        existing = {
            row["rel_path"]: (row["size"], row["mtime"])
            for row in self.conn.execute("SELECT rel_path, size, mtime FROM files")
        }
        added = changed = 0
        to_insert: list[tuple] = []
        for rel, (size, mtime) in observed.items():
            previous = existing.get(rel)
            if previous is None:
                to_insert.append((rel, size, mtime))
                added += 1
            elif previous[0] != size or abs(previous[1] - mtime) > 1.0:
                to_insert.append((rel, size, mtime))
                changed += 1

        removed = [rel for rel in existing if rel not in observed]

        with self.conn:
            if to_insert:
                self.conn.executemany(
                    "INSERT INTO files(rel_path, size, mtime, fingerprint) VALUES(?,?,?,NULL) "
                    "ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size, "
                    "mtime=excluded.mtime, fingerprint=NULL",
                    to_insert,
                )
            if removed:
                self.conn.executemany(
                    "DELETE FROM files WHERE rel_path=?", [(rel,) for rel in removed]
                )
        return added, changed, len(removed)

    def set_fingerprints(self, pairs: list[tuple[str, str]]) -> None:
        """pairs = [(empreinte, chemin relatif), ...]"""
        if not pairs:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE files SET fingerprint=? WHERE rel_path=?", pairs
            )

    def record_import(
        self,
        rel_path: str,
        size: int,
        mtime: float,
        fingerprint: str | None,
        taken_at: datetime | None,
        date_source: str,
        source_name: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO files(rel_path, size, mtime, fingerprint, taken_at, "
                "date_source, source_name, imported_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(rel_path) DO UPDATE SET size=excluded.size, "
                "mtime=excluded.mtime, fingerprint=excluded.fingerprint, "
                "taken_at=excluded.taken_at, date_source=excluded.date_source, "
                "source_name=excluded.source_name, imported_at=excluded.imported_at",
                (
                    rel_path, size, mtime, fingerprint,
                    taken_at.isoformat() if taken_at else None,
                    date_source, source_name,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def start_run(self, run_id: str, source: str, dest: str, version: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, source, dest, version) "
                "VALUES(?,?,?,?,?)",
                (run_id, datetime.now().isoformat(timespec="seconds"), source, dest, version),
            )

    def finish_run(
        self, run_id: str, copied: int, skipped: int, failed: int, total_bytes: int, report: str
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, copied=?, skipped=?, failed=?, bytes=?, "
                "report=? WHERE run_id=?",
                (
                    datetime.now().isoformat(timespec="seconds"),
                    copied, skipped, failed, total_bytes, report, run_id,
                ),
            )

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    def __enter__(self) -> "Manifest":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
