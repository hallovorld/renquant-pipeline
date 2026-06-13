"""Point-in-time data layer tests — lookahead impossible at the API (§III.2)."""
from __future__ import annotations

from pathlib import Path

from renquant_pipeline.kernel.pit_reader import (
    pit_append,
    pit_latest,
    pit_visible,
)


def _payload(tmp_path, name="p.bin", content=b"x"):
    p = tmp_path / name
    p.write_bytes(content)
    return p


class TestPublicationLag:
    def test_finra_e5_rule_not_yet_published(self, tmp_path):
        # SI for 2026-05-30, 9-day publication lag, collected 2026-06-01.
        pit_append(tmp_path, "si", date="2026-05-30",
                   payload_file=_payload(tmp_path), collected_at="2026-06-01",
                   publication_lag_days=9)
        # 2026-06-05: collected, but effective date 06-08 not reached → invisible
        assert pit_visible(tmp_path, "si", "2026-06-05") == []

    def test_visible_after_publication(self, tmp_path):
        pit_append(tmp_path, "si", date="2026-05-30",
                   payload_file=_payload(tmp_path), collected_at="2026-06-01",
                   publication_lag_days=9)
        # 2026-06-08 = 05-30 + 9d → visible
        vis = pit_visible(tmp_path, "si", "2026-06-08")
        assert len(vis) == 1

    def test_not_visible_before_collection(self, tmp_path):
        # Even with 0 lag, a row collected 2026-06-10 is invisible on 06-05.
        pit_append(tmp_path, "fund", date="2026-06-01",
                   payload_file=_payload(tmp_path), collected_at="2026-06-10",
                   publication_lag_days=0)
        assert pit_visible(tmp_path, "fund", "2026-06-05") == []


class TestLatest:
    def test_returns_most_recent_visible(self, tmp_path):
        for d, c in [("2026-05-01", "2026-05-02"), ("2026-05-15", "2026-05-16")]:
            pit_append(tmp_path, "fund", date=d, payload_file=_payload(tmp_path),
                       collected_at=c, publication_lag_days=0)
        latest = pit_latest(tmp_path, "fund", "2026-06-01")
        assert latest is not None and latest.date == "2026-05-15"

    def test_none_when_nothing_published(self, tmp_path):
        pit_append(tmp_path, "fund", date="2026-06-01",
                   payload_file=_payload(tmp_path), collected_at="2026-06-01",
                   publication_lag_days=30)  # not yet effective
        # 'no PIT data published' must be None (missing), never silently zero
        assert pit_latest(tmp_path, "fund", "2026-06-05") is None

    def test_empty_source(self, tmp_path):
        assert pit_visible(tmp_path, "absent", "2026-06-01") == []
        assert pit_latest(tmp_path, "absent", "2026-06-01") is None


class TestManifest:
    def test_append_is_additive(self, tmp_path):
        pit_append(tmp_path, "si", date="2026-05-01",
                   payload_file=_payload(tmp_path), collected_at="2026-05-02",
                   publication_lag_days=0)
        pit_append(tmp_path, "si", date="2026-05-08",
                   payload_file=_payload(tmp_path), collected_at="2026-05-09",
                   publication_lag_days=0)
        assert len(pit_visible(tmp_path, "si", "2026-06-01")) == 2

    def test_sha_recorded(self, tmp_path):
        row = pit_append(tmp_path, "si", date="2026-05-01",
                         payload_file=_payload(tmp_path, content=b"hello"),
                         collected_at="2026-05-02")
        import hashlib
        assert row.sha256 == hashlib.sha256(b"hello").hexdigest()[:16]
