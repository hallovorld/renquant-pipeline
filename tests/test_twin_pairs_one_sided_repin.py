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
    assert len(got) == 2, got            # unjustified re-pin + the superseded entry
    assert any("KERNEL-only" in g for g in got)
    assert any("SUPERSEDED exception" in g for g in got)


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


def test_an_exception_whose_pins_have_moved_on_is_reported_as_SUPERSEDED():
    """It records a move to aaa/222 but the pins read aaa/111, so it no longer
    describes the current state and would pre-authorise a future movement."""
    got = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "111"),
                              exceptions=[_exc()])
    assert len(got) == 1 and "SUPERSEDED exception" in got[0]


def test_a_CURRENT_exception_survives_an_unrelated_later_PR():
    """THE regression codex asked for, and the defect that prompted this rewrite.

    An exception is used on the PR that adds it, and then the next unrelated PR has
    no matching pin movement. The first version reported "not used by this diff" as
    STALE, so a legitimate, justified exception broke CI permanently from the merge
    onward. The check's subject was "did this diff use it" when the question is "does
    it still describe the pins".

    Here: the pins already sit at the exception's `new_*` tuple and nothing moves.
    That is every subsequent PR's baseline, and it must be clean.
    """
    settled = _pins("aaa", "222")          # where the justified re-pin landed
    got = tp.one_sided_repins(settled, settled, exceptions=[_exc()])
    assert got == [], got


def test_the_landing_PR_and_the_NEXT_one_both_pass_with_the_same_entry():
    """The two halves together, since each alone can pass while the pair is broken."""
    e = _exc()
    landing = tp.one_sided_repins(_pins("aaa", "111"), _pins("aaa", "222"),
                                  exceptions=[e])
    assert landing == [], landing
    following = tp.one_sided_repins(_pins("aaa", "222"), _pins("aaa", "222"),
                                    exceptions=[e])
    assert following == [], following


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


# ---------------------------------------------------------------------------
# codex on #232, round 2: the record can be deleted after it lands, and a
# malformed exception file crashed instead of failing closed with a diagnostic.
# ---------------------------------------------------------------------------

def _pins2(public, kernel, name="renquant_pipeline.VetoWeakBuysTask"):
    return {"pairs": {name: {"public_sha256": public, "kernel_sha256": kernel,
                             "kernel_twin_file": "kernel/x.py"}}}


def _exception(name="renquant_pipeline.VetoWeakBuysTask",
               old_pub="a" * 64, old_ker="b" * 64,
               new_pub="a" * 64, new_ker="c" * 64):
    """The REAL schema — flattened keys, as the committed file's `_comment` requires.

    My first version of this helper invented a nested `old`/`new` shape. The guard under
    test read the same invented shape, so both agreed with each other and neither agreed
    with the data: `still_applies` was always False and deleting a live record passed CI
    `[codex on #232]`. A fixture written in the schema of the code under test proves the
    two are consistent, never that either is right.
    """
    return {"pair": name, "reason": "kernel-only comment",
            "old_public_sha256": old_pub, "old_kernel_sha256": old_ker,
            "new_public_sha256": new_pub, "new_kernel_sha256": new_ker}


def test_deleting_a_STILL_APPLICABLE_exception_is_caught():
    """THE hole codex found: base has the record, head deletes it, pins do not move.

    `one_sided_repins` sees an empty exception list and finds nothing to complain
    about — the guard passes because its subject was removed, which is this check's
    own defect one level up.
    """
    base_pins = _pins2("a" * 64, "c" * 64)      # already AT the blessed tuple
    head_pins = _pins2("a" * 64, "c" * 64)      # unchanged
    problems = tp.removed_live_exceptions([_exception()], [], base_pins, head_pins)
    assert len(problems) == 1, problems
    assert "STILL APPLIES" in problems[0]
    assert "VetoWeakBuysTask" in problems[0]


def test_keeping_the_exception_is_silent():
    """ANTI-VACUITY. If this fired on an unchanged file, every PR would be blocked."""
    pins = _pins2("a" * 64, "c" * 64)
    e = _exception()
    assert tp.removed_live_exceptions([e], [e], pins, pins) == []


def test_deleting_an_exception_whose_pair_MOVED_AGAIN_is_allowed():
    """The record has aged out: the pins no longer sit at the tuple it blesses, so it
    justifies nothing and keeping it forever would be the SUPERSEDED case this file
    already reports."""
    base_pins = _pins2("a" * 64, "c" * 64)
    head_pins = _pins2("a" * 64, "d" * 64)      # kernel moved again
    assert tp.removed_live_exceptions([_exception()], [], base_pins, head_pins) == []


def test_deleting_an_exception_while_RE_PINNING_the_same_pair_is_allowed():
    """A PR that re-pins the pair owes a fresh justification, which `one_sided_repins`
    demands on its own. Reporting the deletion too would be a second finding for one
    fact, and would push an author to keep a stale record to silence it."""
    base_pins = _pins2("a" * 64, "c" * 64)
    head_pins = _pins2("a" * 64, "e" * 64)
    assert tp.removed_live_exceptions([_exception()], [], base_pins, head_pins) == []


def test_an_exception_for_a_pair_that_VANISHED_may_be_deleted():
    """The export is gone; nothing is pinned at that tuple any more."""
    base_pins = _pins2("a" * 64, "c" * 64)
    head_pins = {"pairs": {}}
    assert tp.removed_live_exceptions([_exception()], [], base_pins, head_pins) == []


@pytest.mark.parametrize("blob,fragment", [
    ('{"exceptions": 7}', "must be a list"),
    ('{"exceptions": [7]}', "must be an object"),
    ('{"exceptions": ["a"]}', "must be an object"),
    ('{"exceptions": [{}, 3]}', "exceptions[1]"),
    ('{"nope": []}', "no 'exceptions' key"),
    ('7', "must be a list"),
])
def test_a_MALFORMED_exception_file_fails_closed_with_a_DIAGNOSTIC(tmp_path, blob,
                                                                   fragment):
    """Codex: `{"exceptions":[7]}` reached `e.get` and crashed rather than producing
    the fail-closed diagnostic this guard promises. A crash and a diagnostic are both
    non-zero; only one tells the author what to fix, and only one is distinguishable
    from the tool itself being broken."""
    p = tmp_path / "twin_repin_exceptions.json"
    p.write_text(blob, encoding="utf-8")
    with pytest.raises(tp.ExceptionFileError) as ei:
        tp.load_exceptions(p)
    assert fragment in str(ei.value), str(ei.value)


def test_a_WELL_FORMED_exception_file_still_loads(tmp_path):
    """ANTI-VACUITY for the parametrisation above: the validator must not reject the
    shape the repo actually commits."""
    p = tmp_path / "twin_repin_exceptions.json"
    p.write_text(json.dumps({"exceptions": [_exception()]}), encoding="utf-8")
    got = tp.load_exceptions(p)
    assert len(got) == 1 and got[0]["pair"].endswith("VetoWeakBuysTask")
    # and the committed file itself, which is the one that must never trip this
    assert isinstance(tp.load_exceptions(), list)


def test_an_ABSENT_exception_file_is_still_no_exceptions(tmp_path):
    assert tp.load_exceptions(tmp_path / "nope.json") == []


# ---------------------------------------------------------------------------
# codex on #232, round 3: a CLI-level regression whose base exception file uses
# the REAL schema. This is the test that would have caught the dead guard —
# every function-level test I wrote agreed with my invented shape.
# ---------------------------------------------------------------------------

def _write_pins(path, public, kernel, name="renquant_pipeline.VetoWeakBuysTask"):
    path.write_text(json.dumps({"pairs": {name: {
        "public_module": "renquant_pipeline.x", "public_file": "x.py",
        "kernel_twin_file": "kernel/x.py",
        "public_sha256": public, "kernel_sha256": kernel}}}), encoding="utf-8")
    return path


def _run_cli(tmp_path, monkeypatch, *, base_exc, head_exc,
             base_pins=("a" * 64, "c" * 64), head_pins=("a" * 64, "c" * 64)):
    bp = _write_pins(tmp_path / "base_pins.json", *base_pins)
    hp = _write_pins(tmp_path / "head_pins.json", *head_pins)
    be = tmp_path / "base_exceptions.json"
    he = tmp_path / "head_exceptions.json"
    be.write_text(json.dumps({"schema_version": 1, "exceptions": base_exc}), encoding="utf-8")
    he.write_text(json.dumps({"schema_version": 1, "exceptions": head_exc}), encoding="utf-8")
    monkeypatch.setattr(tp, "EXCEPTIONS", he)
    return tp.main(["--pins", str(hp), "--diff-against", str(bp),
                    "--base-exceptions", str(be)])


def test_CLI_catches_a_live_exception_deleted_with_unchanged_pins(tmp_path, monkeypatch,
                                                                  capsys):
    """THE regression. Base carries a real-schema record whose `new_*` tuple equals the
    pins; head deletes it and moves nothing. Must exit non-zero.

    Every function-level test of this guard passed while it was dead, because they and
    the guard shared an invented schema. Only a run through `main()` with a file in the
    schema the repo actually commits could tell the difference.
    """
    rc = _run_cli(tmp_path, monkeypatch, base_exc=[_exception()], head_exc=[])
    out = capsys.readouterr().out
    assert rc == 1, out
    assert "STILL APPLIES" in out
    assert "deleted live exception" in out


def test_CLI_is_silent_when_the_record_is_KEPT(tmp_path, monkeypatch, capsys):
    """ANTI-VACUITY: if this failed too, the guard would block every PR touching the
    file and authors would delete records to get green."""
    e = _exception()
    rc = _run_cli(tmp_path, monkeypatch, base_exc=[e], head_exc=[e])
    assert rc == 0, capsys.readouterr().out


def test_CLI_allows_deleting_a_record_whose_pair_MOVED_AGAIN(tmp_path, monkeypatch,
                                                             capsys):
    """The record has aged out: the pins no longer sit at the tuple it blesses."""
    rc = _run_cli(tmp_path, monkeypatch, base_exc=[_exception()], head_exc=[],
                  base_pins=("a" * 64, "c" * 64), head_pins=("a" * 64, "d" * 64))
    out = capsys.readouterr().out
    assert "STILL APPLIES" not in out


def test_CLI_rejects_a_MALFORMED_base_exception_file(tmp_path, monkeypatch, capsys):
    """The fail-closed path, end to end rather than at the loader."""
    bp = _write_pins(tmp_path / "base_pins.json", "a" * 64, "c" * 64)
    hp = _write_pins(tmp_path / "head_pins.json", "a" * 64, "c" * 64)
    be = tmp_path / "base_exceptions.json"
    be.write_text('{"exceptions": [7]}', encoding="utf-8")
    he = tmp_path / "head_exceptions.json"
    he.write_text(json.dumps({"exceptions": []}), encoding="utf-8")
    monkeypatch.setattr(tp, "EXCEPTIONS", he)
    rc = tp.main(["--pins", str(hp), "--diff-against", str(bp),
                  "--base-exceptions", str(be)])
    assert rc == 2
    assert "must be an object" in capsys.readouterr().err
