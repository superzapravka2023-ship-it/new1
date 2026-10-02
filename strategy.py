"""Поиск перелёта: монета оторвалась от своей средней сильнее обычного.

Логика в одну фразу: если монета улетела ОДНА, пока рынок спокоен, — это
локальный перекос, и он выправляется. Если улетела вместе с рынком — это тренд,
и входить против него нельзя.

Четыре условия:
  1. |цена − EMA| ≥ ENTRY_SIGMA сигм собственного распределения монеты
  2. BTC спокоен (перекос, а не общее движение)
  3. объём на выбросе гаснет (тот, кто вываливал, выдохся)
  4. геометрия выхода проходит по R:R после комиссий
"""
from dataclasses import dataclass, field
from statistics import pstdev
from typing import Literal, Optional

import config as C
from bybit import Candle, Ticker

Direction = Literal["long", "short"]


def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    e = values[0]
    out = []
    for v in values:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Signal:
    symbol: str
    direction: Direction
    entry: float
    tp: float
    sl: float
    ema: float
    dev_pct: float        # отрыв от средней, % (со знаком)
    dev_sigma: float      # он же в сигмах
    sigma_pct: float      # собственная сигма монеты, %
    tp_pct: float
    sl_pct: float
    rr: float
    vol_fade: float
    btc_move: float
    turnover: float
    checks: list[Check] = field(default_factory=list)

    @property
    def arrow(self) -> str:
        return "🟢 ЛОНГ" if self.direction == "long" else "🔴 ШОРТ"

    @property
    def net_win(self) -> float:
        return C.net_win(self.tp_pct)

    @property
    def net_loss(self) -> float:
        return C.net_loss(self.sl_pct)

    @property
    def breakeven(self) -> float:
        return C.breakeven_wr(self.tp_pct, self.sl_pct)


# Последняя разобранная монета — чтобы бот мог показать воронку отбора,
# а не только итоговое «сигналов нет». Заполняется на каждом вызове scan().
probe: dict[str, float] = {"sigma": 0.0, "dev_sigma": 0.0, "bars": 0}


def scan(t: Ticker, candles: list[Candle], btc_move: float
         ) -> tuple[Optional[Signal], str]:
    """Возвращает (сигнал | None, причина отказа)."""
    probe.update(sigma=0.0, dev_sigma=0.0, bars=len(candles))
    need = C.SIGMA_WINDOW + C.EMA_PERIOD + 5
    if len(candles) < need:
        return None, f"мало истории ({len(candles)} из {need})"

    closes = [c.c for c in candles]
    emas = ema_series(closes, C.EMA_PERIOD)
    last, ema = candles[-1], emas[-1]
    if ema <= 0:
        return None, "нет средней"

    # отклонения цены от средней за окно — их разброс и есть «сигма монеты»
    devs = [(closes[i] - emas[i]) / emas[i] * 100
            for i in range(len(closes) - C.SIGMA_WINDOW, len(closes))]
    sigma = pstdev(devs[:-1]) if len(devs) > 2 else 0.0
    probe["sigma"] = sigma
    if sigma <= 0:
        return None, "нулевой разброс"

    dev_pct = (last.c - ema) / ema * 100
    probe["dev_sigma"] = abs(dev_pct / sigma)

    if sigma < C.MIN_SIGMA_PCT:
        return None, f"монета стоит на месте (σ {sigma:.2f}%)"
    if sigma > C.MAX_SIGMA_PCT:
        return None, f"слишком дикая (σ {sigma:.2f}%)"

    dev_sigma = dev_pct / sigma
    if abs(dev_sigma) < C.ENTRY_SIGMA:
        return None, f"отрыв {abs(dev_sigma):.2f}σ < {C.ENTRY_SIGMA:g}σ"

    # цена под средней -> покупаем отскок; над средней -> продаём перелёт
    direction: Direction = "long" if dev_pct < 0 else "short"
    up = direction == "long"

    # рынок должен быть спокоен — иначе это не перекос, а общее движение
    if C.USE_BTC_FILTER and abs(btc_move) > C.BTC_CALM_PCT:
        return None, f"рынок двигается сам (BTC {btc_move:+.2f}%)"

    # объём на выбросе должен гаснуть
    tail = candles[-C.IMPULSE_LOOKBACK:]
    peak_vol = max(c.v for c in tail) or 1e-9
    fade = last.v / peak_vol
    if C.USE_VOL_FADE and fade > C.VOL_FADE_MAX:
        return None, f"выброс ещё идёт (объём {fade * 100:.0f}% от пика)"

    # ---------- геометрия выхода ----------
    entry = last.c
    sign = 1 if up else -1
    tp = entry + sign * abs(ema - entry) * C.TP_FRACTION   # доля пути к средней
    sl = entry - sign * sigma * C.SL_SIGMA / 100 * entry   # на сигму дальше отрыва

    tp_pct = abs(tp - entry) / entry * 100
    sl_pct = abs(sl - entry) / entry * 100
    if tp_pct < C.MIN_TP_PCT:
        return None, f"цель мала ({tp_pct:.2f}%), комиссия съест"
    if sl_pct > C.MAX_SL_PCT:
        return None, f"стоп широк ({sl_pct:.2f}%)"
    rr = C.rr_after_fees(tp_pct, sl_pct)
    if rr < C.MIN_RR:
        return None, f"соотношение {rr:.2f} < {C.MIN_RR:g}"

    checks = [
        Check("Перелёт", True,
              f"{abs(dev_sigma):.2f}σ от EMA{C.EMA_PERIOD} ({dev_pct:+.2f}%)"),
        Check("Рынок спокоен", True,
              f"BTC {btc_move:+.2f}% за {C.BTC_WINDOW_MIN} мин"
              if C.USE_BTC_FILTER else "фильтр выключен"),
        Check("Объём гаснет", True,
              f"{fade * 100:.0f}% от пика выброса" if C.USE_VOL_FADE
              else "фильтр выключен"),
        Check("Геометрия", True,
              f"R:R {rr:.2f} после комиссий · безубыток "
              f"{C.breakeven_wr(tp_pct, sl_pct):.1f}%"),
    ]
    return Signal(symbol=t.symbol, direction=direction, entry=entry, tp=tp, sl=sl,
                  ema=ema, dev_pct=dev_pct, dev_sigma=dev_sigma, sigma_pct=sigma,
                  tp_pct=tp_pct, sl_pct=sl_pct, rr=rr, vol_fade=fade,
                  btc_move=btc_move, turnover=t.turnover24, checks=checks), ""


def btc_move_pct(btc: list[Candle], minutes: int, tf_min: int) -> float:
    bars = max(1, minutes // max(tf_min, 1))
    if len(btc) < bars + 1:
        return 0.0
    ref = btc[-bars - 1].c
    return (btc[-1].c - ref) / ref * 100 if ref else 0.0
