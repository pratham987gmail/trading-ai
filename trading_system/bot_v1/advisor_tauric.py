"""Paper-only second opinion on the momentum shortlist, from TauricResearch/TradingAgents.

TradingAgents (Apache-2.0, https://github.com/TauricResearch/TradingAgents) runs a team of LLM analysts
(fundamentals, news, sentiment, technicals) that debate and return a 5-tier rating for one ticker on one
date: Buy / Overweight / Hold / Underweight / Sell.

Rules of this module (protocol: research/advisor_tauric_prereg.md):
  * It NEVER places, sizes or blocks an order. It only appends ratings to a log.
  * It is never back-tested: an LLM has read the history it would be "predicting", so past dates leak.
    Only ratings logged before the outcome existed count.
  * It is judged later by ``score()`` against the forward return of the same shortlist.

Install (optional; not needed by the bot):  pip install "git+https://github.com/TauricResearch/TradingAgents.git"
Needs one LLM key in the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY ...).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pandas as pd

RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
BULL = {"Buy", "Overweight"}
LOGNAME = "advisor_tauric.jsonl"
TOP_N = 15                      # the shortlist size in the pre-registration


def to_yahoo(symbol: str) -> str:
    """NSE symbol -> the Yahoo ticker TradingAgents expects (RELIANCE -> RELIANCE.NS)."""
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.NS"


def normalise_rating(raw) -> str:
    """Map whatever the graph returned to one of RATINGS, else 'REVIEW' (unparseable; never guessed)."""
    t = str(raw or "").strip().replace("*", "").title()
    return t if t in RATINGS else "REVIEW"


def _default_graph():
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except ImportError as e:
        raise RuntimeError('TradingAgents is not installed. pip install "git+https://github.com/TauricResearch/TradingAgents.git"') from e
    cfg = dict(DEFAULT_CONFIG)
    prov = os.environ.get("TRADINGAGENTS_LLM_PROVIDER")
    if not prov:                                   # pick a provider from whichever key exists
        for env, name, deep, quick in (("ANTHROPIC_API_KEY", "anthropic", "claude-opus-5-5", "claude-sonnet-5-5"),
                                       ("OPENAI_API_KEY", "openai", None, None),
                                       ("GOOGLE_API_KEY", "google", None, None)):
            if os.environ.get(env):
                cfg["llm_provider"] = name
                if deep:
                    cfg["deep_think_llm"], cfg["quick_think_llm"] = deep, quick
                break
        else:
            raise RuntimeError("No LLM key found (set ANTHROPIC_API_KEY, OPENAI_API_KEY or GOOGLE_API_KEY).")
    cfg["max_debate_rounds"] = 1
    cfg["max_risk_discuss_rounds"] = 1
    return TradingAgentsGraph(debug=False, config=cfg)


def _done(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
            out.add((r["symbol"], r["asof"]))
        except (ValueError, KeyError):
            continue
    return out


def advise(symbols: Iterable[str], asof: str, outdir: Path, graph=None,
           log: Callable[[str], None] = print) -> list[dict]:
    """Rate each symbol as of ``asof`` (YYYY-MM-DD) and append to the log. Idempotent per (symbol, asof).

    One failing symbol is recorded as an ERROR row and the rest continue. Returns the new rows only.
    """
    path = Path(outdir) / LOGNAME
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = _done(path)
    todo = [s for s in symbols if (s, asof) not in seen]
    if not todo:
        log(f"advisor: all {len(list(symbols))} names already rated for {asof}")
        return []
    graph = graph or _default_graph()
    rows = []
    for s in todo:
        row = {"symbol": s, "asof": asof, "logged_at": datetime.now().isoformat(timespec="seconds")}
        try:
            _state, signal = graph.propagate(to_yahoo(s), asof)
            row["rating"] = normalise_rating(signal)
        except Exception as e:                      # never let an advisor failure reach the bot
            row.update(rating="ERROR", error=f"{type(e).__name__}: {e}"[:300])
        rows.append(row)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        log(f"advisor: {s:<12} {row['rating']}")
    return rows


def load(outdir: Path) -> pd.DataFrame:
    path = Path(outdir) / LOGNAME
    if not path.exists():
        return pd.DataFrame(columns=["symbol", "asof", "rating"])
    df = pd.DataFrame([json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])
    df["asof"] = pd.to_datetime(df["asof"])
    return df


def score(ratings: pd.DataFrame, closes: pd.DataFrame, horizon: int = 21, seed: int = 0,
          boots: int = 4000) -> dict:
    """Forward-return spread of bullish (Buy/Overweight) vs other ratings, one cohort per ``asof``.

    ``closes``: adjusted closes, dates x symbols. Forward return runs from the first session AFTER
    ``asof`` to ``horizon`` sessions later (the same fill convention as the sandbox). The statistic is the
    mean over cohorts of (mean bullish return - mean non-bullish return); P is a bootstrap over cohorts.
    Cohorts without both groups, and ERROR/REVIEW rows, are excluded and counted.
    """
    r = ratings[ratings.rating.isin(RATINGS)]
    idx = closes.index
    spreads, n_obs = [], 0
    for asof, g in r.groupby("asof"):
        i = idx.searchsorted(pd.Timestamp(asof), side="right")      # first session after asof
        if i + horizon >= len(idx):
            continue                                                # outcome not in yet
        fwd = closes.iloc[i + horizon] / closes.iloc[i] - 1
        f = fwd.reindex(g.symbol).to_numpy()
        ok = np.isfinite(f)
        bull = g.rating.isin(BULL).to_numpy()[ok]
        f = f[ok]
        if bull.all() or (~bull).all():
            continue
        spreads.append(f[bull].mean() - f[~bull].mean()); n_obs += len(f)
    sp = np.array(spreads)
    if len(sp) == 0:
        return dict(cohorts=0, obs=0, spread=np.nan, p=np.nan, note="no settled cohorts with both groups yet")
    boot = np.random.default_rng(seed).choice(sp, (boots, len(sp))).mean(1)
    return dict(cohorts=len(sp), obs=n_obs, spread=float(sp.mean()), p=float((boot <= 0).mean()),
                excluded=int((~ratings.rating.isin(RATINGS)).sum()))


def passes(s: dict, min_cohorts: int = 6, min_obs: int = 90, max_p: float = 0.10) -> bool:
    """Pre-registered promotion mark (advisor_tauric_prereg.md)."""
    return s.get("cohorts", 0) >= min_cohorts and s.get("obs", 0) >= min_obs and s.get("spread", -1) > 0 and s.get("p", 1) < max_p
