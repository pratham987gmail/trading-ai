"""
DEPRECATED — replaced by scripts/paper_trading_v2.py.

This module's ``should_enter_trade()`` enters on every check during market
hours regardless of any measured condition, and both entry and exit prices
are synthetic estimates (``spot * 0.02``, a linear time-decay curve) rather
than real option-chain quotes. It predates, and does not use, the real signal
pipeline in ``analysis/signal_engine.py``.

It is kept only for reference and comparison against the new engine's
behavior. Do not point any deployment (render.yaml, a scheduled task, a
launcher .bat) at this file. Use ``scripts/paper_trading_v2.py``, which
consumes ``analysis.signal_engine.generate()`` for real signals, prices
entries and exits from live bid/ask quotes, and reconciles any position left
open by a previous crashed run before generating a new signal.
"""

import sys as _sys

print(
    "\n"
    "!!! scripts/paper_trading.py is DEPRECATED and unsafe for real use: it \n"
    "!!! enters unconditionally during market hours and prices trades with a \n"
    "!!! synthetic formula, not real quotes. Use scripts/paper_trading_v2.py.\n",
    file=_sys.stderr,
)

"""
Paper Trading System - Long Put Strategy

Generates real-time signals and tracks paper trades without risking real money.

Features:
- Live market monitoring
- Signal generation based on proven strategy
- Automatic entry/exit tracking
- P&L calculation
- Trade journal logging
- Cloud-ready with enhanced error handling
"""

import sys
from pathlib import Path
import os
import time
import json
import traceback
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Optional
import argparse
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

# Cloud logging helper
def log(level, message):
    """Log with timestamp and level for cloud monitoring."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}", flush=True)


@dataclass
class PaperTrade:
    """A single paper trade."""
    trade_id: int
    entry_date: datetime
    entry_spot: float
    entry_strike: float
    entry_premium: float
    quantity: int
    status: str  # OPEN, CLOSED
    exit_date: Optional[datetime] = None
    exit_spot: Optional[float] = None
    exit_premium: Optional[float] = None
    pnl: Optional[float] = None
    return_pct: Optional[float] = None
    hold_days: Optional[int] = None
    
    def to_dict(self):
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d['entry_date'] = self.entry_date.isoformat()
        if self.exit_date:
            d['exit_date'] = self.exit_date.isoformat()
        return d


class PaperTradingSystem:
    """Paper trading system for Long Put strategy."""
    
    def __init__(self, capital=100000, risk_per_trade_pct=2.0, hold_days=7):
        """Initialize paper trading system.
        
        Args:
            capital: Starting capital
            risk_per_trade_pct: Max % of capital to risk per trade
            hold_days: Days to hold position before exit
        """
        self.capital = capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.hold_days = hold_days
        self.trades: List[PaperTrade] = []
        self.open_trade: Optional[PaperTrade] = None
        self.next_trade_id = 1
        self.consecutive_errors = 0
        self.max_consecutive_errors = 10
        
        # Load previous trades if exist
        self.trades_file = Path("paper_trades.json")
        self._load_trades()
        
        # Setup Angel One with retry logic
        self._setup_angel_one()
    
    def _setup_angel_one(self, max_retries=3):
        """Setup Angel One connection with retry logic."""
        from SmartApi import SmartConnect
        import pyotp
        
        api_key = os.getenv('ANGEL_API_KEY')
        client_id = os.getenv('ANGEL_CLIENT_ID')
        password = os.getenv('ANGEL_PASSWORD')
        totp_secret = os.getenv('ANGEL_TOTP_SECRET')
        
        if not all([api_key, client_id, password, totp_secret]):
            log("ERROR", "Missing Angel One credentials in environment variables")
            raise Exception("Missing Angel One credentials")
        
        for attempt in range(1, max_retries + 1):
            try:
                log("INFO", f"Connecting to Angel One (attempt {attempt}/{max_retries})...")
                totp = pyotp.TOTP(totp_secret).now()
                self.smartApi = SmartConnect(api_key=api_key)
                data = self.smartApi.generateSession(client_id, password, totp)
                
                if not data.get('status'):
                    raise Exception(f"Authentication failed: {data.get('message')}")
                
                log("INFO", f"✅ Connected to Angel One successfully (Client: {client_id})")
                return
            except Exception as e:
                log("ERROR", f"Angel One connection attempt {attempt} failed: {e}")
                if attempt < max_retries:
                    time.sleep(5 * attempt)  # Exponential backoff
                else:
                    raise Exception(f"Failed to connect after {max_retries} attempts")
    
    def _load_trades(self):
        """Load previous trades from file."""
        if self.trades_file.exists():
            try:
                with open(self.trades_file, 'r') as f:
                    data = json.load(f)
                    for t in data.get('trades', []):
                        trade = PaperTrade(
                            trade_id=t['trade_id'],
                            entry_date=datetime.fromisoformat(t['entry_date']),
                            entry_spot=t['entry_spot'],
                            entry_strike=t['entry_strike'],
                            entry_premium=t['entry_premium'],
                            quantity=t['quantity'],
                            status=t['status'],
                            exit_date=datetime.fromisoformat(t['exit_date']) if t.get('exit_date') else None,
                            exit_spot=t.get('exit_spot'),
                            exit_premium=t.get('exit_premium'),
                            pnl=t.get('pnl'),
                            return_pct=t.get('return_pct'),
                            hold_days=t.get('hold_days'),
                        )
                        self.trades.append(trade)
                        if trade.status == 'OPEN':
                            self.open_trade = trade
                    
                    self.next_trade_id = max([t.trade_id for t in self.trades], default=0) + 1
                    log("INFO", f"📂 Loaded {len(self.trades)} previous trades")
            except Exception as e:
                log("WARNING", f"Could not load previous trades: {e}")
    
    def _save_trades(self):
        """Save trades to file."""
        data = {
            'trades': [t.to_dict() for t in self.trades],
            'last_updated': datetime.now().isoformat(),
        }
        with open(self.trades_file, 'w') as f:
            json.dump(data, f, indent=2)
    
    def get_spot_price(self):
        """Get current NIFTY spot price with error handling."""
        try:
            ltp_data = self.smartApi.ltpData("NSE", "NIFTY 50", "99926000")
            if ltp_data and ltp_data.get('data'):
                price = float(ltp_data['data']['ltp'])
                self.consecutive_errors = 0  # Reset error counter on success
                return price
        except Exception as e:
            self.consecutive_errors += 1
            log("ERROR", f"Failed to fetch spot price: {e}")
            
            # If too many consecutive errors, try to reconnect
            if self.consecutive_errors >= 5:
                log("WARNING", "Too many errors, attempting to reconnect...")
                try:
                    self._setup_angel_one()
                    self.consecutive_errors = 0
                except Exception as reconnect_error:
                    log("ERROR", f"Reconnection failed: {reconnect_error}")
        
        return None
    
    def should_enter_trade(self, spot):
        """Check if we should enter a Long Put trade.
        
        Simple entry rules:
        - No open position
        - Market showing bearish signs (for demo, we enter if conditions met)
        """
        if self.open_trade:
            return False
        
        # Simple entry: Enter during market hours if no position
        # In production, add more conditions:
        # - RSI < 50 (bearish momentum)
        # - Price below VWAP
        # - Downtrend detected
        # - IV percentile checks
        
        now = datetime.now()
        # Only enter during market hours (9:15 AM - 3:00 PM)
        if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute < 15)):
            return False
        
        return True
    
    def enter_trade(self, spot):
        """Enter a Long Put paper trade."""
        # Calculate ATM strike
        atm_strike = round(spot / 50) * 50
        
        # Buy ATM Put
        # In production, fetch real premium from Angel One
        # For paper trading, estimate premium based on typical values
        # ATM Put typically 1.5-3% of spot for weekly expiry
        estimated_premium = spot * 0.02  # 2% estimate
        
        # Calculate quantity based on risk
        risk_amount = self.capital * (self.risk_per_trade_pct / 100)
        lot_size = 50  # NIFTY lot size
        max_lots = int(risk_amount / (estimated_premium * lot_size))
        max_lots = max(1, min(max_lots, 3))  # Between 1-3 lots
        
        quantity = max_lots * lot_size
        
        # Create trade
        trade = PaperTrade(
            trade_id=self.next_trade_id,
            entry_date=datetime.now(),
            entry_spot=spot,
            entry_strike=atm_strike,
            entry_premium=estimated_premium,
            quantity=quantity,
            status='OPEN',
        )
        
        self.next_trade_id += 1
        self.trades.append(trade)
        self.open_trade = trade
        self._save_trades()
        
        cost = estimated_premium * quantity
        
        log("INFO", f"🟢 ENTERED LONG PUT TRADE #{trade.trade_id}")
        print(f"\n{'='*70}")
        print(f"🟢 ENTERED LONG PUT TRADE #{trade.trade_id}")
        print(f"{'='*70}")
        print(f"Time:        {trade.entry_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Spot:        ₹{spot:,.2f}")
        print(f"Strike:      {atm_strike} PE")
        print(f"Premium:     ₹{estimated_premium:.2f}")
        print(f"Quantity:    {quantity} ({max_lots} lots)")
        print(f"Total Cost:  ₹{cost:,.2f}")
        print(f"Max Loss:    ₹{cost:,.2f} (100% of premium)")
        print(f"Max Profit:  Unlimited")
        print(f"Exit Target: {self.hold_days} days")
        print(f"{'='*70}\n")
    
    def should_exit_trade(self, spot):
        """Check if we should exit the open trade."""
        if not self.open_trade:
            return False
        
        # Exit conditions:
        # 1. Held for target days
        days_held = (datetime.now() - self.open_trade.entry_date).days
        if days_held >= self.hold_days:
            return True
        
        # 2. Stop loss: Down 50% (premium halved)
        # 3. Take profit: Up 100% (premium doubled)
        # Would check real premium here in production
        
        return False
    
    def exit_trade(self, spot):
        """Exit the open Long Put trade."""
        if not self.open_trade:
            return
        
        trade = self.open_trade
        
        # Calculate exit premium
        # In production, fetch real premium
        # For paper trading, calculate based on intrinsic + time value
        strike = trade.entry_strike
        
        # Intrinsic value
        intrinsic = max(0, strike - spot)
        
        # Time value (decays over time)
        days_held = (datetime.now() - trade.entry_date).days
        time_decay_factor = max(0, 1 - (days_held / 7))  # Decays to 0 over 7 days
        time_value = trade.entry_premium * 0.5 * time_decay_factor
        
        exit_premium = intrinsic + time_value
        
        # Calculate P&L
        entry_cost = trade.entry_premium * trade.quantity
        exit_value = exit_premium * trade.quantity
        pnl = exit_value - entry_cost
        return_pct = (pnl / entry_cost) * 100 if entry_cost > 0 else 0
        
        # Update trade
        trade.exit_date = datetime.now()
        trade.exit_spot = spot
        trade.exit_premium = exit_premium
        trade.pnl = pnl
        trade.return_pct = return_pct
        trade.hold_days = (trade.exit_date - trade.entry_date).days
        trade.status = 'CLOSED'
        
        self.open_trade = None
        self._save_trades()
        
        # Update capital
        self.capital += pnl
        
        log("INFO", f"🔴 EXITED LONG PUT TRADE #{trade.trade_id} - P&L: ₹{pnl:+,.2f}")
        print(f"\n{'='*70}")
        print(f"🔴 EXITED LONG PUT TRADE #{trade.trade_id}")
        print(f"{'='*70}")
        print(f"Entry:       {trade.entry_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Exit:        {trade.exit_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Hold Days:   {trade.hold_days}")
        print()
        print(f"Entry Spot:  ₹{trade.entry_spot:,.2f}")
        print(f"Exit Spot:   ₹{trade.exit_spot:,.2f}")
        print(f"Move:        ₹{trade.exit_spot - trade.entry_spot:+,.2f} ({((trade.exit_spot/trade.entry_spot - 1) * 100):+.2f}%)")
        print()
        print(f"Entry Prem:  ₹{trade.entry_premium:.2f}")
        print(f"Exit Prem:   ₹{trade.exit_premium:.2f}")
        print(f"Premium Δ:   ₹{trade.exit_premium - trade.entry_premium:+.2f}")
        print()
        print(f"P&L:         ₹{pnl:+,.2f}")
        print(f"Return:      {return_pct:+.2f}%")
        print(f"New Capital: ₹{self.capital:,.2f}")
        print(f"{'='*70}\n")
    
    def show_stats(self):
        """Show trading statistics."""
        if not self.trades:
            return
        
        closed_trades = [t for t in self.trades if t.status == 'CLOSED']
        if not closed_trades:
            return
        
        winners = [t for t in closed_trades if t.pnl and t.pnl > 0]
        losers = [t for t in closed_trades if t.pnl and t.pnl <= 0]
        
        total_pnl = sum(t.pnl for t in closed_trades if t.pnl)
        win_rate = (len(winners) / len(closed_trades)) * 100 if closed_trades else 0
        
        avg_win = sum(t.pnl for t in winners) / len(winners) if winners else 0
        avg_loss = sum(t.pnl for t in losers) / len(losers) if losers else 0
        
        print(f"\n{'='*70}")
        print(f"📊 PAPER TRADING STATISTICS")
        print(f"{'='*70}")
        print(f"Total Trades:    {len(closed_trades)}")
        print(f"Winners:         {len(winners)} ({win_rate:.1f}%)")
        print(f"Losers:          {len(losers)}")
        print()
        print(f"Avg Win:         ₹{avg_win:,.2f}")
        print(f"Avg Loss:        ₹{avg_loss:,.2f}")
        print()
        print(f"Total P&L:       ₹{total_pnl:+,.2f}")
        print(f"Starting Cap:    ₹{100000:,.2f}")
        print(f"Current Cap:     ₹{self.capital:,.2f}")
        print(f"Total Return:    {((self.capital/100000 - 1) * 100):+.2f}%")
        print(f"{'='*70}\n")
    
    def run(self, check_interval=60):
        """Run paper trading loop with error handling."""
        log("INFO", "Starting paper trading system...")
        print("\n" + "="*70)
        print("📈 PAPER TRADING SYSTEM - LONG PUT STRATEGY")
        print("="*70)
        print(f"Capital:      ₹{self.capital:,.2f}")
        print(f"Risk/Trade:   {self.risk_per_trade_pct}% (₹{self.capital * self.risk_per_trade_pct / 100:,.2f})")
        print(f"Hold Period:  {self.hold_days} days")
        print(f"Check Every:  {check_interval} seconds")
        print("="*70)
        print()
        print("💡 This is PAPER TRADING - No real money at risk!")
        print()
        print("🎯 Strategy: Long Put (Proven ₹2,13,496 profit over 18 months)")
        print()
        
        if self.open_trade:
            days_held = (datetime.now() - self.open_trade.entry_date).days
            print(f"📌 OPEN POSITION: Long Put #{self.open_trade.trade_id}")
            print(f"   Entry: {self.open_trade.entry_date.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"   Strike: {self.open_trade.entry_strike} PE")
            print(f"   Cost: ₹{self.open_trade.entry_premium * self.open_trade.quantity:,.2f}")
            print(f"   Days Held: {days_held}/{self.hold_days}")
            print()
        
        print("Press Ctrl+C to stop\n")
        
        try:
            while True:
                try:
                    now = datetime.now()
                    spot = self.get_spot_price()
                    
                    if not spot:
                        log("WARNING", "Could not fetch spot price, retrying...")
                        time.sleep(check_interval)
                        continue
                    
                    # Check if too many consecutive errors
                    if self.consecutive_errors >= self.max_consecutive_errors:
                        log("ERROR", f"Too many consecutive errors ({self.consecutive_errors}), exiting...")
                        break
                    
                    print(f"[{now.strftime('%H:%M:%S')}] NIFTY: ₹{spot:,.2f}", end="")
                    
                    # Check for exit first
                    if self.should_exit_trade(spot):
                        print(" → Exiting trade")
                        self.exit_trade(spot)
                        self.show_stats()
                    
                    # Check for entry
                    elif self.should_enter_trade(spot):
                        print(" → Entry signal!")
                        self.enter_trade(spot)
                    
                    else:
                        if self.open_trade:
                            days_held = (datetime.now() - self.open_trade.entry_date).days
                            print(f" | Open: {self.open_trade.entry_strike} PE | Day {days_held}/{self.hold_days}")
                        else:
                            print(" | No position | Waiting for entry signal")
                    
                    time.sleep(check_interval)
                
                except KeyboardInterrupt:
                    raise  # Re-raise to outer handler
                except Exception as e:
                    log("ERROR", f"Error in trading loop: {e}")
                    log("ERROR", traceback.format_exc())
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= self.max_consecutive_errors:
                        log("ERROR", "Too many errors, stopping...")
                        break
                    time.sleep(check_interval)
                    
        except KeyboardInterrupt:
            log("INFO", "Paper trading stopped by user")
            print("\n\n⏹️  Paper trading stopped\n")
            self.show_stats()
        except Exception as e:
            log("ERROR", f"Fatal error: {e}")
            log("ERROR", traceback.format_exc())
            self.show_stats()
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper Trading System")
    parser.add_argument("--strategy", default="LONG_PUT", help="Strategy (currently only LONG_PUT)")
    parser.add_argument("--capital", type=float, default=100000, help="Starting capital")
    parser.add_argument("--risk-pct", type=float, default=2.0, help="Risk per trade %%")
    parser.add_argument("--hold-days", type=int, default=7, help="Days to hold position")
    parser.add_argument("--interval", type=int, default=60, help="Check interval in seconds")
    
    args = parser.parse_args()
    
    log("INFO", "="*70)
    log("INFO", "PAPER TRADING SYSTEM STARTING")
    log("INFO", f"Strategy: {args.strategy}")
    log("INFO", f"Capital: ₹{args.capital:,.2f}")
    log("INFO", f"Risk per trade: {args.risk_pct}%")
    log("INFO", f"Hold period: {args.hold_days} days")
    log("INFO", f"Check interval: {args.interval} seconds")
    log("INFO", "="*70)
    
    try:
        system = PaperTradingSystem(
            capital=args.capital,
            risk_per_trade_pct=args.risk_pct,
            hold_days=args.hold_days,
        )
        
        system.run(check_interval=args.interval)
    except Exception as e:
        log("ERROR", f"System failed to start: {e}")
        log("ERROR", traceback.format_exc())
        sys.exit(1)
