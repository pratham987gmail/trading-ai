"""
Option Chain Storage Module

Stores historical option chain snapshots in SQLite for backtesting.
"""

import sqlite3
from pathlib import Path
from datetime import date, datetime
from typing import List, Optional
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from analysis.signal_models import OptionChain, OptionChainStrike


class ChainStorage:
    """SQLite storage for option chains"""
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize storage with database path"""
        if db_path is None:
            # Default: data/option_chains.db
            project_root = Path(__file__).parent.parent
            data_dir = project_root / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "option_chains.db")
        
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Create tables if they don't exist"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Chains table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recorded_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                expiry DATE NOT NULL,
                spot_price REAL NOT NULL,
                timestamp DATETIME NOT NULL,
                UNIQUE(symbol, expiry, timestamp)
            )
        """)
        
        # Strikes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recorded_strikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain_id INTEGER NOT NULL,
                strike REAL NOT NULL,
                call_ltp REAL,
                call_oi INTEGER,
                call_volume INTEGER,
                call_bid REAL,
                call_ask REAL,
                call_iv REAL,
                call_delta REAL,
                call_gamma REAL,
                call_theta REAL,
                call_vega REAL,
                put_ltp REAL,
                put_oi INTEGER,
                put_volume INTEGER,
                put_bid REAL,
                put_ask REAL,
                put_iv REAL,
                put_delta REAL,
                put_gamma REAL,
                put_theta REAL,
                put_vega REAL,
                FOREIGN KEY (chain_id) REFERENCES recorded_chains(id)
            )
        """)
        
        # Indexes for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_chain_lookup 
            ON recorded_chains(symbol, expiry, timestamp)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_strike_chain 
            ON recorded_strikes(chain_id)
        """)
        
        conn.commit()
        conn.close()
    
    def save_chain(self, chain: OptionChain) -> int:
        """
        Save an option chain to database.
        Returns chain_id.
        Raises exception if duplicate (same symbol, expiry, timestamp).
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # Insert chain
            cursor.execute("""
                INSERT INTO recorded_chains (symbol, expiry, spot_price, timestamp)
                VALUES (?, ?, ?, ?)
            """, (chain.symbol, chain.expiry, chain.spot_price, chain.timestamp))
            
            chain_id = cursor.lastrowid
            
            # Insert strikes
            for strike in chain.strikes:
                cursor.execute("""
                    INSERT INTO recorded_strikes (
                        chain_id, strike,
                        call_ltp, call_oi, call_volume, call_bid, call_ask,
                        call_iv, call_delta, call_gamma, call_theta, call_vega,
                        put_ltp, put_oi, put_volume, put_bid, put_ask,
                        put_iv, put_delta, put_gamma, put_theta, put_vega
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    chain_id, strike.strike,
                    strike.call_ltp, strike.call_oi, strike.call_volume, 
                    strike.call_bid, strike.call_ask,
                    strike.call_iv, strike.call_delta, strike.call_gamma, 
                    strike.call_theta, strike.call_vega,
                    strike.put_ltp, strike.put_oi, strike.put_volume,
                    strike.put_bid, strike.put_ask,
                    strike.put_iv, strike.put_delta, strike.put_gamma,
                    strike.put_theta, strike.put_vega
                ))
            
            conn.commit()
            return chain_id
            
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise Exception(f"Duplicate chain: {chain.symbol} {chain.expiry} {chain.timestamp}") from e
        finally:
            conn.close()
    
    def get_chains_on_date(self, symbol: str, target_date: date) -> List[OptionChain]:
        """Get all chains for a symbol on a specific date"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get chains for this date
        cursor.execute("""
            SELECT id, symbol, expiry, spot_price, timestamp
            FROM recorded_chains
            WHERE symbol = ? AND DATE(timestamp) = ?
            ORDER BY timestamp DESC
        """, (symbol, target_date))
        
        chain_rows = cursor.fetchall()
        
        chains = []
        for chain_row in chain_rows:
            chain_id, symbol, expiry, spot_price, timestamp = chain_row
            
            # Get strikes for this chain
            cursor.execute("""
                SELECT strike,
                       call_ltp, call_oi, call_volume, call_bid, call_ask,
                       call_iv, call_delta, call_gamma, call_theta, call_vega,
                       put_ltp, put_oi, put_volume, put_bid, put_ask,
                       put_iv, put_delta, put_gamma, put_theta, put_vega
                FROM recorded_strikes
                WHERE chain_id = ?
                ORDER BY strike
            """, (chain_id,))
            
            strike_rows = cursor.fetchall()
            
            strikes = []
            for strike_row in strike_rows:
                strike_obj = OptionChainStrike(
                    strike=strike_row[0],
                    call_ltp=strike_row[1],
                    call_oi=strike_row[2],
                    call_volume=strike_row[3],
                    call_bid=strike_row[4],
                    call_ask=strike_row[5],
                    call_iv=strike_row[6],
                    call_delta=strike_row[7],
                    call_gamma=strike_row[8],
                    call_theta=strike_row[9],
                    call_vega=strike_row[10],
                    put_ltp=strike_row[11],
                    put_oi=strike_row[12],
                    put_volume=strike_row[13],
                    put_bid=strike_row[14],
                    put_ask=strike_row[15],
                    put_iv=strike_row[16],
                    put_delta=strike_row[17],
                    put_gamma=strike_row[18],
                    put_theta=strike_row[19],
                    put_vega=strike_row[20]
                )
                strikes.append(strike_obj)
            
            # Convert expiry string to date
            if isinstance(expiry, str):
                expiry_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            else:
                expiry_date = expiry
            
            # Convert timestamp string to datetime
            if isinstance(timestamp, str):
                timestamp_dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
            else:
                timestamp_dt = timestamp
            
            chain = OptionChain(
                symbol=symbol,
                expiry=expiry_date,
                spot_price=spot_price,
                strikes=strikes,
                timestamp=timestamp_dt
            )
            chains.append(chain)
        
        conn.close()
        return chains
    
    def get_chain_count(self) -> int:
        """Get total number of chains in database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM recorded_chains")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    
    def get_date_range(self, symbol: str) -> tuple:
        """Get first and last date for a symbol"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MIN(DATE(timestamp)), MAX(DATE(timestamp))
            FROM recorded_chains
            WHERE symbol = ?
        """, (symbol,))
        result = cursor.fetchone()
        conn.close()
        return result if result else (None, None)
