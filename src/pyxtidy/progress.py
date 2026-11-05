"""Affichage de progression minimaliste, sans dépendance externe."""

from __future__ import annotations

import shutil
import sys
import time


def human_bytes(num: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if abs(num) < 1024.0 or unit == "To":
            return f"{num:.0f} {unit}" if unit == "o" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} To"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


class Progress:
    """Barre de progression sur une seule ligne, inerte si la sortie est redirigée."""

    def __init__(self, label: str, total: int = 0, total_bytes: int = 0, enabled: bool = True):
        self.label = label
        self.total = total
        self.total_bytes = total_bytes
        self.done = 0
        self.done_bytes = 0
        self.enabled = enabled and sys.stderr.isatty()
        self.started = time.perf_counter()
        self._last_draw = 0.0

    def advance(self, count: int = 1, size: int = 0) -> None:
        self.done += count
        self.done_bytes += size
        now = time.perf_counter()
        if now - self._last_draw >= 0.15:
            self._last_draw = now
            self.draw()

    def set_total(self, total: int, total_bytes: int = 0) -> None:
        self.total = total
        self.total_bytes = total_bytes

    def draw(self) -> None:
        if not self.enabled:
            return
        elapsed = max(1e-6, time.perf_counter() - self.started)
        parts = [self.label]
        if self.total:
            pct = 100.0 * self.done / self.total
            parts.append(f"{self.done}/{self.total} ({pct:4.1f}%)")
        else:
            parts.append(str(self.done))
        if self.total_bytes:
            rate = self.done_bytes / elapsed
            remaining = (self.total_bytes - self.done_bytes) / rate if rate > 0 else 0
            parts.append(f"{human_bytes(self.done_bytes)}/{human_bytes(self.total_bytes)}")
            parts.append(f"{human_bytes(rate)}/s")
            parts.append(f"reste {human_duration(remaining)}")
        elif self.total and self.done:
            rate = self.done / elapsed
            parts.append(f"{rate:.0f}/s")
        line = "  ".join(parts)
        width = shutil.get_terminal_size((100, 24)).columns - 1
        sys.stderr.write("\r" + line[:width].ljust(width))
        sys.stderr.flush()

    def close(self, summary: str | None = None) -> None:
        if self.enabled:
            sys.stderr.write("\r" + " " * (shutil.get_terminal_size((100, 24)).columns - 1) + "\r")
            sys.stderr.flush()
        if summary:
            print(summary)
