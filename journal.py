"""Lapisan database bersama v2: pencatatan trade & snapshot + agregasi per periode.

Dipakai DUA proses di VPS yang sama:
  - ZupinBot  : record_trade() saat posisi ditutup, record_snapshot() tiap siklus.
  - PnL bot   : stats_for_date(), stats_for_range() saat user kirim /pnl, /week, /month.
"""
import os
import time
import sqlite3
from contextlib import closing

DB_PATH = os.getenv("JOURNAL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db"))
WIB_OFFSET = 7 * 3600


def _date_wib(ts=None):
    t = (ts if ts is not None else time.time()) + WIB_OFFSET
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=8000")
    return c


def init_db():
    with closing(_conn()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, date TEXT NOT NULL, symbol TEXT NOT NULL,
            outcome TEXT NOT NULL, side TEXT, entry REAL, exit_price REAL,
            sl REAL, tp REAL, pnl_usd REAL, mode TEXT, order_id INTEGER)""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(trades)").fetchall()]
        if "order_id" not in cols:
            c.execute("ALTER TABLE trades ADD COLUMN order_id INTEGER")
        c.execute("""CREATE TABLE IF NOT EXISTS snapshots(
            ts INTEGER NOT NULL, date TEXT NOT NULL, equity REAL,
            initial_capital REAL, unrealized REAL, daily_pnl REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_trades_date ON trades(date)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_snap_date ON snapshots(date)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_oid ON trades(order_id) WHERE order_id IS NOT NULL")
        c.commit()


def record_trade(symbol, outcome, side=None, entry=None, exit_price=None,
                 sl=None, tp=None, pnl_usd=None, mode="synthetic", order_id=None, ts=None):
    ts = int(ts or time.time())
    try:
        with closing(_conn()) as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO trades(ts,date,symbol,outcome,side,entry,exit_price,sl,tp,pnl_usd,mode,order_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, _date_wib(ts), str(symbol).upper(), str(outcome).upper(), side,
                 entry, exit_price, sl, tp, pnl_usd, mode, order_id))
            c.commit()
            return cur.rowcount == 1
    except Exception:
        return False


def record_snapshot(equity, initial_capital, unrealized=0.0, daily_pnl=0.0, ts=None):
    ts = int(ts or time.time())
    try:
        with closing(_conn()) as c:
            c.execute("""INSERT INTO snapshots(ts,date,equity,initial_capital,unrealized,daily_pnl)
                         VALUES(?,?,?,?,?,?)""",
                      (ts, _date_wib(ts), equity, initial_capital, unrealized, daily_pnl))
            c.commit()
        return True
    except Exception:
        return False


def latest_snapshot(date=None):
    with closing(_conn()) as c:
        if date:
            row = c.execute("""SELECT equity,initial_capital,unrealized,daily_pnl,ts FROM snapshots
                               WHERE date=? ORDER BY ts DESC LIMIT 1""", (date,)).fetchone()
        else:
            row = c.execute("""SELECT equity,initial_capital,unrealized,daily_pnl,ts FROM snapshots
                               ORDER BY ts DESC LIMIT 1""").fetchone()
    if not row:
        return None
    return {"equity": row[0], "initial_capital": row[1], "unrealized": row[2],
            "daily_pnl": row[3], "ts": row[4]}


def _coin(symbol):
    s = str(symbol).upper()
    for suf in ("USDT", "USDC", "USD", "PERP", "-", "_"):
        s = s.replace(suf, "")
    return s or symbol


def _aggregate(rows):
    """Agregasi baris trade [(outcome, symbol, pnl_usd, mode, date)] -> dict stats."""
    tp = sum(1 for r in rows if r[0] == "TP")
    sl = sum(1 for r in rows if r[0] == "SL")
    other = len(rows) - tp - sl
    per_coin, per_mode, per_day, realized = {}, {}, {}, 0.0
    for outcome, symbol, pnl, mode, date in rows:
        coin = _coin(symbol)
        per_coin[coin] = per_coin.get(coin, 0) + 1
        per_mode[mode or "?"] = per_mode.get(mode or "?", 0) + 1
        per_day[date] = per_day.get(date, 0) + 1
        if pnl is not None:
            realized += float(pnl)
    decided = tp + sl
    wr = round(tp / decided * 100) if decided else None
    return {
        "total": len(rows), "tp": tp, "sl": sl, "other": other,
        "wr": wr, "per_coin": per_coin, "per_mode": per_mode,
        "per_day": per_day, "realized_usd": round(realized, 2),
        "active_days": len(per_day),
    }


def stats_for_date(date):
    with closing(_conn()) as c:
        rows = c.execute("SELECT outcome,symbol,pnl_usd,mode,date FROM trades WHERE date=?", (date,)).fetchall()
    stats = _aggregate(rows)
    stats["date"] = date
    stats["snapshot"] = latest_snapshot(date) or latest_snapshot()
    return stats


def stats_for_range(start_date, end_date):
    """Agregat trade dari start_date sampai end_date (inclusive, format YYYY-MM-DD)."""
    with closing(_conn()) as c:
        rows = c.execute("SELECT outcome,symbol,pnl_usd,mode,date FROM trades WHERE date>=? AND date<=?",
                         (start_date, end_date)).fetchall()
    stats = _aggregate(rows)
    stats["start_date"] = start_date
    stats["end_date"] = end_date
    # snapshot: ambil terbaru dalam range, fallback ke global terbaru
    stats["snapshot"] = latest_snapshot(end_date) or latest_snapshot()
    # equity awal: snapshot paling awal dalam range (untuk hitung PnL periode)
    with closing(_conn()) as c:
        first = c.execute("""SELECT equity FROM snapshots WHERE date>=? AND date<=?
                             ORDER BY ts ASC LIMIT 1""", (start_date, end_date)).fetchone()
    stats["equity_start"] = first[0] if first else None
    return stats
