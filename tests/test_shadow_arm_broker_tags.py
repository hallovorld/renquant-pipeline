"""D6-§2a P-1b: shadow-arm broker tags for the two-arm admission experiment.

Pins that ``alpaca_shadow_a`` / ``alpaca_shadow_b`` are accepted by the
broker allowlist (in BOTH state_paths copies, per this repo's duplication
pattern), that each tag resolves to its own isolated state file + runs.db,
that neither collides with the legacy ``alpaca_shadow`` tag owned by the
untouched Step-4 ops shadow, and that rejection of unknown tags is
unchanged (fail-closed by design).

Spec: renquant-orchestrator #443
doc/design/2026-07-09-governor-prereg-replay-protocol.md §2a.
"""
from __future__ import annotations

import json

import pytest

from renquant_pipeline import kernel
from renquant_pipeline import state_paths as top_state_paths
from renquant_pipeline.kernel import state_paths as kernel_state_paths

SHADOW_ARM_TAGS = ("alpaca_shadow_a", "alpaca_shadow_b")
BOTH_COPIES = (top_state_paths, kernel_state_paths)


@pytest.mark.parametrize("mod", BOTH_COPIES, ids=("top", "kernel"))
@pytest.mark.parametrize("tag", SHADOW_ARM_TAGS)
def test_shadow_arm_tags_accepted_in_both_copies(mod, tag, tmp_path) -> None:
    assert tag in mod.ALLOWED_BROKERS
    assert mod.live_state_path(tmp_path, tag).name == f"live_state.{tag}.json"
    assert mod.runs_db_path(tmp_path / "runs.db", tag).name == f"runs.{tag}.db"


def test_allowlist_copies_stay_identical() -> None:
    """The two hand-duplicated copies must never drift apart."""
    assert top_state_paths.ALLOWED_BROKERS == kernel_state_paths.ALLOWED_BROKERS


@pytest.mark.parametrize("mod", BOTH_COPIES, ids=("top", "kernel"))
def test_arm_state_paths_distinct_and_disjoint_from_legacy_shadow(mod, tmp_path) -> None:
    """Arms A/B and the legacy alpaca_shadow each get their own files."""
    tags = ("alpaca_shadow", "alpaca_shadow_a", "alpaca_shadow_b")
    state_files = {mod.live_state_path(tmp_path, t) for t in tags}
    db_files = {mod.runs_db_path(tmp_path / "runs.db", t) for t in tags}
    assert len(state_files) == 3
    assert len(db_files) == 3


@pytest.mark.parametrize("mod", BOTH_COPIES, ids=("top", "kernel"))
def test_runs_db_idempotence_does_not_cross_arm_boundaries(mod, tmp_path) -> None:
    """A prefix tag (alpaca_shadow) must not be mistaken for an arm tag."""
    a_db = mod.runs_db_path(tmp_path / "runs.db", "alpaca_shadow_a")
    assert a_db.name == "runs.alpaca_shadow_a.db"
    # Idempotent for the SAME tag...
    assert mod.runs_db_path(a_db, "alpaca_shadow_a") == a_db
    # ...but an already-tagged arm-A path is not treated as tagged for the
    # legacy prefix tag or arm B (no suffix-prefix confusion).
    assert mod.runs_db_path(tmp_path / "runs.db", "alpaca_shadow").name == "runs.alpaca_shadow.db"
    assert mod.runs_db_path(tmp_path / "runs.db", "alpaca_shadow_b").name == "runs.alpaca_shadow_b.db"


def test_sentinel_write_through_one_arm_is_invisible_to_the_other(tmp_path) -> None:
    """Genuine collision check: state written via arm A never surfaces via
    arm B or legacy alpaca_shadow reads (not just tag-string inequality)."""
    a_path = top_state_paths.live_state_path(tmp_path, "alpaca_shadow_a")
    a_path.write_text(json.dumps({"sentinel": "arm-a-only"}), encoding="utf-8")

    b_read, b_legacy = top_state_paths.resolve_live_state_read(tmp_path, "alpaca_shadow_b")
    assert b_read != a_path
    assert b_legacy is False
    assert not b_read.exists()

    shadow_read, shadow_legacy = top_state_paths.resolve_live_state_read(
        tmp_path, "alpaca_shadow"
    )
    assert shadow_read != a_path
    assert shadow_legacy is False
    assert not shadow_read.exists()

    a_read, a_legacy = top_state_paths.resolve_live_state_read(tmp_path, "alpaca_shadow_a")
    assert a_read == a_path
    assert a_legacy is False
    assert json.loads(a_read.read_text(encoding="utf-8")) == {"sentinel": "arm-a-only"}


@pytest.mark.parametrize("mod", BOTH_COPIES, ids=("top", "kernel"))
@pytest.mark.parametrize(
    "bad_tag",
    ["alpaca_shadow_c", "alpaca-shadow-a", "../alpaca_shadow_a", "shadow_a"],
)
def test_unknown_tags_still_rejected(mod, bad_tag, tmp_path) -> None:
    with pytest.raises(ValueError, match="Unknown broker_name"):
        mod.live_state_path(tmp_path, bad_tag)
    with pytest.raises(ValueError, match="Unknown broker_name"):
        mod.runs_db_path(tmp_path / "runs.db", bad_tag)


def test_kernel_reexport_sees_new_tags() -> None:
    """If the kernel package re-exports state_paths symbols, they must agree."""
    exported = getattr(kernel, "state_paths", None)
    assert exported is not None
    assert SHADOW_ARM_TAGS[0] in exported.ALLOWED_BROKERS
    assert SHADOW_ARM_TAGS[1] in exported.ALLOWED_BROKERS
