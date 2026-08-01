"""The AC6 review surface must not vanish silently. (GOAL-5 AC6 R2)

A PR-template checklist item is the weakest kind of control: nothing runs it, and it can
be deleted in a one-line diff that no test notices. That is precisely why it gets a test.
The programme's own register has a name for the failure it would otherwise become —
scaffolding that is deployed and then quietly stops existing.

These do NOT claim the rule is enforced. They claim the review surface is present and
says what it is supposed to say, which is the whole of what R2 delivers.
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = REPO / ".github" / "pull_request_template.md"


def test_the_pr_template_exists():
    assert TEMPLATE.is_file(), (
        "the AC6 review surface is the PR template; if it is gone, R2 is gone")


def test_it_carries_the_AC6_gate_design_item():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "GOAL-5 AC6" in text
    assert "governed override path" in text


def test_it_names_all_THREE_required_properties():
    """identity / expiry / binding. Two of three is a checklist that passes on a gate
    nobody can lift, or one nobody can find."""
    text = TEMPLATE.read_text(encoding="utf-8").lower()
    for prop in ("identity", "expiry", "binding"):
        assert f"**{prop}**" in text, prop


def test_it_defines_a_HARD_gate_rather_than_assuming_the_reader_knows():
    """An undefined 'hard gate' lets every author decide the item is N/A."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "tradeable" in text and "market decision" in text


def test_it_REFUSES_temporary_as_an_expiry():
    """The containment-protocol lesson, in the words that were paid for."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert '"Temporary" is not an expiry' in text


def test_it_points_at_the_CANONICAL_rule_not_a_local_paraphrase():
    """A per-repo copy of a rule drifts from the rule. The item must delegate."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "subrepo-operating-model.md" in text
    assert "2026-07-20-ac6-gate-design-rule.md" in text


def test_it_states_that_this_is_NOT_enforcement():
    """The honest scope. Without this line the item reads as a mechanical guarantee, and
    a reviewer could reasonably assume something downstream checks the bundle. Nothing
    does — measured on orch#690."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "review surface, not enforcement" in text
