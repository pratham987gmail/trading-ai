"""The strategies, each a self-contained learner that only ever sees a View.

"Blind" (LEARN) sandboxes: the strategy starts with a pre-declared MENU of rule variants and no
idea which works. Every variant runs as a paper "shadow" book from the first day data allows. At
fixed review dates the strategy picks the variant with the best TRAILING record (champion) and
only puts the ₹ book behind it if the trailing evidence passes a gate. Nothing is chosen with
knowledge of anything after the review date.

"Hindsight" (FIXED) sandboxes: today's bot rule, applied from 2015 with no learning. Its settings
were chosen with data up to 2026, so it is the optimistic reference, not evidence.

Menus (pre-declared before any sandbox was run; written into sandbox_prereg.md):
  MOM   lookback (short,long) in {(63,126),(126,252),(189,252)} x top_n {15,30} x trend {200-SMA, none}
  TREND NIFTY fund vs cash, SMA in {50,100,150,200} or always-in
  CONDOR NIFTY expiry-eve iron condor, sigma wings, short delta {0.10,0.15,0.20} x min VIX {0,16,18,20}
"""
from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pandas as pd

from bot_v1 import condor as C
from bot_v1 import momentum as M
from bot_v1.config import CondorParams, MomentumParams

from .engine import CondorBook, EquityBook
from .store import INDEX_FUND, View, lot_size

SHADOW_COST = 0.00175            # per side on turnover (research protocol 0.35% round trip)
TRAIL = 1260                     # 5 years of sessions for the equity selectors
MIN_TRAIL = 756                  # need >= 3 years of shadow record before trusting it


def _sharpe(x: np.ndarray) -> float:
    sd = x.std()
    return float(x.mean() / sd * math.sqrt(252)) if sd > 0 else -np.inf


def _cagr(x: np.ndarray) -> float:
    return float(np.exp(np.log1p(x).sum() * 252 / len(x)) - 1) if len(x) else 0.0


# ---------------------------------------------------------------------------------- shadows
class Shadow:
    """Fractional paper book for one variant. Decide at close t, fill at close t+1 with cost."""

    def __init__(self, name: str):
        self.name, self.w, self.cash, self.pending = name, {}, 1.0, None
        self.rets: list[float] = []
        self.started = False
        self.targets: list[str] = []

    def step(self, v: View, colix: dict) -> None:
        r = v.returns_today()
        ret = 0.0
        if self.w:
            g = {k: w * (1 + r[colix[k]]) for k, w in self.w.items()}
            port = sum(g.values()) + self.cash
            ret = port - 1
            self.w = {k: x / port for k, x in g.items()}; self.cash /= port
        if self.pending is not None:
            tgt = self.pending; self.pending = None
            new = {k: 1.0 / len(tgt) for k in tgt} if tgt else {}
            turn = sum(abs(new.get(k, 0) - self.w.get(k, 0)) for k in set(new) | set(self.w))
            ret = (1 + ret) * (1 - SHADOW_COST * turn) - 1
            self.w, self.cash = new, 1.0 - sum(new.values())
            self.started = True
        if self.started:
            self.rets.append(ret)

    def decide(self, v: View) -> None:
        raise NotImplementedError


class MomShadow(Shadow):
    def __init__(self, short: int, long: int, top: int, trend: bool):
        super().__init__(f"MOM_{short}_{long}_top{top}_{'sma200' if trend else 'nofilter'}")
        self.p = replace(MomentumParams(), lookback_short=short, lookback_long=long, top_n=top)
        self.trend, self.invested = trend, False

    def decide(self, v: View, universe: list[str]) -> None:
        if not v.is_month_end():
            return
        p = self.p
        n = max(p.lookback_long, p.vol_window, p.min_history) + 6
        closes = v.adj_window(universe, n)
        try:
            if self.trend:
                sig = M.decide(closes, v.nifty(400), v.today, self.invested, list(self.w), p)
                action, tg = sig.action, sig.targets
            else:
                rank, _ = M.rank_universe(closes, v.today, p)
                top = rank.symbol.head(p.top_n).tolist()
                action = "REBALANCE" if (v.today.month in p.rebalance_months or not self.invested) else "HOLD"
                tg = top if action == "REBALANCE" else list(self.w)
        except ValueError:
            return
        if action in ("HOLD", "STAY_IN_CASH"):
            return
        self.pending, self.targets, self.invested = tg, tg, bool(tg)


class TrendShadow(Shadow):
    def __init__(self, sma: int | None):
        super().__init__(f"TREND_sma{sma}" if sma else "TREND_always_in")
        self.sma = sma

    def decide(self, v: View, universe=None) -> None:
        if not v.is_month_end() or not np.isfinite(v.fund_price()):
            return
        if self.sma:
            n = v.nifty(self.sma)
            if len(n) < self.sma:
                return
            tg = [INDEX_FUND] if n.iloc[-1] >= n.mean() else []
        else:
            tg = [INDEX_FUND]
        if tg != self.targets or not self.started:
            self.pending, self.targets = tg, tg


# ---------------------------------------------------------------------------------- equity learner
class EquityLearner:
    """Runs shadows; at reviews (June/Dec month-ends, and the first month-end in the book) picks the
    champion by trailing 5y Sharpe; follows it with real money if its trailing 5y CAGR beats the
    index fund's (gate), otherwise holds the index fund."""

    def __init__(self, name: str, shadows: list[Shadow], capital: float, gate: bool, warmup_from: str,
                 needs_universe: bool, top_n: int = 30, freeze: bool = False, trail: int | None = TRAIL):
        self.name, self.shadows, self.gate = name, shadows, gate
        self.freeze, self.trail = freeze, trail     # freeze: choose once on the first live day, never again
        self.book = EquityBook(capital, top_n)
        self.warmup_from, self.needs_universe = warmup_from, needs_universe
        self.log: list[dict] = []
        self.champion: Shadow | None = None
        self.follow_fund = False
        self.fund_rets: list[float] = []
        self.current: list[str] | None = None
        self.colix = None

    def on_close(self, v: View, book: EquityBook) -> None:
        if self.colix is None:
            self.colix = v._s.col_ix  # index lookup table only (column order), no data
        fr = v.returns_today()[self.colix[INDEX_FUND]]
        self.fund_rets.append(float(fr) if np.isfinite(fr) else 0.0)
        uni = v.universe() if (self.needs_universe and v.is_month_end()) else None
        for s in self.shadows:
            s.step(v, self.colix)
            s.decide(v, uni)
        if not book.active or (self.champion is not None and not v.is_month_end()):
            return                      # first decision on the first live session, then month-ends only
        review = self.champion is None or (not self.freeze and v.today.month in (6, 12))
        if review:
            scores = {}
            for s in self.shadows:
                x = np.asarray(s.rets[-self.trail:] if self.trail else s.rets)
                if len(x) >= MIN_TRAIL or len(self.shadows) == 1:
                    scores[s.name] = _sharpe(x) if len(x) > 20 else 0.0
            if scores:
                best = max(scores, key=scores.get)
                self.champion = next(s for s in self.shadows if s.name == best)
                cx = np.asarray(self.champion.rets[-self.trail:] if self.trail else self.champion.rets)
                fx = np.asarray(self.fund_rets[-len(cx):]) if len(cx) else np.array([])
                excess = _cagr(cx) - _cagr(fx) if len(cx) else 0.0
                self.follow_fund = self.gate and not (excess > 0)
                self.log.append(dict(date=str(v.today.date()), event="review", champion=best,
                                     sharpe=round(scores[best], 3), excess_vs_fund=round(excess, 4),
                                     follow="INDEX_FUND" if self.follow_fund else best,
                                     ranking={k: round(x, 3) for k, x in sorted(scores.items(), key=lambda kv: -kv[1])}))
        if self.champion is None:
            return
        want = [INDEX_FUND] if self.follow_fund else list(self.champion.targets)
        # follow the champion's latest decision (its pending list if it decided today)
        if not self.follow_fund and self.champion.pending is not None:
            want = list(self.champion.pending)
        if self.current is None or sorted(want) != sorted(self.current):
            book.set_target(want)
            self.log.append(dict(date=str(v.today.date()), event="target", n=len(want), names=sorted(want)[:40]))
            self.current = want


class BuyHold:
    def __init__(self, capital: float):
        self.name, self.book, self.warmup_from, self.log, self.done = "BENCH_NIFTY_FUND", EquityBook(capital, 1), "2014-12-01", [], False

    def on_close(self, v: View, book) -> None:
        if book.active and not self.done:
            book.set_target([INDEX_FUND]); self.done = True
            self.log.append(dict(date=str(v.today.date()), event="target", names=[INDEX_FUND]))


# ---------------------------------------------------------------------------------- condor learner
class CondorShadow:
    def __init__(self, delta: float, min_vix: float):
        self.name = f"CONDOR_d{int(delta * 100)}_vix{int(min_vix)}"
        self.p = CondorParams(min_vix=min_vix, short_delta=delta)
        self.open: list[tuple] = []          # (settle_day, plan)
        self.r: list[float] = []             # settled trade R, in settlement order


def _chain(rows: pd.DataFrame, expiry: pd.Timestamp) -> list[dict]:
    ch = rows[rows.expiry_date == expiry]
    piv = ch.pivot_table(index="strike", columns="option_type", values="close", aggfunc="last")
    if not {"CE", "PE"} <= set(piv.columns):
        return []
    hs = lambda x: 0.25 + 0.003 * x
    return [dict(strike=float(k), ce_bid=max(r.CE - hs(r.CE), 0.05), ce_ask=r.CE + hs(r.CE),
                 pe_bid=max(r.PE - hs(r.PE), 0.05), pe_ask=r.PE + hs(r.PE)) for k, r in piv.dropna().iterrows()]


def _settle_r(plan: dict, S: float) -> float:
    L = {(l["kind"], l["side"]): l["strike"] for l in plan["legs"]}
    payout = (max(S - L["CE", "SELL"], 0) - max(S - L["CE", "BUY"], 0)
              + max(L["PE", "SELL"] - S, 0) - max(L["PE", "BUY"] - S, 0))
    units = plan["lot_size"] * plan["lots"]
    from bot_v1.report import condor_charges_points
    net = plan["credit_real"] - payout - condor_charges_points(plan)
    return net / plan["max_loss_pts"]


class CondorLearner:
    """At each expiry eve: every shadow variant paper-trades; the ₹ book follows the champion
    (best trailing avg R among variants with >= MIN_N settled trades) only when the champion's own
    trailing record passes the live promotion rule (PF >= 1.15 and bootstrap P(avg R <= 0) < 0.10)."""

    MIN_N = 20

    def __init__(self, name: str, shadows: list[CondorShadow], capital: float, gate: bool, freeze: bool = False):
        self.name, self.shadows, self.gate = name, shadows, gate
        self.freeze, self.frozen = freeze, None      # frozen = (champion, gate_ok, stats) fixed on first live eve
        self.book = CondorBook(capital)
        self.warmup_from = "2015-01-01"
        self.log: list[dict] = []

    def on_close(self, v: View, book: CondorBook) -> None:
        nifty = v.nifty(5)
        if len(nifty) == 0 or nifty.index[-1] != v.today:
            return
        S = float(nifty.iloc[-1])
        for s in self.shadows:                                      # settle shadows expiring today
            keep = []
            for settle, plan in s.open:
                if settle == v.today:
                    s.r.append(_settle_r(plan, S))
                else:
                    keep.append((settle, plan))
            s.open = keep
        nxt = v.next_session()
        rows = v.option_chain()
        if nxt is None or rows.empty or not v.vix_is_fresh():
            return
        cal = v._s.dates                                           # exchange calendar (published in advance)
        exps = [e for e in sorted(rows.expiry_date.unique()) if e >= nxt]
        target = None
        for e in exps:
            last = cal[cal <= e]
            if len(last) and last[-1] == nxt and (nxt - v.today).days <= 7:
                target = e; break
        if target is None:
            return
        chain = _chain(rows, target)
        if not chain:
            return
        vix = v.vix(); dte = float((nxt - v.today).days); lot = lot_size(target)
        plans = {}
        for s in self.shadows:
            try:
                pl = C.plan_condor(chain, S, vix, str(pd.Timestamp(target).date()), dte, lot, s.p, wing_mode="sigma").to_dict()
            except C.NoTrade:
                continue
            s.open.append((nxt, pl)); plans[s.name] = pl
        if not book.active:
            return
        # choose champion from settled history only
        elig = [s for s in self.shadows if len(s.r) >= (self.MIN_N if self.gate else 0)]
        if not elig:
            self.log.append(dict(date=str(v.today.date()), event="eve", decision="learning (no variant has 20 settled trades)"))
            return
        if self.frozen is not None:
            champ, ok, pf, p, x = self.frozen
            row = dict(date=str(v.today.date()), event="eve", vix=round(vix, 2), champion=champ.name, frozen=True,
                       gate=ok, decision="")
            return self._act(row, champ, ok, plans, book, v, nxt)
        champ = max(elig, key=lambda s: np.mean(s.r) if s.r else 0.0)
        x = np.asarray(champ.r)
        ok, pf, p = True, float("nan"), float("nan")
        if self.gate:
            gl = -x[x < 0].sum()
            pf = float(x[x > 0].sum() / gl) if gl > 0 else float("inf")
            rng = np.random.default_rng(v.t)
            p = float((rng.choice(x, (2000, len(x))).mean(1) <= 0).mean())
            ok = pf >= 1.15 and p < 0.10
        row = dict(date=str(v.today.date()), event="eve", vix=round(vix, 2), champion=champ.name, n=len(x),
                   avg_r=round(float(x.mean()), 4) if len(x) else None, pf=round(pf, 3), p=round(p, 3), gate=ok)
        if self.freeze:
            self.frozen = (champ, ok, pf, p, x.copy())
            row["event"] = "freeze"
            row["ranking"] = {s.name: round(float(np.mean(s.r)), 4) for s in self.shadows if s.r}
        self._act(row, champ, ok, plans, book, v, nxt)

    def _act(self, row, champ, ok, plans, book, v, nxt):
        pl = plans.get(champ.name)
        if not ok:
            row["decision"] = "no trade (champion fails promotion rule)"
        elif pl is None:
            row["decision"] = "no trade (champion's rule does not fire today)"
        else:
            eq = book.cash
            ml1 = pl["max_loss_pts"] * pl["lot_size"]
            if ml1 > 0.25 * eq:
                row["decision"] = f"no trade (1 lot risks ₹{ml1:,.0f} > 25% of equity)"
            else:
                pl = dict(pl, lots=max(1, int(0.10 * eq // ml1)))
                book.open_condor(pl, nxt, v.today)
                row["decision"] = f"TRADE {pl['lots']} lot(s), risk ₹{ml1 * pl['lots']:,.0f}"
        self.log.append(row)


# ---------------------------------------------------------------------------------- factory
def make(name: str, capital: float = 100_000.0):
    if name == "BENCH_NIFTY_FUND":
        return BuyHold(capital)
    if name == "TREND_LEARN":
        return EquityLearner(name, [TrendShadow(n) for n in (50, 100, 150, 200, None)], capital, gate=False,
                             warmup_from="2006-01-01", needs_universe=False, top_n=1)
    if name == "MOM_LEARN":
        sh = [MomShadow(a, b, n, f) for a, b in ((63, 126), (126, 252), (189, 252)) for n in (15, 30) for f in (True, False)]
        return EquityLearner(name, sh, capital, gate=True, warmup_from="2006-01-01", needs_universe=True)
    if name == "MOM_FIXED_HINDSIGHT":
        return EquityLearner(name, [MomShadow(126, 252, 30, True)], capital, gate=False,
                             warmup_from="2013-12-01", needs_universe=True)
    if name == "CONDOR_LEARN":
        return CondorLearner(name, [CondorShadow(d, m) for d in (0.10, 0.15, 0.20) for m in (0, 16, 18, 20)], capital, gate=True)
    if name == "CONDOR_FIXED_HINDSIGHT":
        return CondorLearner(name, [CondorShadow(0.15, 18)], capital, gate=False)
    if name == "TREND_FROZEN":
        return EquityLearner(name, [TrendShadow(n) for n in (50, 100, 150, 200, None)], capital, gate=False,
                             warmup_from="2006-01-01", needs_universe=False, top_n=1, freeze=True, trail=None)
    if name == "MOM_FROZEN":
        sh = [MomShadow(a, b, n, f) for a, b in ((63, 126), (126, 252), (189, 252)) for n in (15, 30) for f in (True, False)]
        return EquityLearner(name, sh, capital, gate=True, warmup_from="2006-01-01", needs_universe=True,
                             freeze=True, trail=None)
    if name == "CONDOR_FROZEN":
        return CondorLearner(name, [CondorShadow(d, m) for d in (0.10, 0.15, 0.20) for m in (0, 16, 18, 20)], capital,
                             gate=True, freeze=True)
    raise KeyError(name)


FROZEN = ["BENCH_NIFTY_FUND", "TREND_FROZEN", "MOM_FROZEN", "CONDOR_FROZEN", "TREND_LEARN", "MOM_LEARN",
          "CONDOR_LEARN", "MOM_FIXED_HINDSIGHT", "CONDOR_FIXED_HINDSIGHT"]
ALL = ["BENCH_NIFTY_FUND", "TREND_LEARN", "MOM_LEARN", "MOM_FIXED_HINDSIGHT", "CONDOR_LEARN", "CONDOR_FIXED_HINDSIGHT"]
