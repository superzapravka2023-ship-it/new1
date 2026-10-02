import logging
import os
import sqlite3
import threading
import time

import config as C

log = logging.getLogger("storage")
_lock = threading.Lock()


def _connect(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return sqlite3.connect(path, check_same_thread=False)


try:
    _conn = _connect(C.DB_PATH)
except (sqlite3.OperationalError, OSError) as e:
    log.warning("БД %s недоступна (%s). Подключи Volume на /data, иначе статистика "
                "теряется при редеплое. Пишу в ./bot.db", C.DB_PATH, e)
    C.DB_PATH = "bot.db"
    _conn = _connect(C.DB_PATH)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, direction TEXT, ts INTEGER,
    qty REAL, entry REAL, tp REAL, sl REAL, leverage INTEGER,
    dev_sigma REAL, sigma_pct REAL, rr REAL, tp_pct REAL, sl_pct REAL,
    status TEXT DEFAULT 'open',      -- open | closed
    outcome TEXT DEFAULT '',         -- tp | sl | manual
    pnl REAL DEFAULT 0, closed_ts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mutes (symbol TEXT PRIMARY KEY, until INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS blacklist (symbol TEXT PRIMARY KEY);
CREATE INDEX IF NOT EXISTS idx_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_closed ON trades(closed_ts);
"""
with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def _q(sql, args=(), fetch=None):
    with _lock:
        cur = _conn.execute(sql, args)
        row = cur.fetchone() if fetch == "one" else (cur.fetchall() if fetch == "all"
                                                     else cur.lastrowid)
        _conn.commit()
        return row


# ---------------- настройки ----------------
def load_settings():
    for r in _q("SELECT key, value FROM settings", fetch="all"):
        if hasattr(C, r["key"]):
            cur = getattr(C, r["key"])
            setattr(C, r["key"], int(r["value"]) if isinstance(cur, int) else r["value"])


def save_setting(key, value):
    _q("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, float(value)))
    cur = getattr(C, key)
    setattr(C, key, int(value) if isinstance(cur, int) else float(value))


def reset_settings():
    _q("DELETE FROM settings")


# ---------------- чёрный список ----------------
def blacklist() -> set[str]:
    rows = _q("SELECT symbol FROM blacklist", fetch="all")
    return C.blacklist() | {r["symbol"] for r in rows}


def ban(symbol: str):
    _q("INSERT OR IGNORE INTO blacklist(symbol) VALUES(?)", (symbol.upper(),))


def unban(symbol: str):
    _q("DELETE FROM blacklist WHERE symbol=?", (symbol.upper(),))


def extra_banned() -> list[str]:
    return [r["symbol"] for r in _q("SELECT symbol FROM blacklist ORDER BY symbol",
                                    fetch="all")]


# ---------------- кулдаун ----------------
def touch_cooldown(symbol: str, minutes: int):
    _q("INSERT INTO mutes(symbol,until) VALUES(?,?) "
       "ON CONFLICT(symbol) DO UPDATE SET until=excluded.until",
       (symbol, int(time.time()) + minutes * 60))


def in_cooldown(symbol: str) -> bool:
    r = _q("SELECT until FROM mutes WHERE symbol=?", (symbol,), fetch="one")
    return bool(r and r["until"] > time.time())


def cooldown_list():
    return _q("SELECT symbol, until FROM mutes WHERE until>? ORDER BY until",
              (int(time.time()),), fetch="all")


# ---------------- сделки ----------------
def add_trade(symbol, direction, qty, entry, tp, sl, leverage,
              dev_sigma=0.0, sigma_pct=0.0, rr=0.0, tp_pct=0.0, sl_pct=0.0) -> int:
    return _q("INSERT INTO trades(symbol,direction,ts,qty,entry,tp,sl,leverage,"
              "dev_sigma,sigma_pct,rr,tp_pct,sl_pct) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (symbol, direction, int(time.time()), qty, entry, tp, sl, leverage,
               dev_sigma, sigma_pct, rr, tp_pct, sl_pct))


def open_trades():
    return _q("SELECT * FROM trades WHERE status='open' ORDER BY ts DESC", fetch="all")


def recent_trades(limit=12):
    return _q("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,), fetch="all")


def get_trade(tid):
    return _q("SELECT * FROM trades WHERE id=?", (tid,), fetch="one")


def update_trade(tid, **kw):
    if not kw:
        return
    sets = ",".join(f"{k}=?" for k in kw)
    _q(f"UPDATE trades SET {sets} WHERE id=?", (*kw.values(), tid))


# ---------------- статистика ----------------
def stats(hours: int):
    since = int(time.time()) - hours * 3600
    rows = _q("SELECT * FROM trades WHERE ts>=?", (since,), fetch="all")
    closed = [r for r in rows if r["status"] == "closed"]
    wins = [r for r in closed if r["pnl"] > 0]
    losses = [r for r in closed if r["pnl"] <= 0]
    pnls = [r["pnl"] for r in closed]
    longs = [r for r in closed if r["direction"] == "long"]
    shorts = [r for r in closed if r["direction"] == "short"]
    rrs = [r["rr"] for r in closed if r["rr"]]
    gw = sum(r["pnl"] for r in wins)
    gl = abs(sum(r["pnl"] for r in losses))
    return {
        "total": len(rows), "open": len(rows) - len(closed), "closed": len(closed),
        "wins": len(wins), "losses": len(losses),
        "winrate": len(wins) / len(closed) * 100 if closed else 0.0,
        "pnl": sum(pnls),
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "avg": sum(pnls) / len(pnls) if pnls else 0.0,
        "pf": gw / gl if gl else 0.0,
        "by_tp": len([r for r in closed if r["outcome"] == "tp"]),
        "by_sl": len([r for r in closed if r["outcome"] == "sl"]),
        "long_n": len(longs),
        "long_pnl": sum(r["pnl"] for r in longs),
        "short_n": len(shorts),
        "short_pnl": sum(r["pnl"] for r in shorts),
        "avg_rr": sum(rrs) / len(rrs) if rrs else 0.0,
        "need_wr": 100 / (1 + sum(rrs) / len(rrs)) if rrs else 0.0,
    }


def top_symbols(hours: int, limit: int = 5):
    since = int(time.time()) - hours * 3600
    return _q("SELECT symbol, COUNT(*) n, SUM(pnl) p FROM trades "
              "WHERE status='closed' AND ts>=? GROUP BY symbol "
              "ORDER BY p DESC LIMIT ?", (since, limit), fetch="all")


def worst_symbols(hours: int, limit: int = 5):
    since = int(time.time()) - hours * 3600
    return _q("SELECT symbol, COUNT(*) n, SUM(pnl) p FROM trades "
              "WHERE status='closed' AND ts>=? GROUP BY symbol "
              "ORDER BY p ASC LIMIT ?", (since, limit), fetch="all")


def day_pnl() -> float:
    r = _q("SELECT COALESCE(SUM(pnl),0) s FROM trades WHERE closed_ts>=?",
           (int(time.time()) - 86400,), fetch="one")
    return float(r["s"]) if r else 0.0
