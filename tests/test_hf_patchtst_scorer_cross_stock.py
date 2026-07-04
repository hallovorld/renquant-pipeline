"""Regression: the 2026-06-15 HF-PatchTST silent mis-score incident (campaign A1).

HFPatchTSTPanelScorer.load() built the model with use_distributional_head only,
never the cross_stock_attn / film_regime flags the checkpoint was trained with.
With load_state_dict(strict=False), a cross-stock model's cross_stock.* tensors
loaded as "unexpected" and were DROPPED, so the forward pass ran through the
channel-independent baseline — producing wrong scores, and never an error.

The fix was shipped 2026-06-15 to the umbrella mirror only (RenQuant commits
6cb2d79 + 1a91680); this file guards the port to the live authority copy
(renquant-pipeline). Incident fixture: a checkpoint whose cross-stock layer is
NOT identity (CrossStockAttentionLayer is identity-at-init via its alpha gate,
so a trained-like perturbation is required for the mis-score to be observable —
exactly as with the real trained xstock model).

Contract pinned here:
  1. The OLD loader semantics mis-score the fixture (tensors dropped + scores
     diverge) — proves the fixture reconstructs the incident.
  2. The fixed loader reconstructs the layer and scores faithfully.
  3. A baseline checkpoint still loads clean (no false positive).
  4. Unexpected tensors FAIL LOUD (never silently dropped).
  5. Declared-but-missing component weights FAIL CLOSED (never a random layer).
"""
from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")
hf_trainer = pytest.importorskip(
    "renquant_model_patchtst.hf_trainer",
    reason="canonical HFPatchTSTRanker (renquant-model) is required to build "
           "the incident fixture",
)

from transformers import PatchTSTConfig  # noqa: E402

from renquant_pipeline.kernel.panel_pipeline.hf_patchtst_scorer import (  # noqa: E402
    HFPatchTSTPanelScorer,
)


def _cfg() -> PatchTSTConfig:
    return PatchTSTConfig(
        num_input_channels=4, context_length=8, patch_length=2, patch_stride=2,
        d_model=16, num_attention_heads=2, num_hidden_layers=1, ffn_dim=32)


def _make_incident_model():
    """Cross-stock ranker whose cross_stock layer is trained-like (non-identity).

    CrossStockAttentionLayer initializes as an exact identity (alpha=0, zeroed
    out-projections), so a freshly built layer would mask the bug: dropping it
    would not change scores. The live incident model was TRAINED — give the
    layer real weight so dropping it mis-scores, as it did live.
    """
    torch.manual_seed(20260615)
    cfg = _cfg()
    model = hf_trainer.HFPatchTSTRanker(
        cfg, use_distributional_head=True, use_cross_stock_attn=True)
    with torch.no_grad():
        for p in model.cross_stock.parameters():
            p.add_(torch.randn_like(p) * 0.05)
        model.cross_stock.alpha.fill_(1.0)
    model.eval()
    return cfg, model


def _save_ckpt(tmp_path, cfg, model, *, declared_flags=None):
    """Save a scorer-format .pt. declared_flags overrides let a test lie about
    the architecture flags (to exercise the fail-loud / fail-closed guards)."""
    flags = {
        "uses_distributional_head": True,
        "uses_film_regime": False,
        "uses_cross_stock_attn": True,
    }
    if declared_flags:
        flags.update(declared_flags)
    p = tmp_path / "hf_patchtst_model.pt"
    torch.save({
        "state_dict": model.state_dict(), "config_dict": cfg.to_dict(),
        "feature_cols": ["f0", "f1", "f2", "f3"], "seq_len": 8,
        "label_col": "fwd_60d_excess", "best_val_ic": 0.0,
        **flags,
    }, p)
    return p


def _panel_input() -> "torch.Tensor":
    torch.manual_seed(7)
    return torch.randn(5, 8, 4)  # (N tickers, seq_len, n_channels)


def test_incident_fixture_old_loader_semantics_mis_score(tmp_path):
    """Pin the OLD (pre-06-15) load() semantics as BROKEN on the fixture.

    Reproduces the removed code path verbatim: construct with the dist flag
    only, load strict=False, swallow the result. Asserts (a) the cross-stock
    tensors are silently dropped and (b) the forward pass produces DIFFERENT
    scores than the trained model — the silent mis-score itself. If this test
    ever fails, the fixture no longer reconstructs the incident and the other
    tests here lose their teeth.
    """
    cfg, true_model = _make_incident_model()
    p = _save_ckpt(tmp_path, cfg, true_model)
    ckpt = torch.load(p, map_location="cpu", weights_only=False)

    # ── the pre-fix load() semantics, verbatim ──
    old_model = hf_trainer.HFPatchTSTRanker(
        PatchTSTConfig(**ckpt["config_dict"]),
        use_distributional_head=ckpt.get("uses_distributional_head", False))
    load_result = old_model.load_state_dict(ckpt["state_dict"], strict=False)
    old_model.eval()

    # (a) the checkpoint's cross-stock tensors were dropped as "unexpected"
    dropped_roots = {k.split(".")[0] for k in load_result.unexpected_keys}
    assert dropped_roots == {"cross_stock"}

    # (b) and the crippled model mis-scores vs the trained model
    x = _panel_input()
    with torch.no_grad():
        old_scores = old_model(x)["score"]
        true_scores = true_model(x)["score"]
    assert not torch.allclose(old_scores, true_scores), (
        "incident fixture no longer distinguishes the baseline path from the "
        "cross-stock path — the regression guard is void")


def test_fixed_loader_reconstructs_cross_stock_and_scores_faithfully(tmp_path):
    cfg, true_model = _make_incident_model()
    scorer = HFPatchTSTPanelScorer.load(_save_ckpt(tmp_path, cfg, true_model))
    assert scorer._model.cross_stock is not None
    x = _panel_input()
    with torch.no_grad():
        got = scorer._model(x)["score"]
        want = true_model(x)["score"]
    assert torch.allclose(got, want), (
        "loaded model does not reproduce the trained model's scores")


def test_baseline_checkpoint_loads_without_cross_stock(tmp_path):
    torch.manual_seed(20260615)
    cfg = _cfg()
    model = hf_trainer.HFPatchTSTRanker(cfg, use_distributional_head=True)
    model.eval()
    p = _save_ckpt(tmp_path, cfg, model,
                   declared_flags={"uses_cross_stock_attn": False})
    scorer = HFPatchTSTPanelScorer.load(p)
    assert scorer._model.cross_stock is None


def test_unexpected_tensor_fails_loud(tmp_path):
    # cross-stock weights in the checkpoint but the flag says baseline → the
    # cross_stock.* tensors are unexpected; loader must refuse, not silently
    # drop (this IS the incident shape).
    cfg, true_model = _make_incident_model()
    p = _save_ckpt(tmp_path, cfg, true_model,
                   declared_flags={"uses_cross_stock_attn": False})
    with pytest.raises(ValueError, match="did not reconstruct"):
        HFPatchTSTPanelScorer.load(p)


@pytest.mark.parametrize(
    ("flag_name", "state_root"),
    [
        ("uses_cross_stock_attn", "cross_stock"),
        ("uses_film_regime", "film"),
    ],
)
def test_missing_component_weights_fail_closed(tmp_path, flag_name, state_root):
    # The checkpoint DECLARES an optional component but carries no tensors for
    # it. Loading must FAIL CLOSED — never score through a randomly
    # initialized layer.
    torch.manual_seed(20260615)
    cfg = _cfg()
    model = hf_trainer.HFPatchTSTRanker(
        cfg, use_distributional_head=False,
        use_film_regime=False, use_cross_stock_attn=False)
    p = _save_ckpt(tmp_path, cfg, model, declared_flags={
        "uses_distributional_head": False,
        "uses_film_regime": flag_name == "uses_film_regime",
        "uses_cross_stock_attn": flag_name == "uses_cross_stock_attn",
    })
    with pytest.raises(ValueError, match=state_root):
        HFPatchTSTPanelScorer.load(p)
