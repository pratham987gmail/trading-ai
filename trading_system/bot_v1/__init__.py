"""bot_v1 — two-strategy Upstox bot built on the validated research (Oct 2026).

Strategy A  Momentum30 + NIFTY 200-day trend filter (the only strategy that passed a
            pre-registered design/holdout test: 2006-16 design, 2017-26 holdout,
            +3.3%/yr over NIFTYBEES with a smaller drawdown). Runs PAPER by default;
            LIVE only with explicit per-batch approval and every charter guard.
Strategy B  Expiry-eve NIFTY iron condor (VIX >= 18, ~15-delta shorts, 1-sigma wings).
            Did NOT pass validation (holdout PF 1.52 but P(avg R <= 0) = 0.12), so per
            the charter it is SHADOW-ONLY: it logs what it would trade at real bid/ask
            and never places orders. It is promoted only by the promotion rule in
            ``report.py`` after enough shadow trades.

Nothing here predicts prices. Momentum ranks stocks by past returns; the condor
harvests option premium. Both can and will have losing stretches.
"""
