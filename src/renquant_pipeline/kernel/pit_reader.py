"""Point-in-time data layer — publication-lag joins (§III.2/§III.3).

Design: renquant-orchestrator
doc/research/2026-06-12-engineering-architecture-deep-plan.md §III.2/§8.
Graduates scripts/engineering/pit_reader.py.

The lookahead-by-default hazard: a feature that joins lagged data (short
interest, fundamentals, earnings) by its NOMINAL date silently uses
numbers that were not yet PUBLISHED on the decision date — the FINRA/E5
short-interest rule and the fundamentals-stale incident class. This layer
makes point-in-time correctness an API GUARANTEE, not a review checklist:

  visible at `as_of`  ⇔  collected_at ≤ as_of
                          AND  date + publication_lag_days ≤ as_of

``pit_append`` records each ingest into an append-only manifest.jsonl;
``pit_visible`` returns only rows visible at a given as_of date. A
backtest at as_of can therefore never read a row that did not exist /
was not published yet — lookahead is impossible by construction.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PitRow:
    date: str                    # nominal data date (e.g. the SI settlement date)
    sha256: str                  # payload content hash (16 hex)
    collected_at: str            # ISO datetime the row was ingested
    publication_lag_days: int    # nominal date + lag = first day it's usable
    payload: str                 # path to the payload file
    rows: int | None = None

    def effective_date(self) -> dt.date:
        return (dt.date.fromisoformat(self.date)
                + dt.timedelta(days=self.publication_lag_days))

    def visible_at(self, as_of: dt.date) -> bool:
        collected = dt.date.fromisoformat(self.collected_at[:10])
        return collected <= as_of and self.effective_date() <= as_of


def _manifest_path(root: Path, source: str) -> Path:
    return Path(root) / source / "manifest.jsonl"


def pit_append(root: Path, source: str, *, date: str, payload_file: Path,
               collected_at: str, publication_lag_days: int = 0,
               rows: int | None = None) -> PitRow:
    """Append one ingest record to ``<root>/<source>/manifest.jsonl``."""
    d = Path(root) / source
    d.mkdir(parents=True, exist_ok=True)
    row = PitRow(
        date=date,
        sha256=hashlib.sha256(Path(payload_file).read_bytes()).hexdigest()[:16],
        collected_at=collected_at,
        publication_lag_days=int(publication_lag_days),
        payload=str(payload_file),
        rows=rows,
    )
    with open(d / "manifest.jsonl", "a") as f:
        f.write(json.dumps(row.__dict__, sort_keys=True) + "\n")
    return row


def _read_manifest(root: Path, source: str) -> list[PitRow]:
    mf = _manifest_path(root, source)
    if not mf.exists():
        return []
    out: list[PitRow] = []
    for line in mf.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append(PitRow(
            date=r["date"], sha256=r["sha256"], collected_at=r["collected_at"],
            publication_lag_days=int(r["publication_lag_days"]),
            payload=r["payload"], rows=r.get("rows")))
    return out


def pit_visible(root: Path, source: str, as_of: str) -> list[PitRow]:
    """Every manifest row visible at ``as_of`` (publication-lag enforced)."""
    asof = dt.date.fromisoformat(as_of)
    return [r for r in _read_manifest(root, source) if r.visible_at(asof)]


def pit_latest(root: Path, source: str, as_of: str) -> PitRow | None:
    """The most recent visible row at ``as_of`` (by effective date, then
    collected_at). None when nothing is visible yet — a backtest must
    treat 'no PIT data published' as missing, never as zero."""
    visible = pit_visible(root, source, as_of)
    if not visible:
        return None
    return max(visible, key=lambda r: (r.effective_date(), r.collected_at))
