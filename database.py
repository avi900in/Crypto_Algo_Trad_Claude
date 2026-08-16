import os
import sqlite3
from datetime import datetime

DB_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DB_DIR, "data", "trading_bot.db")

def get_db_connection():
    # Ensure directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Config Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # 2. Paper Balance Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS paper_balance (
        currency TEXT PRIMARY KEY,
        amount REAL NOT NULL
    )
    """)
    
    # 3. Trades Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        price REAL NOT NULL,
        amount REAL NOT NULL,
        cost REAL NOT NULL,
        fee REAL NOT NULL,
        pnl REAL,
        status TEXT NOT NULL,
        type TEXT NOT NULL, -- 'PAPER' or 'LIVE'
        notes TEXT
    )
    """)
    
    # 4. Performance Log Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS performance_log (
        timestamp TEXT PRIMARY KEY,
        total_equity REAL NOT NULL,
        pnl REAL NOT NULL,
        type TEXT NOT NULL -- 'PAPER' or 'LIVE'
    )
    """)
    
    # Seed default configs if they don't exist
    default_configs = {
        "dry_run": "1",
        "symbols": "BTC/USDT,ETH/USDT,SOL/USDT,ADA/USDT,XRP/USDT",
        "trade_size_usd": "20.0",  # $20 per trade
        "stop_loss_pct": "1.5",    # 1.5% stop loss
        "take_profit_pct": "3.0",  # 3% take profit
        "bot_status": "stopped",   # running or stopped
        "interval": "5m"           # candle timeframe: 5m, 15m, 1h
    }
    
    for k, v in default_configs.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
        
    # Seed paper trading balance if empty
    cursor.execute("SELECT COUNT(*) FROM paper_balance")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO paper_balance (currency, amount) VALUES ('USDT', 100.0)")
        
    conn.commit()
    conn.close()

# Config Helpers
def get_config(key, default=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row['value']
    return default

def set_config(key, value):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_all_configs():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM config")
    rows = cursor.fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}

# Paper Balance Helpers
def get_paper_balance(currency):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT amount FROM paper_balance WHERE currency = ?", (currency,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return float(row['amount'])
    return 0.0

def update_paper_balance(currency, amount):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO paper_balance (currency, amount) VALUES (?, ?)", (currency, float(amount)))
    conn.commit()
    conn.close()

def get_all_paper_balances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT currency, amount FROM paper_balance")
    rows = cursor.fetchall()
    conn.close()
    return {row['currency']: float(row['amount']) for row in rows}

# Trades Helpers
def add_trade(timestamp, symbol, side, price, amount, cost, fee, pnl, status, type, notes=""):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO trades (timestamp, symbol, side, price, amount, cost, fee, pnl, status, type, notes)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (timestamp, symbol, side, float(price), float(amount), float(cost), float(fee), 
          float(pnl) if pnl is not None else None, status, type, notes))
    conn.commit()
    conn.close()

def get_trades(limit=100, trade_type=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if trade_type:
        cursor.execute("SELECT * FROM trades WHERE type = ? ORDER BY timestamp DESC LIMIT ?", (trade_type, limit))
    else:
        cursor.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_open_paper_positions():
    # A simple way to compute current holdings based on trade logs
    # For paper trading, find tokens that have net positive amounts
    balances = get_all_paper_balances()
    positions = {}
    for currency, amount in balances.items():
        if currency != "USDT" and amount > 0.000001:
            # Find the average buy price for this currency from trade history
            conn = get_db_connection()
            cursor = conn.cursor()
            # Find the most recent buy trade that matches this currency prefix (e.g. BTC/USDT or BTC/USD)
            cursor.execute("""
            SELECT price, symbol FROM trades 
            WHERE symbol LIKE ? AND side = 'BUY' AND status = 'COMPLETED' AND type = 'PAPER' 
            ORDER BY timestamp DESC LIMIT 1
            """, (f"{currency}/%",))
            row = cursor.fetchone()
            conn.close()
            buy_price = float(row['price']) if row else 0.0
            actual_symbol = row['symbol'] if row else f"{currency}/USDT"
            positions[currency] = {
                "amount": amount,
                "entry_price": buy_price,
                "symbol": actual_symbol
            }
    return positions

# Performance Helpers
def log_performance(timestamp, total_equity, pnl, type):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO performance_log (timestamp, total_equity, pnl, type)
    VALUES (?, ?, ?, ?)
    """, (timestamp, float(total_equity), float(pnl), type))
    conn.commit()
    conn.close()

def get_performance_history(type='PAPER', limit=100):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM performance_log WHERE type = ? ORDER BY timestamp ASC LIMIT ?", (type, limit))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]

# Initialize on import
init_db()
