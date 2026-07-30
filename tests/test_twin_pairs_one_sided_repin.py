"""`verify()` catches an un-repinned edit. It cannot catch the opposite order.

Edit one twin, re-emit, commit: the file and its pin move together, `verify()` is
clean, and the divergence silently becomes the reviewed baseline. The only place it
shows is the pin DIFF, where a reviewer has to notice that one digest moved and its
partner did not. That is a human noticing job, and #623 records four occasions where
the wrong object was filled in precisely because nobody did.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("twin_pairs", REPO / "tools" / "twin_pairs.py")
tp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tp)

PINS = REPO / "twin_pairs.json"


def _pins(public: str, kernel: str | None, twin: str | None = "k/file.py") -> dict:
    return {"pairs": {"VetoWeakBuysTask": {
        "public_module": "renquant_pipeline.x", "public_file": "x.py",
        "kernel_twin_file": twin, "public_sha256": public, "kernel_sha256": kernel}}}


def test_a_kernel_only_repin_is_reported():
    """THE DEFECT, arriving through the pin file instead of past it."""
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "222"))
    assert len(got) == 1
    assert "KERNEL-only change" in got[0]
    assert "#623 R1" in got[0]


def test_a_public_only_repin_is_reported():
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("bbb", "111"))
    assert len(got) == 1
    assert "PUBLIC-only change" in got[0]


def test_both_sides_moving_is_NOT_reported():
    """Anti-vacuity control. A check that flags every pin update would be ignored
    within a week, which is the same as not having it."""
    assert tp.one_sided_repins(_pins("aaa", "111"), _pins("bbb", "222")) == []


def test_no_change_at_all_is_NOT_reported():
    assert tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "111")) == []


def test_a_pair_with_no_kernel_twin_is_skipped():
    """"One-sided" is undefined without a second side. Reporting it would train the
    reader to ignore the output -- three of the nine pinned exports have no twin."""
    assert tp.one_sided_repins(_pins("aaa", None, twin=None),
                               _pins("bbb", None, twin=None)) == []


def test_a_pair_added_or_removed_is_not_a_one_sided_repin():
    """Those are `verify()`'s job (unpinned export / pinned-but-gone). Reporting them
    here too would double-count and blur which check owns which failure."""
    assert tp.one_sided_repins(_pins("aaa", "111"), {"pairs": {}}) == []
    assert tp.one_sided_repins({"pairs": {}}, _pins("aaa", "111")) == []


def test_empty_or_malformed_inputs_do_not_crash():
    for bad in ({}, {"pairs": None}, {"nope": 1}):
        assert tp.one_sided_repins(bad, bad) == []


# --- CLI ------------------------------------------------------------------------

def test_a_missing_baseline_is_FATAL_not_clean(tmp_path, capsys):
    """A baseline that cannot be read must never read as agreement. Same fail-open
    shape codex rejected on the sibling side of the umbrella scan today."""
    rc = tp.main(["--diff-against", str(tmp_path / "nope.json")])
    assert rc == 2
    assert "cannot be shown to agree" in capsys.readouterr().err


def test_an_unreadable_baseline_is_FATAL(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert tp.main(["--diff-against", str(bad)]) == 2
    assert "unreadable" in capsys.readouterr().err


def test_the_cli_exits_1_on_a_one_sided_repin(tmp_path, capsys):
    base = tmp_path / "base.json"
    live = json.loads(PINS.read_text())
    name = next(k for k, v in live["pairs"].items() if v.get("kernel_twin_file"))
    mutated = json.loads(json.dumps(live))
    mutated["pairs"][name]["kernel_sha256"] = "0" * 64
    base.write_text(json.dumps(mutated))
    rc = tp.main(["--diff-against", str(base), "--pins", str(PINS)])
    assert rc == 1
    assert name in capsys.readouterr().out


def test_the_cli_exits_0_when_the_baseline_is_the_committed_file(tmp_path, capsys):
    """Anti-vacuity control for the CLI: comparing the pins to themselves must be
    clean, or the exit-1 test above proves nothing."""
    assert tp.main(["--diff-against", str(PINS), "--pins", str(PINS)]) == 0
    assert "no one-sided re-pins" in capsys.readouterr().out


# --- codex BLOCKER on #232: the contract said "state a reason", CI gave no way to --
# The first version described a one-sided re-pin as legitimate when justified, then
# always exited 1. That is an unconditional prohibition wearing a contract's clothes:
# it would either block real kernel-only work or push an author to touch an unrelated
# twin purely to appease CI. An exception now suppresses the finding -- but only when
# bound to the pair AND both exact digest tuples, so it cannot be pre-written, cannot
# be reused, and expires the moment either side moves again.

def _exc(pair="VetoWeakBuysTask", op="aaa", ok="111", np_="aaa", nk="222",
         reason="kernel-only private helper; public behaviour unchanged"):
    return {"pair": pair, "old_public_sha256": op, "old_kernel_sha256": ok,
            "new_public_sha256": np_, "new_kernel_sha256": nk, "reason": reason}


def test_an_exact_matching_exception_suppresses_the_finding():
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "222"),
                              exceptions=[_exc()])
    assert got == [], got


def test_an_exception_for_a_DIFFERENT_new_digest_does_not_suppress():
    """The change moved on. A justification written for one revision must not carry
    over to the next one, or it becomes a standing licence."""
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "333"),
                              exceptions=[_exc(nk="222")])
    assert len(got) == 2, got            # unjustified re-pin + the now-stale exception
    assert any("KERNEL-only" in g for g in got)
    assert any("STALE exception" in g for g in got)


def test_an_exception_for_a_DIFFERENT_old_digest_does_not_suppress():
    got = tp.one_sided_repins(_pins("aaa", "999"), _pins("aaa", "222"),
                              exceptions=[_exc(ok="111")])
    assert any("KERNEL-only" in g for g in got)


def test_an_exception_for_another_pair_does_not_suppress():
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "222"),
                              exceptions=[_exc(pair="SomethingElseTask")])
    assert any("KERNEL-only" in g for g in got)


def test_an_exception_with_an_empty_reason_is_rejected():
    """A justification with no justification is a rubber stamp."""
    for blank in ("", "   ", None):
        got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "222"),
                                  exceptions=[_exc(reason=blank)])
        assert any("states no reason" in g for g in got), (blank, got)


def test_a_leftover_exception_is_reported_as_STALE():
    """It matches nothing in this diff, so it is silently pre-authorising the NEXT
    one-sided change. That is the failure mode an allowlist always has."""
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "111"),
                              exceptions=[_exc()])
    assert len(got) == 1 and "STALE exception" in got[0]


def test_the_committed_exception_file_is_empty_and_well_formed():
    """Shipping the mechanism with a pre-populated allowlist would defeat it."""
    data = json.loads((REPO / "twin_repin_exceptions.json").read_text())
    assert data["exceptions"] == []
    assert data["schema_version"] == 1


def test_an_unreadable_exception_file_is_FATAL_not_no_exceptions(tmp_path, capsys, monkeypatch):
    bad = tmp_path / "exc.json"
    bad.write_text("{not json")
    monkeypatch.setattr(tp, "EXCEPTIONS", bad)
    rc = tp.main(["--diff-against", str(PINS), "--pins", str(PINS)])
    assert rc == 2
    assert "must not read as" in capsys.readouterr().err
