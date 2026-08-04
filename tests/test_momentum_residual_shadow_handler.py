"""momentum_residual shadow serving handler — GOAL-7 slice 4b (model#197 F-1).

Fixtures are built with the REAL renquant-model writers (train_momentum_artifact
+ append_to_artifact_ledger + the train tool's serialization convention), so
every verification the handler performs is exercised against artifacts the
production job would actually produce — no hand-rolled ledger bytes whose shape
could drift from the package contract.

The suite pins, in order:
  * the registry dispatch (`momentum_residual` registered; train_cmd refuses);
  * the happy path: verified ledger tail → verified dated artifact → per-ticker
    scores + a healthy `shadow_scorer_health.v1` record through the REAL
    ApplyShadowScoringTask path (no registry/resolver monkeypatching);
  * each failure path's DISTINCT fault record (chain tamper, missing dated
    artifact, self-sha mismatch, row-pin mismatch, reconstruction mismatch,
    missing renquant-model dependency);
  * the EMPTY ledger → `not_yet_published` EXPECTED skip (the designed
    PENDING_FIRST_ARTIFACT window, model#197 amendment 2) — not a fault;
  * the certified-then-deleted ledger (#254): a resolver-to-loader deletion is
    a named `ledger_unreadable:` FAULT (STATE_LOAD_FAILED, nothing cached),
    NEVER the not_yet_published skip — that skip is reserved for a
    successfully read, chain-verified empty ledger;
  * the record-don't-raise control: a faulting momentum lane leaves the
    primary candidates byte-identical to a run with no momentum lane at all;
  * the digest-keyed scorer cache: a weekly ledger append busts the cache so a
    long-lived process serves the NEW tail, not the first-loaded one.

Skips (CI parity): pipeline CI checks out common/base-data/artifacts but NOT
renquant-model, so — exactly like the hf_patchtst suites — these tests
importorskip the model packages and run wherever the sibling checkout (or the
[momentum] extra) provides them.
"""
from __future__ import annotations

import builtins
import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

mm = pytest.importorskip(
    "renquant_model_momentum",
    reason="renquant-model distribution not on the path (CI has no model "
           "checkout; locally the sibling checkout provides it)")
pytest.importorskip("renquant_model_common.momentum_features")

import renquant_pipeline.kernel.panel_pipeline.shadow_health as sh
import renquant_pipeline.kernel.panel_pipeline.shadow_scoring as shadow_scoring
from renquant_pipeline.kernel.panel_pipeline import momentum_residual_scorer as mrs
from renquant_pipeline.kernel.panel_pipeline.model_registry import registry
from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (
    MOMENTUM_DATED_ARTIFACT_BASENAME,
    load_momentum_residual_scorer,
)
from renquant_pipeline.kernel.panel_pipeline.shadow_health import (
    STATE_LOAD_FAILED,
    STATE_NOT_YET_PUBLISHED,
    STATE_OK,
    STATUS_EXPECTED_SKIP,
    STATUS_FAULT,
    STATUS_OK,
    ShadowNotYetPublished,
    finalize_shadow_health,
    mark_expected_skip,
    new_shadow_health,
)
from renquant_pipeline.kernel.panel_pipeline.shadow_scoring import (
    ApplyShadowScoringTask,
)

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
CUTOFF = "2026-07-31"
PREV_CUTOFF = "2026-07-24"
RUN_DATE = datetime.date(2026, 8, 2)   # 2 calendar days after CUTOFF → fresh
LEDGER_REL = "artifacts/momentum/momentum_artifact_ledger.jsonl"

# Small v0-domain-valid params so fixtures stay fast; params_version must be
# "v0" (the only version train-side validation dispatches).
PARAMS = {
    "params_version": "v0", "window": 60, "skip": 5, "min_obs": 30,
    "min_features": 2, "names_per_date_floor": 3, "min_side_obs": 5,
}


@pytest.fixture(autouse=True)
def _clear_caches():
    shadow_scoring._SCORER_CACHE.clear()
    sh._DIGEST_CACHE.clear()
    yield
    shadow_scoring._SCORER_CACHE.clear()
    sh._DIGEST_CACHE.clear()


class _SyntheticReaders:
    """MomentumReaders over deterministic synthetic series (seeded RNG)."""

    def __init__(self, universe, asof, *, n_days=90, seed=7):
        idx = pd.bdate_range(end=pd.Timestamp(asof), periods=n_days)
        rng = np.random.default_rng(seed)
        self._returns = {
            t: pd.Series(rng.normal(0.0005 * (i + 1), 0.02, n_days), index=idx)
            for i, t in enumerate([*universe, "SPY"])}
        self._volume = {
            t: pd.Series(rng.integers(1_000, 100_000, n_days).astype(float),
                         index=idx)
            for t in universe}
        self._sectors = {t: ("TECH" if i % 2 else "ENER")
                         for i, t in enumerate(universe)}

    def tr_returns(self, ticker):
        return self._returns.get(ticker)

    def volume(self, ticker):
        return self._volume.get(ticker)

    def market_tr_returns(self):
        return self._returns["SPY"]

    def sector_of(self):
        return dict(self._sectors)

    def read_digests(self):
        return {"synthetic": "0" * 64}


def _build(asof: str, *, seed=7) -> dict:
    """Train one artifact with the REAL core (pure — nothing written)."""
    return mm.train_momentum_artifact(
        asof, UNIVERSE, PARAMS,
        readers=_SyntheticReaders(UNIVERSE, asof, seed=seed))


def _publish_artifact(root: Path, artifact: dict) -> dict:
    """Write dated artifact + ledger row exactly as the train tool does
    (dated JSON beside the ledger; package append)."""
    dated = root / artifact["cutoff_date"] / MOMENTUM_DATED_ARTIFACT_BASENAME
    dated.parent.mkdir(parents=True, exist_ok=True)
    dated.write_text(
        json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8")
    mm.append_to_artifact_ledger(artifact, root / "momentum_artifact_ledger.jsonl")
    return artifact


def _publish(root: Path, asof: str, *, seed=7, mutate=None) -> dict:
    artifact = _build(asof, seed=seed)
    if mutate is not None:
        artifact = mutate(artifact)
    return _publish_artifact(root, artifact)


def _finite_scores(artifact) -> dict[str, float]:
    return {t: float(v) for t, v in artifact["scores"].items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and np.isfinite(v)}


def _ctx(tmp_path, *, shadow_models, candidates):
    cfg = {
        "ranking": {"panel_scoring": {
            "kind": "xgb",
            "shadow_models": shadow_models,
            "shadow_log_mlflow": False,
        }},
        "_strategy_dir": str(tmp_path),
    }
    return SimpleNamespace(
        config=cfg, candidates=candidates,
        # Deliberately NO panel matrix: the momentum lane must not consume one
        # (regression pin for the scores_by_ticker branch ordering).
        _panel_matrix=None,
        today=RUN_DATE, run_id="run-mom", holdings={}, regime="BULL_CALM",
        counters={},
    )


def _candidates(scores: dict[str, float]):
    return [SimpleNamespace(ticker=t, panel_score=float(i + 1), rank_score=None)
            for i, t in enumerate(sorted(scores))]


def _entry():
    return {"name": "momentum_residual_v0_shadow", "kind": "momentum_residual",
            "artifact_path": LEDGER_REL}


def _read_records(tmp_path):
    sink = tmp_path / "logs" / "shadow_scorer_health.jsonl"
    assert sink.exists(), "health JSONL sink was not written"
    return [json.loads(line) for line in sink.read_text().splitlines() if line]


def _momentum_root(tmp_path) -> Path:
    return tmp_path / "artifacts" / "momentum"


def _snapshot(candidates) -> str:
    return json.dumps([[c.ticker, c.panel_score, c.rank_score]
                       for c in candidates], sort_keys=True)


# ── Registry dispatch ──────────────────────────────────────────────────────────

def test_kind_is_registered_and_train_refuses():
    handler = registry.get("momentum_residual")
    assert handler.requires_history is False
    with pytest.raises(NotImplementedError):
        handler.train_cmd(SimpleNamespace())


# ── Loader happy path ──────────────────────────────────────────────────────────

def test_loader_serves_verified_tail_scores_and_metadata(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    expected = _finite_scores(artifact)
    assert len(expected) >= 3, "fixture must produce a scorable cross-section"

    scorer = load_momentum_residual_scorer(root / "momentum_artifact_ledger.jsonl")
    assert scorer.scores_by_ticker is True
    assert scorer.requires_history is False
    got = scorer.score_tickers([*UNIVERSE, "NOT_IN_UNIVERSE"])
    assert set(got.index) == set(expected)          # absent names OMITTED
    for t, v in expected.items():
        assert got[t] == pytest.approx(v, abs=1e-9)

    meta = scorer.metadata
    assert meta["cutoff_date"] == CUTOFF
    # staleness surface = the tail row's cutoff_date, NOT the (embargo-lagged)
    # measured input cutoff — which stays visible under its own name.
    assert meta["effective_train_cutoff_date"] == CUTOFF
    assert meta["artifact_effective_train_cutoff_date"] == artifact[
        "effective_train_cutoff_date"]
    assert meta["trained_date"] == artifact["trained_at_utc"][:10]
    assert meta["config_fingerprint"].startswith("momentum-v0-")
    assert meta["artifact_content_sha256"] == artifact["content_sha256"]
    assert meta.get("lookahead_days") is None       # no borrowed horizon
    assert meta["ledger_row_index"] == 0


def test_loader_serves_tail_row_not_first_row(tmp_path):
    root = _momentum_root(tmp_path)
    _publish(root, PREV_CUTOFF, seed=3)
    tail_artifact = _publish(root, CUTOFF, seed=9)
    scorer = load_momentum_residual_scorer(root / "momentum_artifact_ledger.jsonl")
    assert scorer.metadata["cutoff_date"] == CUTOFF
    assert scorer.metadata["ledger_row_index"] == 1
    expected = _finite_scores(tail_artifact)
    got = scorer.score_tickers(UNIVERSE)
    assert set(got.index) == set(expected)
    for t, v in expected.items():
        assert got[t] == pytest.approx(v, abs=1e-9)


# ── Task happy path (REAL registry + REAL resolver, no monkeypatching) ─────────

def test_task_emits_ok_record_without_panel_matrix(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    cands = _candidates(_finite_scores(artifact))
    assert ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands)) is None

    (rec,) = _read_records(tmp_path)
    assert rec["shadow_name"] == "momentum_residual_v0_shadow"
    assert rec["kind"] == "momentum_residual"
    assert rec["loaded"] is True
    assert rec["artifact_resolved"] is True
    assert rec["artifact_source"] == "strategy_dir"
    assert rec["content_sha256"] == sh.content_digest(
        root / "momentum_artifact_ledger.jsonl")   # identity = the LEDGER bytes
    assert rec["effective_train_cutoff_date"] == CUTOFF
    assert rec["staleness_days"] == (RUN_DATE - datetime.date(2026, 7, 31)).days
    assert rec["config_fingerprint"].startswith("momentum-v0-")
    assert rec["n_scored"] == len(cands)
    assert rec["coverage_frac"] == pytest.approx(1.0)
    assert rec["state"] == STATE_OK
    assert rec["status"] == STATUS_OK
    assert rec["actionable"] is True
    assert rec["reasons"] == []


# ── The designed empty-ledger window → not_yet_published (NOT a fault) ─────────

def test_empty_ledger_raises_not_yet_published(tmp_path):
    root = _momentum_root(tmp_path)
    root.mkdir(parents=True)
    ledger = root / "momentum_artifact_ledger.jsonl"
    ledger.write_text("", encoding="utf-8")        # exists, zero rows
    with pytest.raises(ShadowNotYetPublished):
        load_momentum_residual_scorer(ledger)


def test_task_empty_ledger_emits_expected_skip_record(tmp_path):
    root = _momentum_root(tmp_path)
    root.mkdir(parents=True)
    (root / "momentum_artifact_ledger.jsonl").write_text("", encoding="utf-8")
    cands = _candidates({"AAA": 1.0, "BBB": 2.0})
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands))
    (rec,) = _read_records(tmp_path)
    assert rec["state"] == STATE_NOT_YET_PUBLISHED
    assert rec["status"] == STATUS_EXPECTED_SKIP
    assert rec["actionable"] is True               # designed state, NOT a fault
    assert rec["loaded"] is False
    assert rec["load_error"] is None
    assert "PENDING_FIRST_ARTIFACT" in rec["reasons"][0]


def test_not_yet_published_is_a_marked_expected_skip_state():
    h = new_shadow_health(shadow_name="m", kind="momentum_residual",
                          artifact_path=LEDGER_REL, run_date=RUN_DATE,
                          run_id="r", n_candidates=0)
    mark_expected_skip(h, STATE_NOT_YET_PUBLISHED, "zero rows")
    finalize_shadow_health(h, run_date=RUN_DATE)   # must pass through unchanged
    assert h["status"] == STATUS_EXPECTED_SKIP
    assert h["actionable"] is True


# ── Fail-closed paths: each check faults with its OWN named refusal ────────────

def _run_and_get_fault(tmp_path, cands):
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands))
    (rec,) = _read_records(tmp_path)
    assert rec["state"] == STATE_LOAD_FAILED
    assert rec["status"] == STATUS_FAULT
    assert rec["actionable"] is False
    assert rec["loaded"] is False
    return rec


def test_chain_tamper_faults_naming_the_chain(tmp_path):
    root = _momentum_root(tmp_path)
    _publish(root, PREV_CUTOFF, seed=3)
    artifact = _publish(root, CUTOFF, seed=9)
    ledger = root / "momentum_artifact_ledger.jsonl"
    # Rewrite history: edit a field in row 0 (valid JSON, broken row_sha).
    lines = ledger.read_text().splitlines()
    row0 = json.loads(lines[0])
    row0["n_scored"] = 999_999
    lines[0] = json.dumps(row0, sort_keys=True, separators=(",", ":"))
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert rec["load_error"].startswith("ledger_chain_verification_failed:")


def test_missing_dated_artifact_faults(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    (root / CUTOFF / MOMENTUM_DATED_ARTIFACT_BASENAME).unlink()
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert rec["load_error"].startswith("dated_artifact_missing:")
    assert CUTOFF in rec["load_error"]


def test_edited_artifact_faults_on_self_sha(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    dated = root / CUTOFF / MOMENTUM_DATED_ARTIFACT_BASENAME
    edited = json.loads(dated.read_text())
    first = sorted(edited["scores"])[0]
    edited["scores"][first] = 123.456              # edit WITHOUT re-stamping
    dated.write_text(json.dumps(edited, indent=2, sort_keys=True,
                                allow_nan=False) + "\n", encoding="utf-8")
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert rec["load_error"].startswith("artifact_content_sha_mismatch:")


def test_swapped_artifact_faults_on_row_pin(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    dated = root / CUTOFF / MOMENTUM_DATED_ARTIFACT_BASENAME
    swapped = json.loads(dated.read_text())
    swapped["trained_at_utc"] = "2026-01-01T00:00:00+00:00"
    swapped.pop("content_sha256")
    swapped["content_sha256"] = mm.content_sha256_of(swapped)  # self-consistent
    dated.write_text(json.dumps(swapped, indent=2, sort_keys=True,
                                allow_nan=False) + "\n", encoding="utf-8")
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert rec["load_error"].startswith("ledger_row_artifact_sha_mismatch:")


def test_fabricated_scores_fault_on_reconstruction(tmp_path):
    """Digests verify identity, not validity: an artifact whose stored scores
    are NOT the declared construction over its stored features — published
    self-consistently and honestly ledgered — still refuses to serve."""
    root = _momentum_root(tmp_path)

    def _fabricate(artifact):
        first = sorted(_finite_scores(artifact))[0]
        artifact["scores"][first] = artifact["scores"][first] + 0.5
        artifact.pop("content_sha256")
        artifact["content_sha256"] = mm.content_sha256_of(artifact)
        return artifact

    artifact = _publish(root, CUTOFF, mutate=_fabricate)
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert rec["load_error"].startswith("scores_reconstruction_mismatch:")


def test_missing_model_dependency_faults_naming_it(tmp_path, monkeypatch):
    """The guarded import: without the renquant-model distribution the lane
    fault-records NAMING the dependency + remedy — never a crash."""
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "renquant_model_momentum" or name.startswith(
                "renquant_model_momentum."):
            raise ModuleNotFoundError(
                f"No module named {'renquant_model_momentum'!r}",
                name="renquant_model_momentum")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(artifact)))
    assert "renquant_model_momentum" in rec["load_error"]
    assert "renquant-pipeline[momentum]" in rec["load_error"]


# ── Record-don't-raise: the primary path is byte-identical under a fault ───────

def test_primary_candidates_byte_identical_under_momentum_fault(tmp_path):
    root = _momentum_root(tmp_path)
    artifact = _publish(root, CUTOFF)
    (root / CUTOFF / MOMENTUM_DATED_ARTIFACT_BASENAME).unlink()  # → fault lane
    scores = _finite_scores(artifact)

    faulting = _ctx(tmp_path, shadow_models=[_entry()],
                    candidates=_candidates(scores))
    before = _snapshot(faulting.candidates)
    assert ApplyShadowScoringTask().run(faulting) is None        # never raises
    assert _snapshot(faulting.candidates) == before

    control = _ctx(tmp_path, shadow_models=[],
                   candidates=_candidates(scores))
    ApplyShadowScoringTask().run(control)
    assert _snapshot(control.candidates) == before


# ── Digest-keyed scorer cache: a weekly append must bust it ────────────────────

def test_ledger_append_busts_scorer_cache_within_one_process(tmp_path):
    root = _momentum_root(tmp_path)
    first = _publish(root, PREV_CUTOFF, seed=3)
    cands = _candidates(_finite_scores(first))
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands))
    _publish(root, CUTOFF, seed=9)                # the weekly append
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands))
    rec1, rec2 = _read_records(tmp_path)
    assert rec1["effective_train_cutoff_date"] == PREV_CUTOFF
    assert rec2["effective_train_cutoff_date"] == CUTOFF          # NEW tail served


# ── Single-read identity closure (codex CR on #253: the TOCTOU window) ─────────

def test_loader_metadata_carries_consumed_digest(tmp_path):
    """The loader reports the digest (identity recipe) of the exact ledger
    bytes it consumed — the field the task's divergence check keys on."""
    root = _momentum_root(tmp_path)
    _publish(root, CUTOFF)
    ledger = root / "momentum_artifact_ledger.jsonl"
    scorer = load_momentum_residual_scorer(ledger)
    assert scorer.metadata["consumed_content_sha256"] == sh.content_digest(ledger)


def test_append_between_certify_and_load_recertifies_never_mixes(tmp_path, monkeypatch):
    """Codex's deterministic regression: a weekly append lands BETWEEN the
    task's identity certification and the loader's read. The serve must never
    put the NEW tail under the OLD certified digest — here the benign race
    re-certifies: the record's content_sha256 is the digest of the bytes the
    loader actually consumed (the post-append ledger), and the served tail is
    the post-append cutoff under exactly that identity."""
    root = _momentum_root(tmp_path)
    first = _publish(root, PREV_CUTOFF, seed=3)
    second = _build(CUTOFF, seed=9)          # deterministic, published mid-race
    ledger = root / "momentum_artifact_ledger.jsonl"

    real_resolve = sh.resolve_artifact_identity
    state = {"raced": False}

    def _racing_resolve(ref, **kwargs):
        ident = real_resolve(ref, **kwargs)
        if not state["raced"]:
            state["raced"] = True
            _publish_artifact(root, second)  # the append lands AFTER certification
        return ident

    monkeypatch.setattr(shadow_scoring, "resolve_artifact_identity", _racing_resolve)
    cands = _candidates({t: v for t, v in _finite_scores(first).items()
                         if t in _finite_scores(second)})
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()], candidates=cands))

    (rec,) = _read_records(tmp_path)
    post_append_digest = sh.content_digest(ledger)
    assert rec["loaded"] is True
    # NEVER new-bytes-under-old-identity: the certified digest is the
    # POST-append ledger — the exact bytes the loader consumed.
    assert rec["content_sha256"] == post_append_digest
    assert rec["effective_train_cutoff_date"] == CUTOFF   # the NEW tail
    # The cache binds the scorer to the digest that was actually certified.
    keys = list(shadow_scoring._SCORER_CACHE)
    assert [k for k in keys if k[0] == "momentum_residual"] == [
        ("momentum_residual", str(ledger.resolve()), post_append_digest)]


def test_identity_divergence_without_recertification_faults(tmp_path, monkeypatch):
    """When re-certification cannot confirm the consumed bytes (the resolver
    keeps certifying the STALE identity), the lane must refuse — a FAULT
    naming the divergence, nothing cached, never served."""
    root = _momentum_root(tmp_path)
    first = _publish(root, PREV_CUTOFF, seed=3)
    ledger = root / "momentum_artifact_ledger.jsonl"
    stale = sh.resolve_artifact_identity(LEDGER_REL, strategy_dir=tmp_path)
    assert stale.resolved
    _publish(root, CUTOFF, seed=9)           # the file moves on; the stub does not

    monkeypatch.setattr(shadow_scoring, "resolve_artifact_identity",
                        lambda *a, **k: stale)
    ApplyShadowScoringTask().run(
        _ctx(tmp_path, shadow_models=[_entry()],
             candidates=_candidates(_finite_scores(first))))

    (rec,) = _read_records(tmp_path)
    assert rec["loaded"] is False
    assert rec["state"] == STATE_LOAD_FAILED
    assert rec["status"] == STATUS_FAULT
    assert rec["actionable"] is False
    assert rec["load_error"].startswith("artifact_identity_divergence:")
    # The record certifies only what WAS certified — the stale digest — and
    # the divergent scorer is never cached under any key.
    assert rec["content_sha256"] == stale.content_sha256
    assert rec["content_sha256"] != sh.content_digest(ledger)
    assert not [k for k in shadow_scoring._SCORER_CACHE
                if k[0] == "momentum_residual"]


# ── Certified-then-deleted ledger: a FAULT, never the designed skip (#254) ─────

def test_loader_missing_ledger_is_a_named_fault_not_a_skip(tmp_path):
    """Regression (#254, from #253's post-merge review): the loader only ever
    sees ALREADY-RESOLVED paths (the task gates on ``identity.resolved``
    first), so a FileNotFoundError from its single read means the file
    disappeared between certification and use — a named ``ledger_unreadable:``
    refusal, never ShadowNotYetPublished."""
    root = _momentum_root(tmp_path)
    _publish(root, CUTOFF)
    ledger = root / "momentum_artifact_ledger.jsonl"
    ledger.unlink()                                # certified path, then gone
    with pytest.raises(ValueError, match=r"^ledger_unreadable:"):
        load_momentum_residual_scorer(ledger)


def test_ledger_deleted_between_certify_and_load_faults_load_failed(
        tmp_path, monkeypatch):
    """Codex's deterministic resolver-to-loader deletion regression (#254):
    the task resolves + certifies the ledger, the file is deleted before the
    loader's read. NOT the designed pre-first-publish window — the record
    must be a STATE_LOAD_FAILED fault with a named load_error, and nothing
    may be cached for the lane."""
    root = _momentum_root(tmp_path)
    first = _publish(root, CUTOFF)
    ledger = root / "momentum_artifact_ledger.jsonl"

    real_resolve = sh.resolve_artifact_identity
    state = {"raced": False}

    def _deleting_resolve(ref, **kwargs):
        ident = real_resolve(ref, **kwargs)
        if not state["raced"]:
            state["raced"] = True
            ledger.unlink()      # the deletion lands AFTER certification
        return ident

    monkeypatch.setattr(shadow_scoring, "resolve_artifact_identity",
                        _deleting_resolve)
    rec = _run_and_get_fault(tmp_path, _candidates(_finite_scores(first)))
    assert rec["state"] != STATE_NOT_YET_PUBLISHED  # the #254 regression pin
    assert rec["load_error"].startswith("ledger_unreadable:")
    assert "disappeared" in rec["load_error"]
    # The record certifies what WAS certified (the pre-delete bytes) …
    assert rec["artifact_resolved"] is True
    assert rec["content_sha256"] is not None
    # … and the failed lane leaves NO cache entry behind.
    assert not [k for k in shadow_scoring._SCORER_CACHE
                if k[0] == "momentum_residual"]


# --- primary-scorer surface (2026-08-03, pipeline#258) ------------------------
#
# The operator asked to SEE the momentum model's orders; running the lane as a
# readonly e2e PRIMARY crashed on the missing PanelScorer surface
# (`LoadScorerTask` logging `len(scorer.feature_cols)` — AttributeError). These
# pin the adapter: an empty feature contract plus index-lookup `score`.

def _lookup_scorer():
    from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (
        MomentumResidualScorer,
    )
    return MomentumResidualScorer(
        scores={"AAPL": 0.5, "MSFT": -0.25},
        metadata={"kind": "momentum_residual"},
    )


def test_primary_surface_exists_with_an_empty_feature_contract():
    s = _lookup_scorer()
    assert s.feature_cols == []
    assert s.seq_len == 1
    assert s.requires_history is False


def test_score_reads_only_the_matrix_index():
    import pandas as pd
    s = _lookup_scorer()
    # columns are junk on purpose — a lookup scorer must not touch them
    x = pd.DataFrame({"junk": [1.0, 2.0]}, index=["AAPL", "MSFT"])
    out = s.score(x)
    assert out.loc["AAPL"] == 0.5 and out.loc["MSFT"] == -0.25


def test_unscored_names_come_back_NaN_not_omitted():
    """The shadow path OMITS unknown names (coverage math counts them); the
    primary path needs them PRESENT as NaN — silent omission would shrink the
    cross-section without any unscored accounting."""
    import math
    import pandas as pd
    s = _lookup_scorer()
    x = pd.DataFrame(index=["AAPL", "ZZZC"])
    out = s.score(x)
    assert list(out.index) == ["AAPL", "ZZZC"]
    assert out.loc["AAPL"] == 0.5 and math.isnan(out.loc["ZZZC"])


def test_score_accepts_and_ignores_ctx():
    import pandas as pd
    s = _lookup_scorer()
    out = s.score(pd.DataFrame(index=["MSFT"]), ctx=object())
    assert out.loc["MSFT"] == -0.25


# --- primary config-consistency: momentum's OWN fingerprint scheme ------------
#
# The generic check compares XGB-recipe fields the lookup artifact does not
# carry (measured 2026-08-03: every field stored=None → a healthy lane
# fail-closed). For kind=momentum_residual the contract is an exact match
# between the artifact's own stamp and a pin the profile MUST declare;
# an absent pin fails closed (an absent expectation is not a passed one).

class _Ctx:
    """The minimal ctx surface _fail_closed_panel_scoring touches."""
    def __init__(self):
        self.candidates = []
        self.config = {}
        self.skip_buys = False


def _consistency(panel_cfg, fingerprint="momentum-v0-fd65161a20b29314"):
    from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as jps
    from renquant_pipeline.kernel.panel_pipeline.momentum_residual_scorer import (
        MomentumResidualScorer,
    )
    scorer = MomentumResidualScorer(
        scores={"AAPL": 0.5},
        metadata={"kind": "momentum_residual",
                  "config_fingerprint": fingerprint},
    )
    ctx = _Ctx()
    ok = jps.LoadScorerTask._assert_config_consistency(ctx, panel_cfg, scorer, None)
    return ok, ctx


def test_momentum_primary_with_matching_pin_passes():
    ok, ctx = _consistency(
        {"expected_config_fingerprint": "momentum-v0-fd65161a20b29314"})
    assert ok is True and ctx.skip_buys is False


def test_momentum_primary_with_NO_pin_fails_closed():
    ok, ctx = _consistency({})
    assert ok is False and ctx.skip_buys is True


def test_momentum_primary_with_MISMATCHED_pin_fails_closed():
    ok, ctx = _consistency(
        {"expected_config_fingerprint": "momentum-v0-0000000000000000"})
    assert ok is False and ctx.skip_buys is True


def test_momentum_branch_does_not_touch_the_generic_xgb_path():
    """A non-momentum scorer must still route to the generic recipe check —
    proven by it NOT short-circuiting to the momentum branch's outcomes when
    the momentum-only pin key is present."""
    from renquant_pipeline.kernel.panel_pipeline import job_panel_scoring as jps

    class _XgbLike:
        kind = "panel_ltr_xgboost"
        metadata = {"config_fingerprint": "sha256:f8fb2259b2bf1537"}

    ctx = _Ctx()
    # strict=False so the generic path degrades to a warning instead of
    # requiring a full artifact fixture; the assertion is only that the
    # momentum branch did not hijack the call.
    ok = jps.LoadScorerTask._assert_config_consistency(
        ctx, {"strict_config_consistency": False,
              "expected_config_fingerprint": "momentum-v0-fd65161a20b29314"},
        _XgbLike(), None)
    assert ok is True and ctx.skip_buys is False


# --- matrix_usable: the ONE shared usability predicate ------------------------

def test_matrix_usable_lookup_scorer_accepts_zero_columns_with_rows():
    import pandas as pd
    from renquant_pipeline.kernel.panel_pipeline.tasks_feature_matrix import (
        matrix_usable,
    )
    s = _lookup_scorer()
    assert matrix_usable(s, pd.DataFrame(index=["AAPL", "MSFT"])) is True


def test_matrix_usable_rejects_zero_rows_for_everyone():
    import pandas as pd
    from renquant_pipeline.kernel.panel_pipeline.tasks_feature_matrix import (
        matrix_usable,
    )
    s = _lookup_scorer()
    assert matrix_usable(s, pd.DataFrame()) is False
    assert matrix_usable(s, None) is False


def test_matrix_usable_feature_scorer_still_requires_columns():
    import pandas as pd
    from renquant_pipeline.kernel.panel_pipeline.tasks_feature_matrix import (
        matrix_usable,
    )

    class _FeatureScorer:
        feature_cols = ["roc60"]

    assert matrix_usable(_FeatureScorer(), pd.DataFrame(index=["AAPL"])) is False
    ok = pd.DataFrame({"roc60": [1.0]}, index=["AAPL"])
    assert matrix_usable(_FeatureScorer(), ok) is True
