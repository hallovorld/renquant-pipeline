"""R2 audit (MED): the sequence (PatchTST) scoring path must fail closed on a
collapsed raw-score surface, like PanelScorer.score does for XGB.

A degenerate cross-section (thin-OHLCV day, feature-builder regression) yields
uniform scores that rank nothing — the BL-1 failure mode. A model that cannot
differentiate names must not drive buys.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from renquant_pipeline.kernel.panel_pipeline.job_panel_scoring import ApplyScoresTask


class _CollapsedScorer:
    requires_history = True
    seq_len = 2
    feature_cols = ["KMID", "KLEN"]
    metadata = {"kind": "hf_patchtst"}

    def score_with_history(self, panel_history, target_tickers):
        # every name gets the same score → collapsed / non-discriminating
        return pd.Series({t: -0.18 for t in target_tickers}, dtype=float)


def _ctx(scorer):
    tickers = ["AAA", "BBB", "CCC"]
    cands = [SimpleNamespace(ticker=t, panel_score=None, rank_score=None)
             for t in tickers]
    return SimpleNamespace(
        _panel_scorer=scorer,
        _panel_matrix=pd.DataFrame({"f": [0.0, 0.0, 0.0]}, index=tickers),
        _panel_history=pd.DataFrame({"ticker": tickers,
                                     "date": [pd.Timestamp("2026-06-10")] * 3,
                                     "KMID": [0.0, 0.0, 0.0]}),
        candidates=cands,
        today=pd.Timestamp("2026-06-10"),
        config={},
        counters={},
    )


def test_collapsed_scores_fail_closed() -> None:
    ctx = _ctx(_CollapsedScorer())
    ApplyScoresTask().run(ctx)
    assert ctx.skip_buys is True
    assert ctx.buy_blocked is True
    assert ctx.candidates == []
    assert ctx._panel_scoring_fail_reason == "panel_score_collapsed"


# (varied / discriminating scores must NOT fail closed — already covered
# end-to-end by the happy-path panel-pipeline lift tests, which score a real
# varied cross-section through ApplyScoresTask without tripping this guard.)
