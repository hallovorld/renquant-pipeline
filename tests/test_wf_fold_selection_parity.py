"""Contract-parity: live WalkForwardModelLoader vs the canonical shared
fold-selection contract (``renquant_common.walk_forward_fold_selection``,
common#33).

Codex review on common#33: "the live WalkForwardModelLoader still has an
inline implementation and does not import it... include the loader refactor
and a contract-parity test that calls the live loader and shared selector on
the same manifest/boundaries". This file is that test: every case drives
BOTH the live loader path (``WalkForwardModelLoader.entry_as_of`` over a
real JSON manifest on disk) AND the shared selector directly (on structural
fold records built from the same fixture rows, the way an extraction
harness would) and asserts they make identical selections.

Edge cases covered: empty manifests, no-eligible-fold windows, strict-``<``
boundary-date ties (prediction date exactly on a fold's safe-last-label
date), embargo overlaps (``effective_train_cutoff_date`` pre-embargoed folds
and newest-fold-still-inside-lookahead windows), out-of-order manifest rows,
and the degenerate duplicate-``cutoff_date`` tie (where the loader's
historical ``eligible[-1]`` rule is pinned as a regression).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd
import pytest

from renquant_common import walk_forward_fold_selection as shared
from renquant_common.walk_forward_fold_selection import (
    feature_cutoff_date,
    safe_last_label_date,
    select_latest_eligible_fold,
)

from renquant_pipeline.kernel.walk_forward import loader as loader_mod
from renquant_pipeline.kernel.walk_forward.loader import WalkForwardModelLoader


# --------------------------------------------------------------------------
# Fixtures: ONE row spec feeds both sides.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Fold:
    """Structural fold record — what an extraction harness feeds the shared
    selector directly (satisfies ``WalkForwardFoldLike`` duck-typing)."""

    cutoff_date: str
    lookahead_days: int = 0
    effective_train_cutoff_date: str | None = None
    artifact_uri: str = ""


def _row(
    cutoff: str,
    uri: str,
    lookahead: int = 0,
    effective: str | None = None,
) -> dict:
    r = {
        "cutoff_date": cutoff,
        # trained_date only needs to satisfy the manifest invariant
        # trained >= cutoff; it plays no role in eligibility/selection.
        "trained_date": "2026-12-31",
        "artifact_uri": uri,
        "lookahead_days": lookahead,
    }
    if effective is not None:
        r["effective_train_cutoff_date"] = effective
    return r


def _fold(r: dict) -> _Fold:
    return _Fold(
        cutoff_date=r["cutoff_date"],
        lookahead_days=r.get("lookahead_days", 0),
        effective_train_cutoff_date=r.get("effective_train_cutoff_date"),
        artifact_uri=r["artifact_uri"],
    )


def _write_manifest(tmp_path, rows):
    p = tmp_path / "walkforward_manifest.json"
    p.write_text(json.dumps({"retrains": rows}))
    return p


def _both_selections(tmp_path, rows, today):
    """Drive the LIVE loader and the SHARED selector on the same fixture.

    Returns ``(live_uri, shared_uri)`` — ``None`` on either side means "no
    eligible fold" (the loader signals it by raising ValueError per the P1
    contract; the shared selector by returning None).
    """
    live_loader = WalkForwardModelLoader(_write_manifest(tmp_path, rows))
    try:
        live = live_loader.entry_as_of(today)
        live_uri = live.artifact_uri
    except ValueError:
        live_uri = None
    chosen = select_latest_eligible_fold([_fold(r) for r in rows], today)
    shared_uri = None if chosen is None else chosen.artifact_uri
    return live_uri, shared_uri


# --------------------------------------------------------------------------
# The loader must CONSUME the shared contract, not carry a fourth copy.
# --------------------------------------------------------------------------

class TestLoaderConsumesSharedContract:
    def test_loader_imports_are_the_shared_functions(self):
        # Delegation is an import, not another inline mirror: the module-top
        # names in the loader ARE the renquant-common functions.
        assert loader_mod._canonical_feature_cutoff_date is shared.feature_cutoff_date
        assert (
            loader_mod._canonical_safe_last_label_date
            is shared.safe_last_label_date
        )
        assert (
            loader_mod._canonical_select_latest_eligible_fold
            is shared.select_latest_eligible_fold
        )


# --------------------------------------------------------------------------
# Helper-level parity: loader staticmethod adapters == shared functions.
# --------------------------------------------------------------------------

class TestHelperParityGrid:
    @pytest.mark.parametrize("cutoff", ["2023-12-01", "2024-01-02"])  # Fri, Tue
    @pytest.mark.parametrize("lookahead", [0, 1, 60])
    @pytest.mark.parametrize("effective", [None, "", "2023-11-01"])
    def test_feature_cutoff_and_safe_last_label_parity(
        self, tmp_path, cutoff, lookahead, effective,
    ):
        rows = [_row(cutoff, "a.json", lookahead, effective)]
        loader = WalkForwardModelLoader(_write_manifest(tmp_path, rows))
        (entry,) = loader.entries
        assert WalkForwardModelLoader._feature_cutoff_date(entry) == (
            feature_cutoff_date(cutoff, effective)
        )
        assert WalkForwardModelLoader._safe_last_label_date(entry) == (
            safe_last_label_date(cutoff, lookahead, effective)
        )

    def test_business_day_not_calendar_day(self, tmp_path):
        # 2023-12-01 is a Friday: +1 BUSINESS day = Monday 12-04, not the
        # calendar-day Saturday 12-02 — the exact divergence class common#33
        # was created to kill.
        rows = [_row("2023-12-01", "a.json", 1)]
        loader = WalkForwardModelLoader(_write_manifest(tmp_path, rows))
        (entry,) = loader.entries
        got = WalkForwardModelLoader._safe_last_label_date(entry)
        assert got == pd.Timestamp("2023-12-04")
        assert got == safe_last_label_date("2023-12-01", 1)
        assert got != pd.Timestamp("2023-12-01") + pd.Timedelta(days=1)


# --------------------------------------------------------------------------
# Selection parity: entry_as_of vs select_latest_eligible_fold.
# --------------------------------------------------------------------------

class TestSelectionParity:
    def test_empty_manifest(self, tmp_path):
        live_uri, shared_uri = _both_selections(tmp_path, [], "2024-06-01")
        assert live_uri is None
        assert shared_uri is None

    def test_no_eligible_fold_before_all_coverage(self, tmp_path):
        rows = [_row("2023-10-02", "a.json", 60)]
        live_uri, shared_uri = _both_selections(tmp_path, rows, "2023-01-01")
        assert live_uri is None
        assert shared_uri is None

    def test_latest_eligible_wins(self, tmp_path):
        rows = [
            _row("2023-10-02", "a.json", 60),
            _row("2023-10-23", "b.json", 60),
            _row("2024-01-15", "c.json", 60),
        ]
        live_uri, shared_uri = _both_selections(tmp_path, rows, "2024-06-01")
        assert live_uri == shared_uri == "c.json"

    def test_out_of_order_manifest_rows(self, tmp_path):
        # Manifest rows deliberately NOT sorted by cutoff_date. The loader
        # sorts at parse; the shared selector needs no pre-sorting (max by
        # cutoff). Both must land on the same fold.
        rows = [
            _row("2024-01-15", "c.json", 60),
            _row("2023-10-02", "a.json", 60),
            _row("2023-10-23", "b.json", 60),
        ]
        live_uri, shared_uri = _both_selections(tmp_path, rows, "2024-06-01")
        assert live_uri == shared_uri == "c.json"
        loader = WalkForwardModelLoader(_write_manifest(tmp_path, rows))
        cutoffs = [e.cutoff_date for e in loader.entries]
        assert cutoffs == sorted(cutoffs)

    def test_boundary_date_tie_strict_exclusion(self, tmp_path):
        # foldNew's safe-last-label date is Fri 2023-12-01 + 1 BDay =
        # Mon 2023-12-04. A prediction exactly ON that boundary date must
        # NOT select it (strict <) — both sides fall back to foldOld. One
        # day later both promote to foldNew.
        rows = [
            _row("2023-11-01", "old.json", 0),
            _row("2023-12-01", "new.json", 1),
        ]
        on_boundary = _both_selections(tmp_path, rows, "2023-12-04")
        assert on_boundary == ("old.json", "old.json")
        past_boundary = _both_selections(tmp_path, rows, "2023-12-05")
        assert past_boundary == ("new.json", "new.json")

    def test_zero_lookahead_boundary_tie(self, tmp_path):
        # lookahead=0: the safe date IS the cutoff; prediction exactly on
        # the cutoff is excluded by both sides.
        rows = [_row("2023-11-01", "a.json", 0)]
        assert _both_selections(tmp_path, rows, "2023-11-01") == (None, None)
        assert _both_selections(tmp_path, rows, "2023-11-02") == (
            "a.json", "a.json",
        )

    def test_embargo_overlap_effective_cutoff_admits(self, tmp_path):
        # foldB pre-embargoed its labels: effective_train_cutoff_date
        # Mon 2023-07-03 + 20 BDay = Mon 2023-07-31 << plain cutoff_date
        # 2023-09-01 (still in the future at prediction time). Both sides
        # must admit foldB via the EFFECTIVE date — a cutoff_date-only
        # mirror would wrongly exclude it (the renquant-model#64 bug class).
        rows = [
            _row("2023-06-01", "a.json", 0),
            _row("2023-09-01", "b.json", 20, effective="2023-07-03"),
        ]
        live_uri, shared_uri = _both_selections(tmp_path, rows, "2023-08-15")
        assert live_uri == shared_uri == "b.json"
        # Boundary tie ON the embargo edge: safe date is exactly
        # 2023-07-31 → strict < excludes foldB, both fall back to foldA.
        on_edge = _both_selections(tmp_path, rows, "2023-07-31")
        assert on_edge == ("a.json", "a.json")

    def test_newest_fold_inside_embargo_window_older_wins(self, tmp_path):
        # foldB (newest cutoff) is still inside its 60-BDay label embargo at
        # prediction time (safe ≈ 2023-12-08): its window overlaps today, so
        # both sides must skip it and select the older, already-safe foldA.
        rows = [
            _row("2023-07-03", "a.json", 0),
            _row("2023-09-15", "b.json", 60),
        ]
        live_uri, shared_uri = _both_selections(tmp_path, rows, "2023-09-20")
        assert live_uri == shared_uri == "a.json"
        # Once past the embargo, both promote to foldB.
        later = _both_selections(tmp_path, rows, "2023-12-11")
        assert later == ("b.json", "b.json")

    def test_duplicate_cutoff_tie_degenerate(self, tmp_path):
        # DEGENERATE case — well-formed manifests never duplicate
        # cutoff_date. The loader's historical rule is ``eligible[-1]`` over
        # the stably-ascending-sorted entries: LAST manifest row among the
        # tied cutoffs. Pinned here as a regression so the delegation
        # cannot silently flip it.
        rows = [
            _row("2023-06-01", "first.json", 0),
            _row("2023-06-01", "second.json", 0),
        ]
        loader = WalkForwardModelLoader(_write_manifest(tmp_path, rows))
        live = loader.entry_as_of("2023-08-01")
        assert live.artifact_uri == "second.json"
        # Under the loader's calling convention (entries fed in descending
        # order), the shared selector returns the exact same entry —
        # Python's ``max`` keeps the FIRST maximal element, so descending
        # order makes first-max == the historical last-among-ties.
        chosen = select_latest_eligible_fold(
            list(reversed(loader.entries)), "2023-08-01",
        )
        assert chosen is loader.entries[-1]
        assert chosen.artifact_uri == live.artifact_uri
        # KNOWN list-order sensitivity of the shared selector, pinned so a
        # future change is loud: called on ASCENDING order it keeps the
        # first tied row instead. (The common#33 docstring's "last-in-max
        # wins" wording is inaccurate for Python max — flagged on the PR.
        # Unique cutoff dates, the well-formed case, are order-insensitive,
        # as every other test in this class proves.)
        ascending = select_latest_eligible_fold(
            [_fold(r) for r in rows], "2023-08-01",
        )
        assert ascending.artifact_uri == "first.json"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
