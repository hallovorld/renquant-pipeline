"""A WF-gate pass says what it is a statement ABOUT — and admission does not change.

Measured 2026-08-01 across every stamped artifact under
`RenQuant/backtesting/renquant_104/artifacts`: 53 carry a gate block, 53 of 53 have
`candidate_artifact_used=False`, 40 of those carry `passed=True`, and 51 of 53 share ONE
recipe fingerprint. The producer records that qualifier deliberately; this consumer was
discarding it.

The behaviour-invariance tests are the load-bearing half. Turning the qualifier into a
failure would block new buys on every artifact in the tree simultaneously — the fix-wave
rule exists because a compliance change that stops order placement is worse than the gap
it closes.
"""

from __future__ import annotations

import json

import pytest

from renquant_pipeline.kernel import preflight as P


def _stamp(**over):
    wf = {"passed": True, "wf_3cut_sharpe_mean": 1.0, "spy_sharpe_mean": 0.5,
          "strategy_minus_spy_sharpe_mean": 0.5,
          # Without this the "passing run" fixture never reaches the pass path, and the
          # test would assert that the qualifier reaches the bundle on a run that FAILED.
          "n_cuts_beat_spy_sharpe": 3,
          "sanity_regime_ic": {"passed": True}}
    wf.update(over)
    return wf


# --------------------------------------------------------------- the three scopes --
def test_a_manifest_pass_is_labelled_RECIPE_not_artifact():
    """The live shape: 40 artifacts carry exactly this."""
    out = P._gate_evidence_scope(_stamp(artifact_usage={
        "candidate_artifact_used": False, "eval_scope": "walkforward_manifest"}))
    assert out["gate_evidence_scope"] == P.GATE_SCOPE_RECIPE
    assert out["gate_candidate_artifact_used"] is False
    assert out["gate_eval_scope"] == "walkforward_manifest"


def test_a_scored_candidate_is_labelled_ARTIFACT():
    out = P._gate_evidence_scope(_stamp(artifact_usage={
        "candidate_artifact_used": True, "eval_scope": "static_artifact"}))
    assert out["gate_evidence_scope"] == P.GATE_SCOPE_ARTIFACT


def test_a_MISSING_usage_block_is_UNSTATED_which_is_not_recipe_and_not_artifact():
    """Absent is a third fact. Collapsing it into either one asserts something the
    stamp never said."""
    out = P._gate_evidence_scope(_stamp())
    assert out["gate_evidence_scope"] == P.GATE_SCOPE_UNSTATED
    assert out["gate_candidate_artifact_used"] is None


@pytest.mark.parametrize("bad", ["n/a", 7, [], 0, 1, None])
def test_a_NON_BOOLEAN_flag_is_UNSTATED_not_silently_False(bad):
    """`0` and `1` are the trap: truthiness would read them as the boolean they are not.
    An unrecognised value has not established that the artifact went unscored any more
    than that it was scored."""
    out = P._gate_evidence_scope(_stamp(artifact_usage={"candidate_artifact_used": bad}))
    assert out["gate_evidence_scope"] == P.GATE_SCOPE_UNSTATED
    assert out["gate_candidate_artifact_used"] is None


@pytest.mark.parametrize("bad", ["n/a", 7, [], "", 0])
def test_a_MALFORMED_artifact_usage_does_not_raise(bad):
    """`(x or {}).get(...)` is not a guard: a non-empty string is truthy, so the fallback
    never fires and `.get` raises. That shape has produced four separate defects here."""
    assert (P._gate_evidence_scope(_stamp(artifact_usage=bad))["gate_evidence_scope"]
            == P.GATE_SCOPE_UNSTATED)


def test_a_non_string_eval_scope_is_dropped_not_stringified():
    out = P._gate_evidence_scope(_stamp(artifact_usage={
        "candidate_artifact_used": False, "eval_scope": 3}))
    assert out["gate_eval_scope"] is None


# ------------------------------------------------- behaviour invariance (the point) --
def _run(tmp_path, wf, run_mode="full"):
    art = tmp_path / "artifacts" / "prod" / "panel-ltr.alpha158_fund.json"
    art.parent.mkdir(parents=True, exist_ok=True)
    art.write_text(json.dumps({"metadata": {"wf_gate_metadata": wf}}))
    cfg = {"ranking": {"panel_scoring": {
        "kind": "xgb", "artifact_path": "artifacts/prod/panel-ltr.alpha158_fund.json"}}}
    return P._check_wf_gate_metadata(cfg, tmp_path, run_mode)


@pytest.mark.parametrize("usage", [
    None,
    {"candidate_artifact_used": False, "eval_scope": "walkforward_manifest"},
    {"candidate_artifact_used": True, "eval_scope": "static_artifact"},
    "n/a",
])
def test_ADMISSION_IS_UNCHANGED_by_the_scope_qualifier(tmp_path, usage):
    """The whole safety argument. If this ever diverges, the change stopped being a
    surfacing change and became a policy change, which is not mine to make."""
    base = _run(tmp_path, _stamp())
    wf = _stamp() if usage is None else _stamp(artifact_usage=usage)
    got = _run(tmp_path, wf)
    assert (got.ok, got.severity) == (base.ok, base.severity)


def test_a_failed_gate_still_fails_and_now_says_what_it_failed_ABOUT(tmp_path):
    r = _run(tmp_path, _stamp(passed=False, artifact_usage={
        "candidate_artifact_used": False, "eval_scope": "walkforward_manifest"}))
    assert r.ok is False and r.severity == "hard"
    assert r.details["gate_evidence_scope"] == P.GATE_SCOPE_RECIPE


def test_the_qualifier_reaches_the_run_bundle_on_a_PASSING_run(tmp_path):
    """Surfacing that never reaches the bundle is scaffolding, not evidence."""
    r = _run(tmp_path, _stamp(artifact_usage={
        "candidate_artifact_used": False, "eval_scope": "walkforward_manifest"}))
    assert r.ok is True
    assert r.details["gate_evidence_scope"] == P.GATE_SCOPE_RECIPE
    assert r.details["gate_eval_scope"] == "walkforward_manifest"


def test_sell_only_paths_are_untouched(tmp_path):
    r = _run(tmp_path, _stamp(passed=False, artifact_usage={
        "candidate_artifact_used": False}), run_mode="sell_only")
    assert r.ok is True and r.severity == "soft"
