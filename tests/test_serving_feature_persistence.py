"""Serving feature persistence — rollout step 2 of pipeline#250's design.

Pins the four design contracts:

(a) the persisted parquet is BYTE-FOR-BYTE the matrix the scorer consumed on
    a synthetic session (the design's core test) — exercised through the
    REAL kernel ``ApplyScoresTask`` on both the plain snapshot path and the
    alpha158/panel_ltr_xgboost transform path;
(b) a write failure records ``status: write_failed`` and does NOT raise —
    decisions (per-candidate scores) unchanged;
(c) sidecar block shape (design-verbatim keys) incl. sha256 correctness
    (recomputed and compared);
(d) the block is absent-tolerant downstream: absent ⇒ payload key absent ⇒
    payload byte-identical to before this surface existed.
"""
from __future__ import annotations

import datetime
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import ApplyScoresTask
from renquant_pipeline.serving_features import (
    RECORD_ATTR,
    SERVING_FEATURES_BLOCK_KEY,
    SERVING_FEATURES_FILENAME,
    STAGED_ATTR,
    STATUS_WRITE_FAILED,
    STATUS_WRITTEN,
    serving_features_bundle_block,
    stage_serving_features,
    write_staged_serving_features,
)

DESIGN_KEYS = {
    "path", "sha256", "n_rows", "n_cols",
    "feature_cutoff", "feature_builder_version", "panel_read_sha256",
}


class _CaptureScorer:
    """Snapshot scorer that records the exact matrix object it consumed."""

    requires_history = False
    seq_len = 1

    def __init__(self, feature_cols, metadata=None):
        self.feature_cols = list(feature_cols)
        self.metadata = metadata or {}
        self.consumed = None

    def score(self, feature_matrix, ctx=None):  # noqa: ARG002
        # Deep-copy at consumption time: this is the ground truth the
        # persisted parquet must match byte-for-byte.
        self.consumed = feature_matrix.copy(deep=True)
        return pd.Series(
            np.linspace(0.1, 0.9, len(feature_matrix)),
            index=feature_matrix.index, name="panel_score",
        )


def _kernel_ctx(tickers=("AAA", "BBB", "CCC"), **extra):
    candidates = [
        SimpleNamespace(ticker=t, rank_score=None, panel_score=None,
                        model_type=None, legacy_model_type=None)
        for t in tickers
    ]
    return SimpleNamespace(
        config={"ranking": {"panel_scoring": {"enabled": True}}},
        candidates=candidates, holdings={}, _panel_matrix=None, **extra,
    )


def _plain_snapshot_ctx(tmp_path, tickers=("AAA", "BBB", "CCC")):
    """Synthetic session on the plain snapshot path: scorer.score(X) consumes
    ctx._panel_matrix directly (kind=None, requires_history=False)."""
    cols = ["f1", "f2", "f3"]
    rng = np.random.default_rng(7)
    matrix = pd.DataFrame(
        rng.standard_normal((len(tickers), len(cols))),
        index=list(tickers), columns=cols,
    )
    scorer = _CaptureScorer(cols, metadata={"feature_preprocess_version": 2})
    ctx = _kernel_ctx(tickers)
    ctx._panel_matrix = matrix
    ctx._panel_scorer = scorer
    ctx.today = datetime.date(2026, 8, 2)
    ctx.run_output_dir = str(tmp_path)
    return ctx, scorer


def _read_back(path):
    return pd.read_parquet(path)


def _assert_byte_for_byte(parquet_path, consumed: pd.DataFrame):
    rb = _read_back(parquet_path)
    assert list(rb.columns) == ["ticker", *consumed.columns]
    assert list(rb["ticker"]) == [str(t) for t in consumed.index]
    values = rb[list(consumed.columns)].to_numpy()
    assert values.dtype == consumed.to_numpy().dtype
    assert values.tobytes() == consumed.to_numpy().tobytes(), (
        "persisted matrix bytes differ from the matrix the scorer consumed"
    )


# ── (a) the design's core test: parquet == consumed matrix, byte-for-byte ──

class TestByteForByte:
    def test_plain_snapshot_path(self, tmp_path):
        ctx, scorer = _plain_snapshot_ctx(tmp_path)
        ApplyScoresTask().run(ctx)
        assert scorer.consumed is not None
        _assert_byte_for_byte(tmp_path / SERVING_FEATURES_FILENAME, scorer.consumed)

    def test_alpha158_xgb_transform_path(self, tmp_path, monkeypatch):
        """The design's named site: X_aligned post transform_feature_frame is
        exactly what lands on disk. Fake alpha158 rows per ticker; REAL
        transform (artifact-stored means/stds) runs in between."""
        from renquant_pipeline.kernel.panel_pipeline import alpha158_features

        cols = ["KMID", "KLEN", "ROC5"]
        raw = {
            "AAA": {"KMID": 1.0, "KLEN": 2.0, "ROC5": 3.0},
            "BBB": {"KMID": 4.0, "KLEN": 5.0, "ROC5": 6.0},
            "CCC": {"KMID": 7.0, "KLEN": 8.0, "ROC5": 9.0},
        }

        def fake_alpha158(ohlcv, today):  # noqa: ARG001
            marker = int(float(ohlcv["close"].iloc[-1]))
            return dict(raw["ABC"[marker] * 3])

        monkeypatch.setattr(alpha158_features, "compute_alpha158_at", fake_alpha158)

        metadata = {
            "kind": "panel_ltr_xgboost",
            "feature_preprocess_version": 2,
            "feature_means": [2.0, 3.0, 4.0],
            "feature_stds": [1.0, 2.0, 4.0],
        }
        scorer = _CaptureScorer(cols, metadata=metadata)
        tickers = ["AAA", "BBB", "CCC"]
        ctx = _kernel_ctx(tickers)
        ctx._panel_matrix = pd.DataFrame({"__alpha158_target__": 1.0}, index=tickers)
        ctx._panel_scorer = scorer
        ctx.today = datetime.date(2026, 8, 2)
        ctx.ohlcv = {
            t: pd.DataFrame({"close": [float(i)] * 80})
            for i, t in enumerate(tickers)
        }
        ctx.run_output_dir = str(tmp_path)

        ApplyScoresTask().run(ctx)

        assert scorer.consumed is not None
        # The transform actually transformed (post-transform ≠ raw rows):
        assert float(scorer.consumed.loc["AAA", "KMID"]) != 1.0
        _assert_byte_for_byte(tmp_path / SERVING_FEATURES_FILENAME, scorer.consumed)
        record = getattr(ctx, RECORD_ATTR)
        assert record["status"] == STATUS_WRITTEN
        assert record["feature_builder_version"] == "2"
        assert record["feature_cutoff"] == "2026-08-02"

    def test_staged_copy_is_immune_to_later_mutation(self, tmp_path):
        """In-place mutation of the live matrix AFTER scoring (the sentiment-
        gate pattern) must not change what was persisted."""
        ctx, scorer = _plain_snapshot_ctx(tmp_path)
        del ctx.run_output_dir  # defer the write past the mutation
        ApplyScoresTask().run(ctx)
        consumed = scorer.consumed.copy(deep=True)
        ctx._panel_matrix.iloc[:, :] = 0.0  # downstream in-place zeroing
        record = write_staged_serving_features(ctx, tmp_path)
        assert record["status"] == STATUS_WRITTEN
        _assert_byte_for_byte(tmp_path / SERVING_FEATURES_FILENAME, consumed)


# ── (b) write failure records, never raises; decisions unchanged ────────────

class TestWriteFailureNeverRaises:
    def test_unwritable_dir_records_write_failed(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where a directory must go")
        ctx, scorer = _plain_snapshot_ctx(tmp_path)
        ctx.run_output_dir = str(blocker / "nested")  # mkdir will fail
        ApplyScoresTask().run(ctx)  # must not raise
        record = getattr(ctx, RECORD_ATTR)
        assert record["status"] == STATUS_WRITE_FAILED
        assert record["error"]
        assert DESIGN_KEYS <= set(record)

    def test_decisions_byte_unaffected_by_write_failure(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        failing_ctx, _ = _plain_snapshot_ctx(tmp_path)
        failing_ctx.run_output_dir = str(blocker / "nested")
        control_ctx, _ = _plain_snapshot_ctx(tmp_path / "ok")
        ApplyScoresTask().run(failing_ctx)
        ApplyScoresTask().run(control_ctx)
        failing = {c.ticker: (c.panel_score, c.rank_score)
                   for c in failing_ctx.candidates}
        control = {c.ticker: (c.panel_score, c.rank_score)
                   for c in control_ctx.candidates}
        assert failing == control

    def test_to_parquet_exception_recorded_not_raised(self, tmp_path, monkeypatch):
        ctx, _ = _plain_snapshot_ctx(tmp_path)
        del ctx.run_output_dir

        def boom(self, *a, **k):  # noqa: ARG001
            raise OSError("disk full (synthetic)")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
        ApplyScoresTask().run(ctx)  # staging only — no write attempted yet
        record = write_staged_serving_features(ctx, tmp_path)
        assert record["status"] == STATUS_WRITE_FAILED
        assert "disk full" in record["error"]
        assert not (tmp_path / SERVING_FEATURES_FILENAME).exists()

    def test_payload_writer_survives_write_failure(self, tmp_path, monkeypatch):
        from renquant_pipeline.inference import (
            write_runtime_inference_payload_from_live_context,
        )
        ctx, _ = _plain_snapshot_ctx(tmp_path)
        del ctx.run_output_dir
        ApplyScoresTask().run(ctx)
        monkeypatch.setattr(
            pd.DataFrame, "to_parquet",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError("synthetic")),
        )
        ctx.market_snapshot = {"as_of": "2026-08-02"}
        out = write_runtime_inference_payload_from_live_context(
            ctx, tmp_path / "payload.json",
        )
        payload = json.loads(out.read_text())
        block = payload[SERVING_FEATURES_BLOCK_KEY]
        assert block["status"] == STATUS_WRITE_FAILED
        assert block["error"]


# ── (c) sidecar block shape + sha256 correctness ────────────────────────────

class TestSidecarBlock:
    def test_block_shape_and_sha256(self, tmp_path):
        ctx, scorer = _plain_snapshot_ctx(tmp_path)
        ApplyScoresTask().run(ctx)
        record = getattr(ctx, RECORD_ATTR)
        assert set(record) == DESIGN_KEYS | {"status"}
        assert record["status"] == STATUS_WRITTEN
        path = tmp_path / SERVING_FEATURES_FILENAME
        assert record["path"] == str(path)
        recomputed = hashlib.sha256(path.read_bytes()).hexdigest()
        assert record["sha256"] == recomputed
        assert record["n_rows"] == 3
        assert record["n_cols"] == len(scorer.feature_cols)
        assert record["feature_cutoff"] == "2026-08-02"
        # sourced from the artifact's feature_preprocess_version — the key
        # the transform's version already lives under (no invented constant)
        assert record["feature_builder_version"] == "2"
        # no panel parquet is read on the snapshot serving path — honest None
        assert record["panel_read_sha256"] is None

    def test_panel_read_sha256_when_a_panel_file_was_read(self, tmp_path):
        panel = tmp_path / "panel.parquet"
        panel.write_bytes(b"panel bytes stand-in")
        ctx = SimpleNamespace(today=datetime.date(2026, 8, 2))
        matrix = pd.DataFrame({"f1": [1.0, 2.0]}, index=["AAA", "BBB"])
        scorer = _CaptureScorer(["f1"], metadata={"feature_preprocess_version": 2})
        stage_serving_features(ctx, matrix, scorer, panel_read_path=panel)
        record = write_staged_serving_features(ctx, tmp_path / "out")
        assert record["status"] == STATUS_WRITTEN
        assert record["panel_read_sha256"] == hashlib.sha256(
            b"panel bytes stand-in").hexdigest()

    def test_writer_is_idempotent(self, tmp_path):
        ctx, _ = _plain_snapshot_ctx(tmp_path)
        ApplyScoresTask().run(ctx)
        first = getattr(ctx, RECORD_ATTR)
        again = write_staged_serving_features(ctx, tmp_path / "elsewhere")
        assert again is first  # completed record returned as-is, no rewrite
        assert not (tmp_path / "elsewhere").exists()

    def test_missing_version_stamp_records_none(self, tmp_path):
        ctx = SimpleNamespace(today=datetime.date(2026, 8, 2))
        matrix = pd.DataFrame({"f1": [1.0]}, index=["AAA"])
        stage_serving_features(ctx, matrix, _CaptureScorer(["f1"], metadata={}))
        record = write_staged_serving_features(ctx, tmp_path)
        assert record["status"] == STATUS_WRITTEN
        assert record["feature_builder_version"] is None


# ── (d) absent-tolerance: the additive idiom's standard test ────────────────

class TestAbsentTolerantDownstream:
    def test_history_scorer_run_stages_nothing(self):
        """A sequence scorer (score_with_history) consumes no snapshot
        matrix — the recorder must not fire at all."""

        class _HistoryScorer(_CaptureScorer):
            requires_history = True
            seq_len = 2

            def score_with_history(self, panel_history, target_tickers):  # noqa: ARG002
                return pd.Series(
                    [0.5] * len(target_tickers), index=target_tickers,
                    name="panel_score",
                )

        tickers = ["AAA", "BBB", "CCC"]
        ctx = _kernel_ctx(tickers)
        ctx._panel_scorer = _HistoryScorer([], metadata={"kind": "hf_patchtst"})
        ctx._panel_matrix = pd.DataFrame({"__history_target__": 1.0}, index=tickers)
        ctx.today = datetime.date(2026, 8, 2)
        ctx._panel_history = pd.DataFrame({
            "ticker": [t for t in tickers for _ in range(2)],
            "date": pd.to_datetime(["2026-07-31", "2026-08-01"] * 3),
        })
        ApplyScoresTask().run(ctx)
        assert getattr(ctx, STAGED_ATTR, None) is None
        assert getattr(ctx, RECORD_ATTR, None) is None
        assert serving_features_bundle_block(ctx) is None

    def test_absent_block_leaves_payload_byte_identical(self, tmp_path):
        from renquant_pipeline.inference import (
            runtime_inference_payload,
            write_runtime_inference_payload,
        )
        from renquant_pipeline.inference import InferenceContext

        def _contract_ctx():
            return InferenceContext(
                strategy_config={"watchlist": ["AAA"]},
                data_manifest={}, artifact_manifest={},
                market_snapshot={"as_of": "2026-08-02"},
            )

        plain = runtime_inference_payload(_contract_ctx())
        assert SERVING_FEATURES_BLOCK_KEY not in plain

        staged_ctx = _contract_ctx()
        matrix = pd.DataFrame({"f1": [1.0]}, index=["AAA"])
        stage_serving_features(staged_ctx, matrix, _CaptureScorer(["f1"]))
        out = write_runtime_inference_payload(staged_ctx, tmp_path / "p.json")
        with_block = json.loads(out.read_text())
        assert with_block[SERVING_FEATURES_BLOCK_KEY]["status"] == STATUS_WRITTEN
        assert (tmp_path / SERVING_FEATURES_FILENAME).exists()
        # additive proof: strip the one new key ⇒ exactly the plain payload
        with_block.pop(SERVING_FEATURES_BLOCK_KEY)
        assert with_block == plain

    def test_live_context_snapshot_absent_and_present(self, tmp_path):
        from renquant_pipeline.inference import (
            live_context_snapshot_from_live_context,
        )
        base = {
            "strategy_config": {"watchlist": ["AAA"]},
            "market_snapshot": {"as_of": "2026-08-02"},
            "decision_trace": [],
        }
        snap = live_context_snapshot_from_live_context(dict(base))
        assert snap.serving_features is None
        assert SERVING_FEATURES_BLOCK_KEY not in snap.to_runtime_payload()

        ctx = SimpleNamespace(**base, today=datetime.date(2026, 8, 2))
        stage_serving_features(
            ctx, pd.DataFrame({"f1": [1.0]}, index=["AAA"]),
            _CaptureScorer(["f1"]),
        )
        write_staged_serving_features(ctx, tmp_path)
        snap2 = live_context_snapshot_from_live_context(ctx)
        payload = snap2.to_runtime_payload()
        assert payload[SERVING_FEATURES_BLOCK_KEY]["status"] == STATUS_WRITTEN

    def test_unstaged_write_is_a_noop(self, tmp_path):
        ctx = SimpleNamespace()
        assert write_staged_serving_features(ctx, tmp_path) is None
        assert not (tmp_path / SERVING_FEATURES_FILENAME).exists()

    def test_public_exports_resolve(self):
        import renquant_pipeline as rp

        assert rp.SERVING_FEATURES_FILENAME == "serving_features.parquet"
        assert callable(rp.stage_serving_features)
        assert callable(rp.write_staged_serving_features)
        assert callable(rp.serving_features_bundle_block)
