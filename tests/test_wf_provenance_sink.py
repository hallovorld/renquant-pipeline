"""WF sim-time provenance sink contract (design #215, ``wf_sim_provenance.v1``).

Covers the pipeline piece of the merged provenance contract
(``doc/design/2026-07-27-wf-sim-provenance-contract.md``):

* JSONL sink round-trip + append-only file format + identity completion;
* ``fold_resolved`` emission from ``WalkForwardModelLoader`` over a
  synthetic manifest (fixture style shared with
  ``tests/test_wf_fold_selection_parity.py``);
* ``score_committed`` canonical-payload digest determinism (ticker-sort
  row-order independence, float-``repr`` stability, int/float
  normalization);
* the PIT invariant (``input_watermark <= score_timestamp``) emitting
  ``pit_violation: true`` on breach — record still produced;
* default ``provenance_sink=None`` / absent ctx attrs = byte-identical
  behavior and NO file written (zero live-surface delta);
* idempotent re-emit = no-op (audit clock excluded from identity).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from renquant_pipeline.context import InferenceContext
from renquant_pipeline.kernel.persistence import ensure_schema
from renquant_pipeline.kernel.pipeline.task_score_distribution import (
    RecordScoreDistributionTask,
)
from renquant_pipeline.kernel.selection import CandidateResult
from renquant_pipeline.kernel.walk_forward.loader import WalkForwardModelLoader
from renquant_pipeline.kernel.walk_forward.provenance import (
    DIGEST_RE,
    RECORD_KIND_FOLD_RESOLVED,
    RECORD_KIND_SCORE_COMMITTED,
    SCHEMA_VERSION,
    JsonlProvenanceSink,
    build_fold_resolved_record,
    build_score_committed_record,
    canonical_score_payload,
    score_payload_digest,
    sha256_digest,
)

NY = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------
# Fixture helpers (manifest style shared with test_wf_fold_selection_parity)
# --------------------------------------------------------------------------

def _row(
    cutoff: str,
    uri: str,
    lookahead: int = 0,
    effective: str | None = None,
    calibrator_uri: str | None = None,
) -> dict:
    r = {
        "cutoff_date": cutoff,
        "trained_date": "2026-12-31",
        "artifact_uri": uri,
        "lookahead_days": lookahead,
    }
    if effective is not None:
        r["effective_train_cutoff_date"] = effective
    if calibrator_uri is not None:
        r["calibrator_uri"] = calibrator_uri
    return r


def _write_manifest(tmp_path, rows):
    p = tmp_path / "walkforward_manifest.json"
    p.write_text(json.dumps({"retrains": rows}))
    return p


def _sink(tmp_path, sim_run_id="sim-run-1", **kwargs) -> JsonlProvenanceSink:
    return JsonlProvenanceSink(
        sim_run_id, tmp_path / "wf_provenance", **kwargs,
    )


def _read_records(sink: JsonlProvenanceSink) -> list[dict]:
    if not sink.path.exists():
        return []
    return [
        json.loads(line)
        for line in sink.path.read_text().splitlines()
        if line
    ]


def _fold_record(**overrides) -> dict:
    base = dict(
        prediction_date="2024-06-03",
        cutoff_date="2024-01-15",
        trained_date="2026-12-31",
        effective_train_cutoff_date=None,
        lookahead_days=60,
        artifact_uri="fold.json",
        calibrator_uri=None,
        manifest_path="/x/walkforward_manifest.json",
        manifest_digest="sha256:" + "ab" * 32,
        artifact_digest="sha256:" + "cd" * 32,
        is_real_content_digest=True,
        family="json",
        fingerprint_schema="legacy",
    )
    base.update(overrides)
    return build_fold_resolved_record(**base)


def _score_record(**overrides) -> dict:
    base = dict(
        prediction_date="2024-06-03",
        score_observation_key=["run-1", "2024-06-03", "sim"],
        score_payload_digest="sha256:" + "ef" * 32,
        n_rows=2,
        artifact_digest="sha256:" + "cd" * 32,
        score_timestamp="2024-06-03T16:00:00-04:00",
    )
    base.update(overrides)
    return build_score_committed_record(**base)


# --------------------------------------------------------------------------
# Sink writer: round-trip, file format, identity completion, append-only.
# --------------------------------------------------------------------------

class TestJsonlSinkWriter:
    def test_round_trip_file_format(self, tmp_path):
        sink = _sink(tmp_path, seed=7, revision_pins={"pipeline": "abc123"})
        sink.emit(_fold_record())
        sink.emit(_score_record())

        assert sink.path == tmp_path / "wf_provenance" / "sim-run-1.jsonl"
        records = _read_records(sink)
        assert [r["record_kind"] for r in records] == [
            RECORD_KIND_FOLD_RESOLVED, RECORD_KIND_SCORE_COMMITTED,
        ]
        for r in records:
            assert r["schema_version"] == SCHEMA_VERSION
            # identity completed by the sink from run_backtest-scope args
            assert r["sim_run_id"] == "sim-run-1"
            # audit clock present, timezone-aware, audit-ONLY
            emitted = dt.datetime.fromisoformat(r["emitted_at_utc"])
            assert emitted.tzinfo is not None
        fold, score = records
        assert fold["seed"] == 7
        assert fold["revision_pins"] == {"pipeline": "abc123"}
        # score_committed carries decision-time semantics, not the audit clock
        assert score["score_timestamp"] == "2024-06-03T16:00:00-04:00"
        assert score["persisted"] is True
        assert score["pit_violation"] is False

    def test_append_only_across_sink_instances(self, tmp_path):
        _sink(tmp_path).emit(_fold_record())
        # A new sink for the same sim_run_id APPENDS (never truncates) —
        # the design's append-only durability rule across process restarts.
        second = _sink(tmp_path)
        second.emit(_score_record())
        assert len(_read_records(second)) == 2

    def test_cross_run_emit_refused(self, tmp_path):
        sink = _sink(tmp_path)
        record = _fold_record(sim_run_id="some-other-run")
        with pytest.raises(ValueError, match="cross-run"):
            sink.emit(record)
        assert _read_records(sink) == []

    def test_unsafe_sim_run_id_refused(self, tmp_path):
        for bad in ("", ".", "..", "a/b"):
            with pytest.raises(ValueError, match="safe filename"):
                JsonlProvenanceSink(bad, tmp_path)

    def test_wrong_schema_version_refused(self, tmp_path):
        sink = _sink(tmp_path)
        record = _fold_record()
        record["schema_version"] = "wf_sim_provenance.v0"
        with pytest.raises(ValueError, match="schema_version"):
            sink.emit(record)


class TestIdempotentReemit:
    def test_identical_score_committed_reemit_is_noop(self, tmp_path):
        sink = _sink(tmp_path)
        sink.emit(_score_record())
        sink.emit(_score_record())  # byte-identical content (audit excluded)
        assert len(_read_records(sink)) == 1

    def test_differing_score_committed_appends(self, tmp_path):
        # A retry that produced DIFFERENT content appends — extraction's
        # byte-identity rule then rejects the date, honestly.
        sink = _sink(tmp_path)
        sink.emit(_score_record())
        sink.emit(_score_record(score_payload_digest="sha256:" + "99" * 32))
        assert len(_read_records(sink)) == 2

    def test_fold_resolved_reentrant_dedup_per_bar(self, tmp_path):
        sink = _sink(tmp_path)
        sink.emit(_fold_record())
        sink.emit(_fold_record())
        assert len(_read_records(sink)) == 1
        # a different bar is a different key — appends
        sink.emit(_fold_record(prediction_date="2024-06-04"))
        assert len(_read_records(sink)) == 2


# --------------------------------------------------------------------------
# Record constructors: grammar + decision-time semantics + PIT invariant.
# --------------------------------------------------------------------------

class TestRecordConstructors:
    def test_score_timestamp_required(self):
        with pytest.raises(ValueError, match="score_timestamp is REQUIRED"):
            _score_record(score_timestamp=None)

    def test_naive_score_timestamp_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _score_record(score_timestamp="2024-06-03T16:00:00")

    @pytest.mark.parametrize("bad", [
        "sha256:short",
        "sha256:" + "ab" * 8,        # the #211 16-hex abbreviated form
        "md5:" + "ab" * 32,
        "ab" * 32,
    ])
    def test_full_digest_grammar_enforced(self, bad):
        with pytest.raises(ValueError, match="sha256:<64 hex>"):
            _fold_record(manifest_digest=bad)
        with pytest.raises(ValueError, match="sha256:<64 hex>"):
            _score_record(score_payload_digest=bad)

    def test_real_content_digest_requires_digest(self):
        with pytest.raises(ValueError, match="is_real_content_digest"):
            _fold_record(artifact_digest=None, is_real_content_digest=True)

    def test_observation_key_must_have_three_coordinates(self):
        with pytest.raises(ValueError, match="score_observation_key"):
            _score_record(score_observation_key=["run-1", "2024-06-03"])

    def test_pit_violation_on_watermark_after_decision(self):
        # Breach STILL builds a record (append-only honesty) with the flag.
        record = _score_record(
            input_watermark="2024-06-03T16:00:01-04:00",
        )
        assert record["pit_violation"] is True
        assert record["input_watermark"] == "2024-06-03T16:00:01-04:00"

    @pytest.mark.parametrize("watermark", [
        "2024-06-03T16:00:00-04:00",   # equality is NOT a breach (<=)
        "2024-06-03T15:59:00-04:00",
        "2024-06-03T19:00:00+00:00",   # cross-offset comparison, pre-close
    ])
    def test_no_pit_violation_at_or_before_decision(self, watermark):
        record = _score_record(input_watermark=watermark)
        assert record["pit_violation"] is False

    def test_missing_watermark_recorded_null_not_a_pass(self):
        record = _score_record(input_watermark=None)
        assert record["input_watermark"] is None
        assert record["pit_violation"] is False


# --------------------------------------------------------------------------
# Canonical score-payload serialization + digest determinism.
# --------------------------------------------------------------------------

def _payload_row(ticker, raw_panel, mu, rank_score, sigma):
    return {
        "ticker": ticker, "raw_panel": raw_panel, "mu": mu,
        "rank_score": rank_score, "sigma": sigma,
    }


class TestCanonicalScorePayload:
    ROWS = [
        _payload_row("MSFT", 0.25, 0.011, 0.75, 0.19),
        _payload_row("AAPL", 0.5, -0.002, 0.9, 0.21),
        _payload_row("NVDA", None, None, None, None),
    ]

    def test_same_frame_same_digest(self):
        assert score_payload_digest(self.ROWS) == score_payload_digest(
            [dict(r) for r in self.ROWS],
        )
        assert DIGEST_RE.match(score_payload_digest(self.ROWS))

    def test_row_order_independence_via_ticker_sort(self):
        assert score_payload_digest(self.ROWS) == score_payload_digest(
            list(reversed(self.ROWS)),
        )

    def test_serialization_shape_is_pinned(self):
        # The exact byte layout is the cross-repo contract the Phase-A
        # extractor recomputes — pin it.
        payload = canonical_score_payload([
            _payload_row("MSFT", 0.25, None, 0.75, 0.19),
            _payload_row("AAPL", 1, -0.002, 0.9, 0.21),
        ])
        assert payload == (
            b'["AAPL","1.0","-0.002","0.9","0.21"]\n'
            b'["MSFT","0.25",null,"0.75","0.19"]'
        )
        assert score_payload_digest(
            [_payload_row("MSFT", 0.25, None, 0.75, 0.19),
             _payload_row("AAPL", 1, -0.002, 0.9, 0.21)],
        ) == sha256_digest(payload)

    def test_int_float_normalization(self):
        as_int = [_payload_row("AAPL", 1, 0, 2, 3)]
        as_float = [_payload_row("AAPL", 1.0, 0.0, 2.0, 3.0)]
        assert score_payload_digest(as_int) == score_payload_digest(as_float)

    def test_float_repr_stability_distinguishes_near_values(self):
        a = [_payload_row("AAPL", 0.3, None, None, None)]
        b = [_payload_row("AAPL", 0.1 + 0.2, None, None, None)]
        # repr keeps the full shortest-roundtrip form — 0.30000000000000004
        # is NOT 0.3, and the digest must see the difference.
        assert score_payload_digest(a) != score_payload_digest(b)


# --------------------------------------------------------------------------
# Loader: fold_resolved emission at the entry_as_of seam.
# --------------------------------------------------------------------------

class TestLoaderFoldResolvedEmission:
    def _manifest_with_artifact(self, tmp_path, **row_kwargs):
        artifact = tmp_path / "fold_a.json"
        artifact.write_text(json.dumps({"model": "synthetic", "n": 1}))
        rows = [
            _row("2023-10-02", "stale.json", 60),
            _row("2024-01-15", artifact.name, 60, **row_kwargs),
        ]
        return _write_manifest(tmp_path, rows), artifact

    def test_emits_resolution_actually_served(self, tmp_path):
        manifest, artifact = self._manifest_with_artifact(tmp_path)
        sink = _sink(tmp_path, seed=11, revision_pins={"pipeline": "deadbeef"})
        loader = WalkForwardModelLoader(manifest, provenance_sink=sink)

        entry = loader.entry_as_of("2024-06-03")
        assert entry.artifact_uri == artifact.name

        (record,) = _read_records(sink)
        assert record["record_kind"] == RECORD_KIND_FOLD_RESOLVED
        assert record["schema_version"] == SCHEMA_VERSION
        # identity — completed by the sink
        assert record["sim_run_id"] == "sim-run-1"
        assert record["prediction_date"] == "2024-06-03"
        assert record["seed"] == 11
        assert record["revision_pins"] == {"pipeline": "deadbeef"}
        # fold — the RetrainEntry actually served
        assert record["cutoff_date"] == "2024-01-15"
        assert record["trained_date"] == "2026-12-31"
        assert record["effective_train_cutoff_date"] is None
        assert record["lookahead_days"] == 60
        assert record["artifact_uri"] == artifact.name
        # manifest — path + whole-file digest
        assert record["manifest_path"] == str(manifest)
        assert record["manifest_digest"] == (
            "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
        )
        # artifact — the same whole-file bytes _scorer_claim_for_entry hashes
        assert record["artifact_digest"] == (
            "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
        assert record["is_real_content_digest"] is True
        assert record["family"] == "json"
        # unstamped synthetic payload dispatches on the legacy route
        assert record["fingerprint_schema"] == "legacy"
        assert record["calibrator_digest"] is None

    def test_reentrant_bar_emits_once_new_bar_emits_again(self, tmp_path):
        manifest, _ = self._manifest_with_artifact(tmp_path)
        sink = _sink(tmp_path)
        loader = WalkForwardModelLoader(manifest, provenance_sink=sink)
        loader.entry_as_of("2024-06-03")
        loader.entry_as_of("2024-06-03")  # model_as_of/calibrator_as_of path
        assert len(_read_records(sink)) == 1
        loader.entry_as_of("2024-06-04")
        assert len(_read_records(sink)) == 2

    def test_missing_artifact_is_honest_not_fatal(self, tmp_path):
        manifest = _write_manifest(
            tmp_path, [_row("2024-01-15", "nowhere.json", 60)],
        )
        sink = _sink(tmp_path)
        loader = WalkForwardModelLoader(manifest, provenance_sink=sink)
        loader.entry_as_of("2024-06-03")
        (record,) = _read_records(sink)
        assert record["artifact_digest"] is None
        assert record["is_real_content_digest"] is False

    def test_non_json_family_stays_on_file_hash_identity(self, tmp_path):
        checkpoint = tmp_path / "fold_b.pt"
        checkpoint.write_bytes(b"\x80\x02fake-checkpoint")
        manifest = _write_manifest(
            tmp_path, [_row("2024-01-15", checkpoint.name, 60)],
        )
        sink = _sink(tmp_path)
        loader = WalkForwardModelLoader(manifest, provenance_sink=sink)
        loader.entry_as_of("2024-06-03")
        (record,) = _read_records(sink)
        assert record["family"] == "pt"
        assert record["fingerprint_schema"] == "legacy"
        assert record["artifact_digest"] == (
            "sha256:" + hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        )

    def test_calibrator_digest_captured_when_bound(self, tmp_path):
        calibrator = tmp_path / "cal_a.json"
        calibrator.write_text(json.dumps({"calibration": True}))
        manifest, _ = self._manifest_with_artifact(
            tmp_path, calibrator_uri=calibrator.name,
        )
        sink = _sink(tmp_path)
        loader = WalkForwardModelLoader(manifest, provenance_sink=sink)
        loader.entry_as_of("2024-06-03")
        (record,) = _read_records(sink)
        assert record["calibrator_uri"] == calibrator.name
        assert record["calibrator_digest"] == (
            "sha256:" + hashlib.sha256(calibrator.read_bytes()).hexdigest()
        )

    def test_default_none_no_file_and_identical_selection(self, tmp_path):
        # Zero live-surface delta: no sink ⇒ no file, and the resolution is
        # identical to the sink-carrying loader's (parity with the existing
        # loader/selection tests).
        manifest, _ = self._manifest_with_artifact(tmp_path)
        default_loader = WalkForwardModelLoader(manifest)
        sink = _sink(tmp_path)
        sink_loader = WalkForwardModelLoader(manifest, provenance_sink=sink)
        assert (
            default_loader.entry_as_of("2024-06-03")
            == sink_loader.entry_as_of("2024-06-03")
        )
        # only the sink-carrying loader's file exists; the default loader
        # wrote nothing anywhere
        files = list((tmp_path / "wf_provenance").glob("*.jsonl"))
        assert files == [sink.path]
        assert len(_read_records(sink)) == 1


# --------------------------------------------------------------------------
# Task: score_committed emission at the persistence commit point.
# --------------------------------------------------------------------------

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    return conn


def _task_ctx(conn: sqlite3.Connection) -> InferenceContext:
    ctx = InferenceContext(
        config={
            "watchlist": ["AAPL", "MSFT"],
            "sector_map": {"AAPL": "TECH", "MSFT": "TECH"},
            "score_db": {"enabled": True},
        },
        today=dt.date(2024, 6, 3),
        models={},
        candidates=[
            CandidateResult("MSFT", 0.1, 0.75, 0.0, panel_score=0.25,
                            mu=0.011, sigma=0.19),
            CandidateResult("AAPL", 0.1, 0.9, 0.0, panel_score=0.5,
                            mu=-0.002, sigma=0.21),
        ],
        holdings={},
    )
    ctx.regime = "BULL_CALM"
    ctx.run_id = "2024-06-03-sim-abcd1234"
    ctx._db = conn
    return ctx


ARTIFACT_ECHO = "sha256:" + "cd" * 32


class TestTaskScoreCommittedEmission:
    def _run(self, tmp_path, *, watermark=None, run_timestamp=None):
        conn = _db()
        ctx = _task_ctx(conn)
        sink = _sink(tmp_path)
        ctx._wf_provenance_sink = sink
        ctx._wf_active_fold = {"artifact_digest": ARTIFACT_ECHO}
        if watermark is not None:
            ctx._wf_input_watermark = watermark
        if run_timestamp is not None:
            ctx.run_timestamp = run_timestamp
        RecordScoreDistributionTask().run(ctx)
        return conn, ctx, sink

    def test_emits_after_successful_insert_and_digest_matches_db(
        self, tmp_path,
    ):
        conn, ctx, sink = self._run(tmp_path)
        (record,) = _read_records(sink)
        assert record["record_kind"] == RECORD_KIND_SCORE_COMMITTED
        assert record["sim_run_id"] == "sim-run-1"
        assert record["prediction_date"] == "2024-06-03"
        assert record["score_observation_key"] == [
            ctx.run_id, "2024-06-03", "sim",
        ]
        assert record["persisted"] is True
        assert record["artifact_digest"] == ARTIFACT_ECHO

        # Mini Phase-A step 2: read the observation back AT the key,
        # recompute the digest over what was read, require equality.
        rows = conn.execute(
            "SELECT ticker, raw_panel, mu, rank_score, sigma"
            "  FROM score_distribution WHERE run_id=? AND date=? AND run_type=?",
            tuple(record["score_observation_key"]),
        ).fetchall()
        assert len(rows) == record["n_rows"] == 2
        recomputed = score_payload_digest([
            _payload_row(*r) for r in rows
        ])
        assert recomputed == record["score_payload_digest"]

    def test_score_timestamp_fallback_is_session_close_on_bar_date(
        self, tmp_path,
    ):
        # Sim ctx leaves run_timestamp None (bar-date-only semantics) — the
        # design-named fallback is the simulated session decision instant:
        # 16:00 America/New_York on the prediction date.
        _, _, sink = self._run(tmp_path)
        (record,) = _read_records(sink)
        expected = dt.datetime.combine(
            dt.date(2024, 6, 3), dt.time(16, 0), tzinfo=NY,
        ).isoformat()
        assert record["score_timestamp"] == expected
        assert record["pit_violation"] is False

    def test_ctx_run_timestamp_wins_when_present(self, tmp_path):
        ts = dt.datetime(2024, 6, 3, 15, 45, tzinfo=NY)
        _, _, sink = self._run(tmp_path, run_timestamp=ts)
        (record,) = _read_records(sink)
        assert record["score_timestamp"] == ts.isoformat()

    def test_pit_violation_flows_through_task_emit(self, tmp_path):
        _, _, sink = self._run(
            tmp_path, watermark="2024-06-03T16:30:00-04:00",
        )
        (record,) = _read_records(sink)
        assert record["pit_violation"] is True
        assert record["input_watermark"] == "2024-06-03T16:30:00-04:00"

    def test_rerun_same_bar_is_noop_changed_scores_append(self, tmp_path):
        conn = _db()
        ctx = _task_ctx(conn)
        sink = _sink(tmp_path)
        ctx._wf_provenance_sink = sink
        ctx._wf_active_fold = {"artifact_digest": ARTIFACT_ECHO}
        task = RecordScoreDistributionTask()
        task.run(ctx)
        task.run(ctx)  # INSERT OR REPLACE + identical payload → no-op
        assert len(_read_records(sink)) == 1
        ctx.candidates[0].rank_score = 0.51  # a real re-score
        task.run(ctx)
        assert len(_read_records(sink)) == 2

    def test_absent_sink_attrs_is_default_path(self, tmp_path):
        # No ctx._wf_provenance_sink ⇒ byte-identical default behavior:
        # rows persist, nothing emitted, no provenance dir created.
        conn = _db()
        ctx = _task_ctx(conn)
        RecordScoreDistributionTask().run(ctx)
        n = conn.execute("SELECT COUNT(*) FROM score_distribution").fetchone()[0]
        assert n == 2
        assert not (tmp_path / "wf_provenance").exists()

    def test_failed_insert_emits_nothing(self, tmp_path):
        conn = _db()
        conn.execute("DROP TABLE score_distribution")
        ctx = _task_ctx(conn)
        sink = _sink(tmp_path)
        ctx._wf_provenance_sink = sink
        ctx._wf_active_fold = {"artifact_digest": ARTIFACT_ECHO}
        assert RecordScoreDistributionTask().run(ctx) is False
        assert _read_records(sink) == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
