"""Unit tests for bot_v1 (no network)."""
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from bot_v1 import condor as C
from bot_v1 import execution as X
from bot_v1 import momentum as M
from bot_v1.config import BotConfig, CondorParams, MomentumParams
from bot_v1.journal import Journal
from bot_v1.upstox_api import UpstoxAPI, UpstoxAPIError

P = MomentumParams()


def _closes(n_days=400, n_sym=40, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2024-01-01", periods=n_days)
    drift = np.linspace(-0.001, 0.002, n_sym)          # higher index = stronger trend
    r = rng.normal(drift, 0.01, size=(n_days, n_sym))
    return pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), index=idx, columns=[f"S{i:02d}" for i in range(n_sym)])


def test_rank_prefers_strong_trends_and_uses_only_past():
    cl = _closes()
    asof = cl.index[350]
    rk, _ = M.rank_universe(cl, asof, P)
    top = rk.symbol.head(10).tolist()
    assert sum(int(s[1:]) >= 25 for s in top) >= 8
    cl2 = cl.copy(); cl2.iloc[351:] = cl2.iloc[351:] * 5          # future change must not alter the ranking
    rk2, _ = M.rank_universe(cl2, asof, P)
    assert rk.symbol.tolist() == rk2.symbol.tolist()


def test_flags_split_like_jump():
    cl = _closes(); cl.iloc[300:, 3] = cl.iloc[300:, 3] / 2        # unadjusted 1:1 bonus look-alike
    rk, flagged = M.rank_universe(cl, cl.index[350], P)
    assert "S03" in flagged and "S03" not in rk.symbol.tolist()


@pytest.mark.parametrize("filter_on,invested,month,expected", [
    (False, True, 3, "EXIT_TO_CASH"), (False, False, 3, "STAY_IN_CASH"),
    (True, False, 3, "ENTER_FROM_CASH"), (True, True, 6, "REBALANCE"), (True, True, 3, "HOLD")])
def test_decide_actions(filter_on, invested, month, expected):
    cl = _closes()
    idx = cl.index
    nifty = pd.Series(np.where(filter_on, np.linspace(100, 200, len(idx)), np.linspace(200, 100, len(idx))), index=idx)
    asof = idx[idx.month == month][-1] if any(idx.month == month) else idx[-1]
    asof = [d for d in idx if d.month == month and d >= idx[260]][-1]
    sig = M.decide(cl, nifty, asof, invested, ["S01"] if invested else [], P)
    assert sig.action == expected
    if expected == "HOLD":
        assert sig.targets == ["S01"]
    if expected in ("EXIT_TO_CASH", "STAY_IN_CASH"):
        assert sig.targets == []


def test_target_shares_skips_unaffordable():
    sh, skipped = M.target_shares(["A", "B"], {"A": 100.0, "B": 90_000.0}, 100_000)
    assert sh == {"A": 500} and skipped == ["B"]


def _chain(spot=25000, step=50):
    rows = []
    for k in np.arange(spot - 1500, spot + 1550, step):
        ce = max(spot - k, 0) + max(5, 120 - abs(k - spot) * 0.15)
        pe = max(k - spot, 0) + max(5, 120 - abs(k - spot) * 0.15)
        rows.append(dict(strike=float(k), ce_bid=ce - 1, ce_ask=ce + 1, pe_bid=pe - 1, pe_ask=pe + 1))
    return rows


def test_condor_no_trade_when_vix_low():
    with pytest.raises(C.NoTrade):
        C.plan_condor(_chain(), 25000, 15.0, "2026-10-20", 1.0, 65, CondorParams())


def test_condor_plan_and_settle():
    for mode in ("sigma", "points100"):
        plan = C.plan_condor(_chain(), 25000, 20.0, "2026-10-20", 1.0, 65, CondorParams(), wing_mode=mode)
        sc = next(l for l in plan.legs if l.side == "SELL" and l.kind == "CE")
        sp = next(l for l in plan.legs if l.side == "SELL" and l.kind == "PE")
        assert sc.strike > 25000 > sp.strike
        assert plan.credit_real < plan.credit_mid           # real bid/ask is worse than mid
        inside = C.settle(plan, 25000)
        assert inside["pnl_pts"] == pytest.approx(plan.credit_real)
        far = C.settle(plan, 30000)
        assert far["pnl_pts"] == pytest.approx(-plan.max_loss_pts)
        assert far["r"] == pytest.approx(-1.0)
    p100 = C.plan_condor(_chain(), 25000, 20.0, "2026-10-20", 1.0, 65, CondorParams(), wing_mode="points100")
    assert p100.width == pytest.approx(100.0)


def test_condor_rejects_missing_quotes():
    ch = _chain()
    for r in ch:
        r["ce_bid"] = 0.0
    with pytest.raises(C.NoTrade):
        C.plan_condor(ch, 25000, 20.0, "2026-10-20", 1.0, 65, CondorParams())


def test_journal_cash_and_positions(tmp_path):
    j = Journal(tmp_path / "j.sqlite")
    assert j.get_cash("MOM30", 100_000) == 100_000
    j.record_fill("MOM30", "PAPER", "A", "K", "BUY", 10, 100.0, 5.0, "", "t")
    assert j.positions("MOM30")["A"]["qty"] == 10
    assert j.get_cash("MOM30", 0) == pytest.approx(100_000 - 1005)
    j.record_fill("MOM30", "PAPER", "A", "K", "SELL", 10, 110.0, 5.0, "", "t")
    assert "A" not in j.positions("MOM30")
    assert j.get_cash("MOM30", 0) == pytest.approx(100_000 - 1005 + 1095)
    with pytest.raises(ValueError):
        j.record_fill("MOM30", "PAPER", "A", "K", "SELL", 1, 110.0, 0, "", "t")


def test_plan_orders_and_risk(tmp_path):
    cfg = BotConfig()
    sig = M.MomentumSignal(pd.Timestamp("2026-06-30"), 1, 1, True, True, pd.DataFrame(), ["A", "B"], "REBALANCE", [])
    held = {"C": {"key": "KC", "qty": 10, "avg": 100.0}}
    prices = {"A": 100.0, "B": 200.0, "C": 100.0}
    orders, _ = X.plan_orders(sig, held, 99_000, prices, {"A": "KA", "B": "KB", "C": "KC"}, cfg)
    sides = [(o.symbol, o.side) for o in orders]
    assert ("C", "SELL") in sides and ("A", "BUY") in sides and ("B", "BUY") in sides
    assert X.risk_check(orders, 99_000, 100_000, prices, replace(cfg, momentum=replace(cfg.momentum, top_n=2))) == []
    bad = [X.Order("A", "KA", "BUY", 10_000, 100.0)]
    assert X.risk_check(bad, 1_000, 100_000, prices, cfg)          # too big and not enough cash


def test_execute_requires_approval(tmp_path):
    j = Journal(tmp_path / "j.sqlite"); j.get_cash("MOM30", 100_000)
    orders = [X.Order("A", "KA", "BUY", 10, 100.0)]
    assert X.execute(orders, j, BotConfig(), lambda s: False) == 0 and j.positions("MOM30") == {}
    assert X.execute(orders, j, BotConfig(), lambda s: True, fill_prices={"A": 99.0}) == 1
    assert j.positions("MOM30")["A"]["avg"] == 99.0


class _Sess:
    def __init__(self):
        self.posted = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.posted = (url, json, headers)

        class R:
            status_code = 200; content = b"x"
            def json(self_inner): return {"status": "success", "data": {"order_id": "OID1"}}
        return R()


def test_orders_refused_in_paper_and_without_static_ip(monkeypatch):
    api = UpstoxAPI("tok", BotConfig(mode="PAPER"), session=_Sess())
    with pytest.raises(UpstoxAPIError):
        api.place_limit_order("K", 1, "BUY", 100.0)
    api2 = UpstoxAPI("tok", BotConfig(mode="LIVE", registered_static_ips=("1.2.3.4",)), session=_Sess())
    monkeypatch.setattr(UpstoxAPI, "public_ip", staticmethod(lambda: "9.9.9.9"))
    with pytest.raises(UpstoxAPIError):
        api2.place_limit_order("K", 1, "BUY", 100.0)


def test_live_order_is_limit_with_algo_header(monkeypatch):
    s = _Sess()
    api = UpstoxAPI("tok", BotConfig(mode="LIVE", registered_static_ips=("1.2.3.4",)), session=s)
    monkeypatch.setattr(UpstoxAPI, "public_ip", staticmethod(lambda: "1.2.3.4"))
    assert api.place_limit_order("NSE_EQ|X", 3, "BUY", 101.234) == "OID1"
    url, body, hdr = s.posted
    assert url.endswith("/v2/order/place") and body["order_type"] == "LIMIT" and body["price"] == 101.23
    assert hdr["X-Algo-Name"] == BotConfig().algo_name and body["tag"] == BotConfig().order_tag


def test_funds_excluded_from_universe():
    from bot_v1.data import is_fund
    for f in ["LIQUIDCASE", "SILVERBEES", "GOLDBEES", "NIFTYBEES", "CPSEETF", "HDFCSILVER", "LIQUIDBEES"]:
        assert is_fund(f), f
    for s in ["RELIANCE", "WELCORP", "BHEL", "DIVISLAB", "BANKBARODA", "MCX"]:
        assert not is_fund(s), s


def test_rebalance_with_overweight_kept_holding_still_fits_cash():
    """Regression (2006-26 backtest): a kept holding 15% overweight is not trimmed (inside the 20%
    tolerance), so full-size new buys used to exceed cash and the risk check blocked the rebalance."""
    cfg = replace(BotConfig(), momentum=replace(BotConfig().momentum, top_n=2))
    sig = M.MomentumSignal(pd.Timestamp("2026-06-30"), 1, 1, True, True, pd.DataFrame(), ["A", "B"], "REBALANCE", [])
    held = {"A": {"key": "KA", "qty": 575, "avg": 100.0}, "C": {"key": "KC", "qty": 100, "avg": 100.0}}
    prices = {"A": 100.0, "B": 100.0, "C": 100.0}
    cash = 100_000 - 57_500 - 10_000
    orders, notes = X.plan_orders(sig, held, cash, prices, {"A": "KA", "B": "KB", "C": "KC"}, cfg)
    assert not any(o.symbol == "A" for o in orders)                      # kept, not trimmed
    assert X.risk_check(orders, cash, 100_000, prices, cfg) == []
    assert any("scaled" in n for n in notes)


def test_auto_approval_only_in_paper(monkeypatch):
    from bot_v1 import run as R
    monkeypatch.setenv("BOTV1_AUTO", "1")
    monkeypatch.setenv("BOTV1_MODE", "PAPER")
    assert R._approve("x") is True
    monkeypatch.setenv("BOTV1_MODE", "LIVE")
    assert R._approve("x") is False


def test_first_session_of_month():
    from datetime import date
    from bot_v1 import run as R
    class FakeAPI:
        def __init__(self, closed): self.closed = closed
        def is_trading_day(self, d): return date.fromisoformat(d).weekday() < 5 and d not in self.closed
    assert R._first_session_of_month(FakeAPI(set()), date(2026, 11, 2))          # Sun 1 Nov -> Mon 2 Nov
    assert not R._first_session_of_month(FakeAPI(set()), date(2026, 11, 3))
    assert R._first_session_of_month(FakeAPI({"2026-10-01"}), date(2026, 10, 2))  # holiday on the 1st


def test_auto_without_token_logs_and_exits_2(tmp_path, monkeypatch):
    from bot_v1 import run as R
    monkeypatch.setenv("BOTV1_DATA", str(tmp_path))
    monkeypatch.setattr(R, "_token", lambda: "")
    with pytest.raises(SystemExit) as e:
        R.main(["auto"])
    assert e.value.code == 2
    assert "TOKEN_OR_NETWORK" in (tmp_path / "STATUS.txt").read_text()


def test_condor_scan_v2_logs_both_indices_and_variants(tmp_path, monkeypatch):
    from datetime import date, datetime as real_dt
    from bot_v1 import run as R
    from bot_v1 import report as RP
    class FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None): return real_dt(2026, 10, 14, 15, 10)
    class FixedDate(date):
        @classmethod
        def today(cls): return date(2026, 10, 14)
    monkeypatch.setattr(R, "datetime", FixedDT); monkeypatch.setattr(R, "date", FixedDate)
    def chain(spot, step):
        rows = []
        for k in range(int(spot * 0.9) // step * step, int(spot * 1.1), step):
            ce = max(spot - k, 0) + 30 * 2.7 ** (-abs(k - spot) / (spot * 0.01)); pe = max(k - spot, 0) + 30 * 2.7 ** (-abs(k - spot) / (spot * 0.01))
            rows.append(dict(strike=float(k), ce_bid=ce * 0.99, ce_ask=ce * 1.01, pe_bid=pe * 0.99, pe_ask=pe * 1.01))
        return rows
    class FakeAPI:
        def is_trading_day(self, d): return date.fromisoformat(d).weekday() < 5
        def ltp(self, keys): return {"NSE_INDEX|India VIX": 20.0, "NSE_INDEX|Nifty 50": 22600.0, "BSE_INDEX|SENSEX": 72700.0}
        def option_contracts(self, key):
            return [{"expiry": "2026-10-15", "lot_size": 65 if "Nifty" in key else 20}]
        def option_chain(self, key, exp): return chain(22600, 50) if "Nifty" in key else chain(72700, 100)
        def daily_candles(self, key, a, b): return [[a, 0, 0, 0, 22600.0 if "Nifty" in key else 72700.0, 0, 0]]
    j = Journal(tmp_path / "j.sqlite"); cfg = BotConfig()
    R.cmd_condor_scan(cfg, j, FakeAPI())
    tags = sorted(v for _, _, v, _ in j.open_condors())
    assert tags == sorted(f"{u}:{v}" for u in ("NIFTY", "SENSEX") for v in ("sigma", "points100", "d10", "d20", "nv15w15", "nv20w15"))
    R.cmd_condor_scan(cfg, j, FakeAPI())                       # idempotent
    assert len(j.open_condors()) == 12
    class Later(FixedDT):
        @classmethod
        def now(cls, tz=None): return real_dt(2026, 10, 15, 16, 30)
    class LaterDate(date):
        @classmethod
        def today(cls): return date(2026, 10, 15)
    monkeypatch.setattr(R, "datetime", Later); monkeypatch.setattr(R, "date", LaterDate)
    R.cmd_condor_settle(cfg, j, FakeAPI())
    assert j.open_condors() == []
    st = RP.condor_stats(j)
    assert "ALL:sigma" in set(st.variant)
    assert st.set_index("variant").loc["NIFTY:d10", "promotion"] == "info only (secondary)"


def test_condor_v3_no_vix_variants_trade_when_vix_low(tmp_path, monkeypatch):
    """v3: the no-filter wide-wing variants log at VIX 12 while the VIX>=18 rule logs nothing."""
    from datetime import date, datetime as real_dt
    import json
    from bot_v1 import run as R
    class FixedDT(real_dt):
        @classmethod
        def now(cls, tz=None): return real_dt(2026, 10, 14, 15, 10)
    class FixedDate(date):
        @classmethod
        def today(cls): return date(2026, 10, 14)
    monkeypatch.setattr(R, "datetime", FixedDT); monkeypatch.setattr(R, "date", FixedDate)
    def chain(spot, step):
        rows = []
        for k in range(int(spot * 0.9) // step * step, int(spot * 1.1), step):
            x = 30 * 2.7 ** (-abs(k - spot) / (spot * 0.01))
            ce, pe = max(spot - k, 0) + x, max(k - spot, 0) + x
            rows.append(dict(strike=float(k), ce_bid=ce * 0.99, ce_ask=ce * 1.01, pe_bid=pe * 0.99, pe_ask=pe * 1.01))
        return rows
    class FakeAPI:
        def is_trading_day(self, d): return date.fromisoformat(d).weekday() < 5
        def ltp(self, keys): return {"NSE_INDEX|India VIX": 12.0, "NSE_INDEX|Nifty 50": 22600.0, "BSE_INDEX|SENSEX": 72700.0}
        def option_contracts(self, key): return [{"expiry": "2026-10-15", "lot_size": 65 if "Nifty" in key else 20}]
        def option_chain(self, key, exp): return chain(22600, 50) if "Nifty" in key else chain(72700, 100)
    j = Journal(tmp_path / "j.sqlite")
    R.cmd_condor_scan(BotConfig(), j, FakeAPI())
    got = {v: json.loads(p) for _, _, v, p in j.open_condors()}
    assert sorted(got) == sorted(f"{u}:{v}" for u in ("NIFTY", "SENSEX") for v in ("nv15w15", "nv20w15"))
    p = got["NIFTY:nv15w15"]
    assert p["width"] >= 1.4 * p["sigma_pts"] - 50              # 1.5-sigma wings (to the nearest strike)


# ---------------------------------------------------------------- TradingAgents advisor (paper-only)
class _StubGraph:
    def __init__(self, ratings):
        self.ratings, self.calls = ratings, []

    def propagate(self, ticker, date):
        self.calls.append(ticker)
        r = self.ratings[ticker.replace(".NS", "")]
        if isinstance(r, Exception):
            raise r
        return {}, r


def test_advisor_logs_idempotently_and_survives_a_failing_name(tmp_path):
    from bot_v1 import advisor_tauric as AT
    g = _StubGraph({"AAA": "Buy", "BBB": "**Sell**", "CCC": RuntimeError("no data"), "DDD": "Maybe"})
    rows = AT.advise(["AAA", "BBB", "CCC", "DDD"], "2026-10-30", tmp_path, graph=g, log=lambda *_: None)
    assert [r["rating"] for r in rows] == ["Buy", "Sell", "ERROR", "REVIEW"]
    assert g.calls == ["AAA.NS", "BBB.NS", "CCC.NS", "DDD.NS"]
    assert AT.advise(["AAA", "BBB"], "2026-10-30", tmp_path, graph=g, log=lambda *_: None) == []   # no re-roll
    assert len(AT.load(tmp_path)) == 4


def test_advisor_score_uses_only_post_asof_returns_and_excludes_errors(tmp_path):
    from bot_v1 import advisor_tauric as AT
    idx = pd.bdate_range("2026-01-01", periods=300)
    cl = pd.DataFrame({s: 100.0 for s in "ABCD"}, index=idx)
    asofs = [idx[i] for i in (20, 60, 100, 140, 180, 220)]
    rows = []
    for a in asofs:
        i = idx.get_loc(a)
        cl.loc[idx[i + 1]:, "A"] *= 1.0                      # level fixed at fill day ...
        cl.loc[idx[i + 22]:, "A"] *= 1.10                    # ... A gains 10% over the next 21 sessions
        cl.loc[idx[i + 22]:, "B"] *= 0.90
        rows += [dict(symbol="A", asof=a, rating="Buy"), dict(symbol="B", asof=a, rating="Sell"),
                 dict(symbol="C", asof=a, rating="Hold"), dict(symbol="D", asof=a, rating="ERROR")]
    s = AT.score(pd.DataFrame(rows), cl)
    assert s["cohorts"] == 6 and s["obs"] == 18 and s["excluded"] == 6
    assert s["spread"] == pytest.approx(0.15)           # A +10% vs mean(B -10%, C 0%) = -5%
    # a rating whose outcome is not in yet is not scored
    late = pd.DataFrame([dict(symbol="A", asof=idx[-5], rating="Buy"), dict(symbol="B", asof=idx[-5], rating="Sell")])
    assert AT.score(late, cl)["cohorts"] == 0
    assert AT.to_yahoo("reliance") == "RELIANCE.NS" and AT.to_yahoo("X.BO") == "X.BO"


# ---------------------------------------------------------------- momentum health monitor
def test_ic_monitor_reads_a_working_signal_and_triggers_the_pause_rule():
    from bot_v1 import monitor as MON
    rng = np.random.default_rng(1)
    n, k = 1100, 60
    idx = pd.bdate_range("2020-01-01", periods=n)
    drift = rng.normal(0, 0.0012, k)                              # persistent per-stock trend -> momentum works
    cl = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(drift, 0.012, (n, k)), axis=0)), index=idx,
                      columns=[f"S{i:02d}" for i in range(k)])
    h = MON.ic_history(cl, P, top=10)
    s = MON.summarise(h)
    assert len(h) >= 18 and s[24]["mean_ic"] > 0 and s["review"].startswith("OK")
    # reverse the sign of the relationship: later months follow the losers -> negative ICIR -> pause
    bad = h.copy(); bad["ic"] = -bad["ic"].abs()
    assert MON.summarise(bad)["review"] == "PAUSE NEW MONEY AND REVIEW"
    assert MON.summarise(h.head(5))["review"].startswith("insufficient")
