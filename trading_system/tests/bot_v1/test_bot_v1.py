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
