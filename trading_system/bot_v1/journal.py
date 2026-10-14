"""SQLite journal: every signal, proposal, approval, fill, position, shadow condor, equity mark."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, strategy TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, mode TEXT, symbol TEXT,
    instrument_key TEXT, side TEXT, qty INTEGER, price REAL, charges REAL, order_id TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS positions (strategy TEXT, symbol TEXT, instrument_key TEXT, qty INTEGER,
    avg_price REAL, PRIMARY KEY (strategy, symbol));
CREATE TABLE IF NOT EXISTS cash (strategy TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS marks (date TEXT, strategy TEXT, equity REAL, benchmark REAL, PRIMARY KEY (date, strategy));
CREATE TABLE IF NOT EXISTS condor_shadow (id INTEGER PRIMARY KEY, created TEXT, expiry TEXT, variant TEXT,
    status TEXT, plan TEXT, result TEXT);
"""


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.environ.get("BOTV1_SQLITE_NOLOCK") == "1":   # shared-folder mounts without POSIX locks (single writer only)
            self.db = sqlite3.connect(f"file:{path.as_posix()}?nolock=1", uri=True)
            self.db.execute("PRAGMA journal_mode=MEMORY")
        else:
            self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    @staticmethod
    def now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def event(self, kind: str, strategy: str, payload: dict) -> None:
        self.db.execute("INSERT INTO events (ts, kind, strategy, payload) VALUES (?,?,?,?)",
                        (self.now(), kind, strategy, json.dumps(payload, default=str)))
        self.db.commit()

    # ---- cash & positions ----
    def get_cash(self, strategy: str, default: float) -> float:
        r = self.db.execute("SELECT amount FROM cash WHERE strategy=?", (strategy,)).fetchone()
        if r is None:
            self.set_cash(strategy, default)
            return default
        return r[0]

    def set_cash(self, strategy: str, amount: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO cash VALUES (?,?)", (strategy, amount))
        self.db.commit()

    def positions(self, strategy: str) -> dict[str, dict]:
        rows = self.db.execute("SELECT symbol, instrument_key, qty, avg_price FROM positions WHERE strategy=?", (strategy,))
        return {s: {"key": k, "qty": q, "avg": a} for s, k, q, a in rows}

    def record_fill(self, strategy: str, mode: str, symbol: str, key: str, side: str, qty: int, price: float,
                    charges: float, order_id: str, source: str) -> None:
        self.db.execute("INSERT INTO fills (ts, strategy, mode, symbol, instrument_key, side, qty, price, charges, order_id, source)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (self.now(), strategy, mode, symbol, key, side, qty, price, charges, order_id, source))
        pos = self.positions(strategy).get(symbol)
        cash = self.get_cash(strategy, 0.0)
        if side == "BUY":
            nq = (pos["qty"] if pos else 0) + qty
            avg = ((pos["qty"] * pos["avg"]) if pos else 0) + qty * price
            self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?)", (strategy, symbol, key, nq, avg / nq))
            cash -= qty * price + charges
        else:
            nq = (pos["qty"] if pos else 0) - qty
            if nq < 0:
                raise ValueError(f"sell of {qty} {symbol} exceeds held quantity")
            if nq == 0:
                self.db.execute("DELETE FROM positions WHERE strategy=? AND symbol=?", (strategy, symbol))
            else:
                self.db.execute("UPDATE positions SET qty=? WHERE strategy=? AND symbol=?", (nq, strategy, symbol))
            cash += qty * price - charges
        self.set_cash(strategy, cash)

    def mark(self, day: str, strategy: str, equity: float, benchmark: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?,?)", (day, strategy, equity, benchmark))
        self.db.commit()

    # ---- shadow condor ----
    def add_condor(self, expiry: str, variant: str, plan: dict) -> None:
        self.db.execute("INSERT INTO condor_shadow (created, expiry, variant, status, plan) VALUES (?,?,?,?,?)",
                        (self.now(), expiry, variant, "OPEN", json.dumps(plan)))
        self.db.commit()

    def open_condors(self) -> list[tuple]:
        return self.db.execute("SELECT id, expiry, variant, plan FROM condor_shadow WHERE status='OPEN'").fetchall()

    def close_condor(self, cid: int, result: dict) -> None:
        self.db.execute("UPDATE condor_shadow SET status='SETTLED', result=? WHERE id=?", (json.dumps(result), cid))
        self.db.commit()

    def settled_condors(self) -> list[tuple]:
        return self.db.execute("SELECT expiry, variant, plan, result FROM condor_shadow WHERE status='SETTLED'").fetchall()
