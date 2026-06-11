from __future__ import annotations

from renquant_pipeline.kernel.selection import passes_sector_guard


def test_sector_guard_ignores_unmapped_held_tickers_for_other_sectors() -> None:
    assert passes_sector_guard(
        "XYZ",
        ["AAPL", "JNJ"],
        {"XYZ": "Energy", "AAPL": "Tech"},
        max_per_sector=2,
        defensive_set=set(),
    )


def test_sector_guard_still_blocks_unmapped_candidate() -> None:
    assert not passes_sector_guard(
        "XYZ",
        ["AAPL"],
        {"AAPL": "Tech"},
        max_per_sector=2,
        defensive_set=set(),
    )


def test_sector_guard_still_counts_mapped_held_same_sector() -> None:
    assert not passes_sector_guard(
        "XYZ",
        ["XOM", "CVX", "JNJ"],
        {"XYZ": "Energy", "XOM": "Energy", "CVX": "Energy"},
        max_per_sector=2,
        defensive_set=set(),
    )
