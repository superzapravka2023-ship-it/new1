import os


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _b(name: str, default: str = "1") -> bool:
    return os.getenv(name, default) == "1"


# ==================== TELEGRAM ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = _i("ADMIN_CHAT_ID", 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

# ==================== СЧЁТ ====================
TRADE_ENV = os.getenv("TRADE_ENV", "demo")        # demo | testnet | live
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

AUTO_TRADE = _b("AUTO_TRADE")
# Подтягивать позиции с биржи, которых нет в базе. Выключи, если на этом же
# демо-счёте работает другой бот — иначе боты будут забирать сделки друг друга.
ADOPT_ORPHANS = _b("ADOPT_ORPHANS", "0")
POSITION_USD = _f("POSITION_USD", 1000)           # номинал сделки
LEVERAGE = _i("LEVERAGE", 10)                     # маржа = POSITION_USD / LEVERAGE
MARGIN_MODE = os.getenv("MARGIN_MODE", "cross")   # cross | isolated
MAX_CONCURRENT = _i("MAX_CONCURRENT", 4)
MAX_SAME_SIDE = _i("MAX_SAME_SIDE", 3)

# ==================== ВХОД: ПЕРЕЛЁТ ОТ СРЕДНЕЙ ====================
EMA_PERIOD = _i("EMA_PERIOD", 20)                 # средняя, от которой меряем отрыв
SIGMA_WINDOW = _i("SIGMA_WINDOW", 100)            # окно для расчёта сигмы отклонения
ENTRY_SIGMA = _f("ENTRY_SIGMA", 2.5)              # порог входа, в сигмах
# 0.38, а не 0.30: при σ ниже 0.38% геометрия всё равно не даёт R:R 1.10,
# и монета отсеивалась позже и молча. Честный порог = тот, что реально бьёт.
MIN_SIGMA_PCT = _f("MIN_SIGMA_PCT", 0.39)         # монета должна реально ходить, %
MAX_SIGMA_PCT = _f("MAX_SIGMA_PCT", 4.0)          # слишком дикая — пропускаем

# рынок должен быть спокоен: перелёт одной монеты = перекос, всего рынка = тренд
BTC_CALM_PCT = _f("BTC_CALM_PCT", 0.45)           # |движение BTC| за окно, %
BTC_WINDOW_MIN = _i("BTC_WINDOW_MIN", 15)
USE_BTC_FILTER = _b("USE_BTC_FILTER")

# объём на выбросе должен гаснуть — тот, кто вываливал, выдохся
VOL_FADE_MAX = _f("VOL_FADE_MAX", 0.80)           # объём последней свечи / объём выброса
USE_VOL_FADE = _b("USE_VOL_FADE")
IMPULSE_LOOKBACK = _i("IMPULSE_LOOKBACK", 6)      # где ищем свечу-выброс

# ==================== ВЫХОД ====================
# TP — доля пути обратно к средней. SL — на столько сигм дальше отрыва.
TP_FRACTION = _f("TP_FRACTION", 0.60)
SL_SIGMA = _f("SL_SIGMA", 0.9)
MIN_TP_PCT = _f("MIN_TP_PCT", 0.45)               # ниже — комиссия съедает слишком много
MAX_SL_PCT = _f("MAX_SL_PCT", 2.5)                # потолок риска на сделку
MIN_RR = _f("MIN_RR", 1.10)                       # TP к SL после комиссий
TIME_STOP_MIN = _i("TIME_STOP_MIN", 90)           # не отработало — выходим (0 = выкл)

FEE_TAKER = _f("FEE_TAKER", 0.055)                # %, вход по рынку
FEE_MAKER = _f("FEE_MAKER", 0.020)                # %, выход лимиткой

# ==================== СКАНЕР ====================
TIMEFRAME = os.getenv("TIMEFRAME", "5")
CANDLES = _i("CANDLES", 150)
# Перелёт в 2.5σ живёт одну-три свечи. Полный обход должен укладываться
# в это окно, иначе бот физически не видит часть движений.
SCAN_INTERVAL = _i("SCAN_INTERVAL", 15)
SYNC_INTERVAL = _i("SYNC_INTERVAL", 15)
BATCH_SIZE = _i("BATCH_SIZE", 70)
REQ_PAUSE = _f("REQ_PAUSE", 0.08)                 # пауза между свечами, сек
COOLDOWN_MIN = _i("COOLDOWN_MIN", 30)

MIN_TURNOVER = _f("MIN_TURNOVER", 7_000_000)
MAX_SPREAD_PCT = _f("MAX_SPREAD_PCT", 0.10)
MIN_AGE_DAYS = _i("MIN_AGE_DAYS", 30)
MIN_PRICE = _f("MIN_PRICE", 0.0000001)
BLACKLIST = os.getenv(
    "BLACKLIST",
    "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,TRXUSDT,LTCUSDT,LINKUSDT")

# ==================== УВЕДОМЛЕНИЯ ====================
NOTIFY_BLOCKED = _b("NOTIFY_BLOCKED", "0")
ERROR_ALERT_AFTER = _i("ERROR_ALERT_AFTER", 20)
ERROR_ALERT_COOLDOWN_MIN = _i("ERROR_ALERT_COOLDOWN_MIN", 60)

SETTABLE = {
    "POSITION_USD":   (POSITION_USD, 100, "$"),
    "LEVERAGE":       (LEVERAGE, 1, "x"),
    "MAX_CONCURRENT": (MAX_CONCURRENT, 1, "шт"),
    "MAX_SAME_SIDE":  (MAX_SAME_SIDE, 1, "шт"),
    "ENTRY_SIGMA":    (ENTRY_SIGMA, 0.1, "σ"),
    "TP_FRACTION":    (TP_FRACTION, 0.05, ""),
    "SL_SIGMA":       (SL_SIGMA, 0.05, "σ"),
    "MIN_RR":         (MIN_RR, 0.05, ""),
    "MIN_TP_PCT":     (MIN_TP_PCT, 0.05, "%"),
    "MIN_SIGMA_PCT":  (MIN_SIGMA_PCT, 0.05, "%"),
    "BTC_CALM_PCT":   (BTC_CALM_PCT, 0.05, "%"),
    "VOL_FADE_MAX":   (VOL_FADE_MAX, 0.05, ""),
    "TIME_STOP_MIN":  (TIME_STOP_MIN, 15, "м"),
    "MIN_TURNOVER":   (MIN_TURNOVER, 5_000_000, "$"),
    "COOLDOWN_MIN":   (COOLDOWN_MIN, 15, "м"),
}


def blacklist() -> set[str]:
    return {s.strip().upper() for s in BLACKLIST.split(",") if s.strip()}


def net_win(tp_pct: float, pos: float | None = None) -> float:
    """Чистая прибыль при тейке: вход по рынку, выход лимиткой."""
    p = pos or POSITION_USD
    g = p * tp_pct / 100
    return g - p * FEE_TAKER / 100 - (p + g) * FEE_MAKER / 100


def net_loss(sl_pct: float, pos: float | None = None) -> float:
    """Чистый убыток при стопе: обе стороны по рынку."""
    p = pos or POSITION_USD
    g = p * sl_pct / 100
    return g + p * FEE_TAKER / 100 + (p - g) * FEE_TAKER / 100


def rr_after_fees(tp_pct: float, sl_pct: float) -> float:
    w, l = net_win(tp_pct), net_loss(sl_pct)
    return w / l if l > 0 else 0.0


def breakeven_wr(tp_pct: float, sl_pct: float) -> float:
    w, l = net_win(tp_pct), net_loss(sl_pct)
    return l / (l + w) * 100 if (l + w) else 0.0
