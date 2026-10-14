"""Thin Upstox REST wrapper for bot_v1 (quotes, option chain, charges, orders).

Endpoints (Upstox API v2): GET /v2/market-quote/ltp, GET /v2/market-quote/quotes,
GET /v2/option/chain, GET /v2/option/contract, GET /v2/charges/brokerage,
GET /v3/historical-candle/..., POST /v2/order/place (order host), GET /v2/order/details.
The access token comes from the project's existing login flow (research/cli/upstox_login.py
-> UPSTOX_ACCESS_TOKEN). Tokens expire daily; re-login each morning.
Order placement is refused unless mode == LIVE, the machine's public IP is one of the
registered static IPs, and every order is LIMIT with the X-Algo-Name header.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .config import BotConfig


class UpstoxAPIError(RuntimeError):
    pass


class UpstoxAPI:
    def __init__(self, token: str, cfg: BotConfig, session: requests.Session | None = None):
        if not token:
            raise UpstoxAPIError("no Upstox access token — run the daily login first")
        self.cfg = cfg
        self.s = session or requests.Session()
        self._hdr = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self._last_order_ts = 0.0

    # ---------------- reads ----------------
    def _get(self, path: str, params: dict | None = None, host: str | None = None) -> Any:
        r = self.s.get((host or self.cfg.api_host) + path, params=params, headers=self._hdr, timeout=15)
        try:
            body = r.json()
        except ValueError:
            raise UpstoxAPIError(f"GET {path}: HTTP {r.status_code}, non-JSON response")
        if r.status_code != 200 or body.get("status") != "success":
            raise UpstoxAPIError(f"GET {path}: HTTP {r.status_code} {body.get('errors') or body}")
        return body["data"]

    def ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        out = {}
        for i in range(0, len(instrument_keys), 400):
            data = self._get("/v2/market-quote/ltp", {"instrument_key": ",".join(instrument_keys[i:i + 400])})
            for v in data.values():
                out[v["instrument_token"]] = float(v["last_price"])
        return out

    def is_trading_day(self, day: str) -> bool:
        """NSE trading session on `day` (YYYY-MM-DD)? Weekends are closed; holidays come from
        GET /v2/market/holidays/{date} (an NSE entry in closed_exchanges = holiday)."""
        from datetime import date as _d
        if _d.fromisoformat(day).weekday() >= 5:
            return False
        data = self._get(f"/v2/market/holidays/{day}") or []
        for h in data if isinstance(data, list) else [data]:
            if str(h.get("date", day))[:10] == day and "NSE" in (h.get("closed_exchanges") or []):
                return False
        return True

    def daily_candles(self, instrument_key: str, from_date: str, to_date: str) -> list[list]:
        data = self._get(f"/v3/historical-candle/{instrument_key}/days/1/{to_date}/{from_date}")
        return data.get("candles", [])

    def option_chain(self, underlying_key: str, expiry: str) -> list[dict]:
        rows = self._get("/v2/option/chain", {"instrument_key": underlying_key, "expiry_date": expiry})
        out = []
        for r in rows:
            c, p = r.get("call_options") or {}, r.get("put_options") or {}
            cm, pm = c.get("market_data") or {}, p.get("market_data") or {}
            out.append({
                "strike": float(r["strike_price"]), "spot": float(r.get("underlying_spot_price") or 0),
                "ce_bid": float(cm.get("bid_price") or 0), "ce_ask": float(cm.get("ask_price") or 0),
                "pe_bid": float(pm.get("bid_price") or 0), "pe_ask": float(pm.get("ask_price") or 0),
                "ce_ltp": float(cm.get("ltp") or 0), "pe_ltp": float(pm.get("ltp") or 0),
                "ce_key": c.get("instrument_key", ""), "pe_key": p.get("instrument_key", ""),
            })
        return out

    def option_contracts(self, underlying_key: str) -> list[dict]:
        return self._get("/v2/option/contract", {"instrument_key": underlying_key})

    def charges(self, instrument_key: str, qty: int, side: str, price: float, product: str = "D") -> float:
        data = self._get("/v2/charges/brokerage", {"instrument_token": instrument_key, "quantity": qty,
                                                   "product": product, "transaction_type": side, "price": round(price, 2)})
        return float(data["charges"]["total"])

    def order_details(self, order_id: str) -> dict:
        return self._get("/v2/order/details", {"order_id": order_id})

    # ---------------- orders (LIVE only) ----------------
    @staticmethod
    def public_ip() -> str:
        return requests.get("https://api.ipify.org", timeout=10).text.strip()

    def assert_live_allowed(self) -> None:
        if self.cfg.mode != "LIVE":
            raise UpstoxAPIError("order placement refused: mode is not LIVE")
        if not self.cfg.registered_static_ips:
            raise UpstoxAPIError("order placement refused: no registered static IPs configured (BOTV1_STATIC_IPS)")
        ip = self.public_ip()
        if ip not in self.cfg.registered_static_ips:
            raise UpstoxAPIError(f"order placement refused: this machine's IP {ip} is not a registered static IP")

    def place_limit_order(self, instrument_key: str, qty: int, side: str, price: float, product: str = "D") -> str:
        self.assert_live_allowed()
        wait = 1.0 / self.cfg.max_orders_per_second - (time.time() - self._last_order_ts)
        if wait > 0:
            time.sleep(wait)
        body = {"quantity": int(qty), "product": product, "validity": "DAY", "price": round(price, 2),
                "tag": self.cfg.order_tag, "instrument_token": instrument_key, "order_type": "LIMIT",
                "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}
        hdr = dict(self._hdr, **{"Content-Type": "application/json", "X-Algo-Name": self.cfg.algo_name})
        r = self.s.post(self.cfg.order_host + "/v2/order/place", json=body, headers=hdr, timeout=15)
        self._last_order_ts = time.time()
        b = r.json() if r.content else {}
        if r.status_code != 200 or b.get("status") != "success":
            raise UpstoxAPIError(f"order rejected: HTTP {r.status_code} {b.get('errors') or b}")
        return b["data"]["order_id"]
