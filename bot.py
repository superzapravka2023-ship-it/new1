import asyncio
import collections
import contextlib
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

import config as C
import storage as db
from bybit import Bybit, Ticker
from strategy import Signal, btc_move_pct, probe, scan
from trade import BybitTrade, TradeManager, plural

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

bot = Bot(C.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

ENV_LABEL = {"demo": "🧪 ДЕМО", "testnet": "🧪 TESTNET", "live": "💵 РЕАЛ"}

# Пресеты согласованы: при меньшем пороге входа тейк выходит мельче,
# поэтому σ монеты требуется БОЛЬШЕ, иначе геометрия всё равно не пустит.
PRESETS = {
    "many": ("🌊 Больше сделок", {"ENTRY_SIGMA": 2.3, "TP_FRACTION": 0.70,
                                  "MIN_RR": 1.05, "MIN_SIGMA_PCT": 0.30,
                                  "MIN_TP_PCT": 0.40, "BTC_CALM_PCT": 0.60,
                                  "VOL_FADE_MAX": 0.90, "COOLDOWN_MIN": 20}),
    "bal":  ("⚖️ Баланс",       {"ENTRY_SIGMA": 2.5, "TP_FRACTION": 0.60,
                                  "MIN_RR": 1.10, "MIN_SIGMA_PCT": 0.39,
                                  "MIN_TP_PCT": 0.45, "BTC_CALM_PCT": 0.45,
                                  "VOL_FADE_MAX": 0.80, "COOLDOWN_MIN": 30}),
    "pick": ("🎯 Строже",        {"ENTRY_SIGMA": 3.0, "TP_FRACTION": 0.60,
                                  "MIN_RR": 1.25, "MIN_SIGMA_PCT": 0.32,
                                  "MIN_TP_PCT": 0.50, "BTC_CALM_PCT": 0.30,
                                  "VOL_FADE_MAX": 0.70, "COOLDOWN_MIN": 60}),
}


class State:
    started = time.time()
    paused = False
    symbols = 0
    scanned = 0
    checked = 0
    last_scan = 0.0
    scans = 0
    errors = 0
    last_error = ""
    last_error_ts = 0.0
    err_alerted = 0.0
    universe: list[str] = []
    tickers: dict[str, Ticker] = {}
    btc_move = 0.0
    signals_seen = 0
    trader: TradeManager | None = None
    trade_err = ""
    balance = {"equity": 0.0, "available": 0.0}

    # ---- диагностика: почему не было сделок
    why = collections.Counter()          # на каком фильтре отвалилось
    gates = collections.Counter()        # почему не вошли при живом сигнале
    best_sigma = 0.0                     # самый большой отрыв, что видели
    best_sym = ""
    best_ts = 0.0
    sigma_hist = collections.Counter()   # распределение σ монет
    entry_errs = 0
    last_entry_err = ""
    last_entry_err_ts = 0.0
    last_signal_ts = 0.0
    empty_universe = 0                   # циклов с пустым списком монет
    overrides: dict[str, tuple[float, float]] = {}   # база перебила Railway


S = State()


REASONS = (
    ("мало истории",   "📉 мало свечей"),
    ("нулевой разброс", "📉 нулевой разброс"),
    ("монета стоит",   "😴 монета тихая (σ мала)"),
    ("слишком дикая",  "🌪 слишком дикая (σ велика)"),
    ("отрыв",          "📏 отрыв меньше порога"),
    ("рынок двигается", "₿ BTC шевелится"),
    ("выброс ещё идёт", "🔊 объём не погас"),
    ("цель мала",      "💸 тейк мал, комиссия съест"),
    ("стоп широк",     "🛑 стоп шире потолка"),
    ("соотношение",    "⚖️ R:R ниже порога"),
)


def why_bucket(why: str) -> str:
    for head, label in REASONS:
        if why.startswith(head):
            return label
    return why[:28]


# ================================================================ утилиты
def fnum(x: float) -> str:
    if x == 0:
        return "0"
    if x >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def money(x: float) -> str:
    if abs(x) >= 1_000_000_000:
        return f"${x / 1e9:.2f}B"
    if abs(x) >= 1_000_000:
        return f"${x / 1e6:.1f}M"
    if abs(x) >= 1000:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def bar(v: float, mx: float, cells: int = 10) -> str:
    n = max(0, min(cells, round(v / mx * cells))) if mx else 0
    return "▰" * n + "▱" * (cells - n)


def ago(ts: float) -> str:
    if not ts:
        return "—"
    s = int(time.time() - ts)
    if s < 60:
        return f"{s}с"
    if s < 3600:
        return f"{s // 60}м"
    if s < 86400:
        return f"{s // 3600}ч {s % 3600 // 60}м"
    return f"{s // 86400}д"


def admin_only(uid: int) -> bool:
    return C.ADMIN_CHAT_ID == 0 or uid == C.ADMIN_CHAT_ID


async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None = None):
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(text, reply_markup=kb)


# ================================================================ клавиатуры
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💼 Позиции"), KeyboardButton(text="📊 Сегодня")],
            [KeyboardButton(text="📈 30 дней"), KeyboardButton(text="💰 Счёт")],
            [KeyboardButton(text="📜 История"), KeyboardButton(text="📡 Статус")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="❓ Стратегия")],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Выбери раздел")


def nav(*rows, back: str = "main") -> InlineKeyboardMarkup:
    kb = [list(r) for r in rows if r]
    kb.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav:{back}"),
               InlineKeyboardButton(text="⬅️ Меню", callback_data="nav:main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Позиции", callback_data="nav:pos"),
         InlineKeyboardButton(text="💰 Счёт", callback_data="nav:acc")],
        [InlineKeyboardButton(text="📊 Сегодня", callback_data="nav:day"),
         InlineKeyboardButton(text="📈 30 дней", callback_data="nav:month")],
        [InlineKeyboardButton(text="📜 История", callback_data="nav:hist"),
         InlineKeyboardButton(text="📡 Статус", callback_data="nav:status")],
        [InlineKeyboardButton(text="🔬 Почему нет сделок", callback_data="nav:diag")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="nav:settings"),
         InlineKeyboardButton(text="⛔️ Чёрный список", callback_data="nav:ban")],
        [InlineKeyboardButton(text="❓ Стратегия", callback_data="nav:help")],
        [InlineKeyboardButton(
            text="⏸ Пауза" if not S.paused else "▶️ Запустить",
            callback_data="pause")],
    ])


# ================================================================ экраны
def screen_main() -> str:
    d = db.stats(24)
    dot = "⏸ <b>Пауза</b>" if S.paused else "🟢 <b>Сканер работает</b>"
    if not S.trader:
        tl = f"⚪️ Торговля выключена{(' · ' + S.trade_err) if S.trade_err else ''}"
    else:
        tl = (f"{ENV_LABEL.get(C.TRADE_ENV, C.TRADE_ENV)} · "
              f"{'🤖 авто' if C.AUTO_TRADE else '✋ вручную'}")
    return (
        "🎯 <b>REVERSION SCALPER</b>\n"
        f"<i>Bybit · отскок после перелёта, {C.TIMEFRAME}м · лонг и шорт</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{dot}  ·  скан {ago(S.last_scan)} назад\n"
        f"{tl}\n\n"
        f"🔍 Монет в работе:  <b>{S.scanned}</b> из {S.symbols}\n"
        f"💼 Открыто:  <b>{d['open']}</b> из {C.MAX_CONCURRENT}\n"
        f"📊 За сутки:  <b>{d['closed']}</b> сделок · "
        f"<b>${d['pnl']:+.2f}</b> · {d['winrate']:.0f}% в плюс\n"
        f"💰 Депозит:  <b>${S.balance['equity']:,.2f}</b>\n"
        f"₿ BTC {C.BTC_WINDOW_MIN}м:  <b>{S.btc_move:+.2f}%</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"Вход от <b>{C.ENTRY_SIGMA:g}σ</b> · тейк {C.TP_FRACTION:.0%} пути к средней · "
        f"стоп {C.SL_SIGMA:g}σ\n"
        f"Сделка ${C.POSITION_USD:,.0f} · {C.LEVERAGE}x · {C.MARGIN_MODE}"
    ).replace(",", " ")


def screen_status() -> str:
    up = int(time.time() - S.started)
    err = (f"\n⚠️ <b>Ошибка</b> ({ago(S.last_error_ts)} назад)\n"
           f"<code>{S.last_error}</code>\n" if S.last_error else "")
    return (
        "📡 <b>СТАТУС</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{'⏸ на паузе' if S.paused else '🟢 работает'}\n"
        f"Аптайм: <b>{up // 3600}ч {up % 3600 // 60}м</b> · циклов <b>{S.scans}</b>\n"
        f"Свечей разобрано: <b>{S.checked}</b> · ошибок <b>{S.errors}</b>\n"
        f"Последний скан: <b>{ago(S.last_scan)} назад</b>\n"
        f"Монет в отборе: <b>{S.scanned}</b> из <b>{S.symbols}</b>\n"
        f"Сигналов найдено: <b>{S.signals_seen}</b>\n"
        f"{err}\n"
        "<b>Условия входа</b>\n"
        f"<pre>таймфрейм   {C.TIMEFRAME} мин\n"
        f"перелёт     ≥ {C.ENTRY_SIGMA:g}σ от EMA{C.EMA_PERIOD}\n"
        f"σ монеты    {C.MIN_SIGMA_PCT:g}–{C.MAX_SIGMA_PCT:g}%\n"
        f"BTC спокоен {('≤ ' + format(C.BTC_CALM_PCT, 'g') + '%') if C.USE_BTC_FILTER else 'выкл'}\n"
        f"объём гаснет {('≤ ' + format(C.VOL_FADE_MAX, 'g')) if C.USE_VOL_FADE else 'выкл'}\n"
        f"тейк        {C.TP_FRACTION:.0%} пути к средней\n"
        f"стоп        {C.SL_SIGMA:g}σ дальше\n"
        f"мин. R:R    {C.MIN_RR:g}\n"
        f"мин. тейк   {C.MIN_TP_PCT:g}%\n"
        f"тайм-стоп   {C.TIME_STOP_MIN} мин\n"
        f"оборот      ≥ {money(C.MIN_TURNOVER)}\n"
        f"кулдаун       {C.COOLDOWN_MIN} мин</pre>"
    )


def screen_diag() -> tuple[str, InlineKeyboardMarkup]:
    """Живая воронка отбора: на каком именно шаге умирают все кандидаты."""
    total = sum(S.why.values())
    L = ["🔬 <b>ДИАГНОСТИКА</b>",
         "━━━━━━━━━━━━━━━━━━━━"]

    if S.overrides:
        L.append("ℹ️ <b>Эти параметры взяты из кнопок, а не из Railway:</b>")
        L.append("<pre>")
        for k, (env, cur) in S.overrides.items():
            L.append(f"{LABELS.get(k, k)[:14]:<14} {_fmt(k, env)} → {_fmt(k, cur)}")
        L.append("</pre>")
        L.append("<i>Сброс: ⚙️ Настройки → ♻️</i>\n")

    if not total:
        L.append("Сканер ещё не сделал ни одной проверки.\n")
        if S.empty_universe:
            L.append(f"⚠️ Список монет пуст уже <b>{S.empty_universe}</b> циклов.\n"
                     f"Смягчи спред (сейчас ≤{C.MAX_SPREAD_PCT:g}%) "
                     f"или оборот (≥{money(C.MIN_TURNOVER)}).")
        return "\n".join(L), nav(back="diag")

    L.append(f"Проверок: <b>{total}</b> · монет в отборе <b>{S.scanned}</b> "
             f"из {S.symbols}\n")
    L.append("<b>Где умирают кандидаты</b>")
    L.append("<pre>")
    for label, n in S.why.most_common(10):
        share = n / total * 100
        L.append(f"{bar(share, 100, 8)} {share:>5.1f}%  {label}")
    L.append("</pre>")

    # самый близкий к порогу отрыв — сразу видно, порог виноват или нет
    if S.best_sigma:
        L.append(f"📏 Максимальный отрыв за всё время:\n"
                 f"   <b>{S.best_sigma:.2f}σ</b> · {S.best_sym} · "
                 f"{ago(S.best_ts)} назад\n"
                 f"   порог входа <b>{C.ENTRY_SIGMA:g}σ</b>")

    # распределение собственной сигмы — проходят ли монеты по геометрии
    if S.sigma_hist:
        tot = sum(S.sigma_hist.values())
        L.append("\n<b>σ монет (собственный разброс)</b>")
        L.append("<pre>")
        top = max(S.sigma_hist.values())
        for key in ("<0.2", "0.2-0.3", "0.3-0.4", "0.4-0.6", "0.6-1.0", ">1.0"):
            n = S.sigma_hist.get(key, 0)
            L.append(f"{key:>8}%  {bar(n, top, 8)} {n / tot * 100:>5.1f}%")
        L.append("</pre>")
        L.append(f"<i>Для R:R {C.MIN_RR:g} при отрыве {C.ENTRY_SIGMA:g}σ "
                 f"нужна σ ≥ {need_sigma():.2f}%</i>")

    L.append(f"\n🎯 Сигналов найдено: <b>{S.signals_seen}</b>"
             + (f" · последний {ago(S.last_signal_ts)} назад"
                if S.signals_seen and S.last_signal_ts else ""))

    if S.gates:
        L.append("\n<b>Сигнал был, но не вошли</b>")
        L.append("<pre>")
        for g, n in S.gates.most_common(6):
            L.append(f"{n:>4} × {g}")
        L.append("</pre>")

    if S.entry_errs:
        L.append(f"\n⛔️ <b>Ошибок входа: {S.entry_errs}</b> "
                 f"({ago(S.last_entry_err_ts)} назад)\n"
                 f"<code>{S.last_entry_err}</code>")

    if S.empty_universe:
        L.append(f"\n⚠️ Пустой список монет: <b>{S.empty_universe}</b> циклов")

    return "\n".join(L), nav(
        [InlineKeyboardButton(text="🧹 Сбросить счётчики", callback_data="diag:reset")],
        back="diag")


def need_sigma() -> float:
    """Минимальная σ монеты, при которой геометрия проходит по R:R."""
    lo, hi = 0.01, 10.0
    for _ in range(60):
        mid = (lo + hi) / 2
        tp = C.TP_FRACTION * C.ENTRY_SIGMA * mid
        sl = C.SL_SIGMA * mid
        if tp < C.MIN_TP_PCT or C.rr_after_fees(tp, sl) < C.MIN_RR:
            lo = mid
        else:
            hi = mid
    return hi


def sigma_key(s: float) -> str:
    if s < 0.2:
        return "<0.2"
    if s < 0.3:
        return "0.2-0.3"
    if s < 0.4:
        return "0.3-0.4"
    if s < 0.6:
        return "0.4-0.6"
    if s < 1.0:
        return "0.6-1.0"
    return ">1.0"


def open_pnl() -> tuple[float, float]:
    total, worst = 0.0, 0.0
    for t in db.open_trades():
        tk = S.tickers.get(t["symbol"])
        if not tk:
            continue
        sign = 1 if t["direction"] == "long" else -1
        p = (tk.last - t["entry"]) * t["qty"] * sign
        total += p
        worst = min(worst, p)
    return total, worst


def screen_positions() -> tuple[str, InlineKeyboardMarkup]:
    if not S.trader:
        return ("💼 <b>ПОЗИЦИИ</b>\n━━━━━━━━━━━━━━━━━━━━\nТорговля не подключена.\n\n"
                f"<i>{S.trade_err or 'Добавь BYBIT_API_KEY и BYBIT_API_SECRET.'}</i>",
                nav(back="pos"))
    rows = db.open_trades()
    adopt = InlineKeyboardButton(text="🔄 Подтянуть с биржи", callback_data="adopt")
    if not rows:
        d = db.stats(24)
        return (f"💼 <b>ПОЗИЦИИ</b> · {ENV_LABEL.get(C.TRADE_ENV, C.TRADE_ENV)}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "Открытых позиций нет.\n\n"
                f"За сутки закрыто <b>{d['closed']}</b> · PnL <b>${d['pnl']:+.2f}</b>\n"
                f"Готов взять до {C.MAX_CONCURRENT} сделок.", nav([adopt], back="pos"))
    out = [f"💼 <b>ПОЗИЦИИ</b> · {ENV_LABEL.get(C.TRADE_ENV, C.TRADE_ENV)}",
           "━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for t in rows:
        tk = S.tickers.get(t["symbol"])
        sign = 1 if t["direction"] == "long" else -1
        pct = ((tk.last - t["entry"]) / t["entry"] * 100 * sign) if tk else 0.0
        usd = (tk.last - t["entry"]) * t["qty"] * sign if tk else 0.0
        icon = "🟢" if usd >= 0 else "🔴"
        d = "🟢 ЛОНГ" if t["direction"] == "long" else "🔴 ШОРТ"
        out.append(f"{icon} <b>{t['symbol']}</b> {d} <code>#{t['id']}</code>\n"
                   f"    вход {fnum(t['entry'])} · сейчас {fnum(tk.last) if tk else '—'}\n"
                   f"    <b>{pct:+.2f}%</b>  ≈  <b>${usd:+.2f}</b>\n"
                   f"    🎯 {fnum(t['tp'])} · 🛑 {fnum(t['sl'])} · {ago(t['ts'])}")
        btns.append(InlineKeyboardButton(text=f"{icon} {t['symbol'].replace('USDT', '')}",
                                         callback_data=f"pos:{t['id']}"))
    tot, _ = open_pnl()
    out.append(f"\nСуммарно в позициях: <b>${tot:+.2f}</b>")
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return "\n".join(out), nav(*grid, [adopt], back="pos")


def screen_position(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    t = db.get_trade(tid)
    if not t:
        return "Сделка не найдена.", nav(back="pos")
    tk = S.tickers.get(t["symbol"])
    closed = t["status"] == "closed"
    sign = 1 if t["direction"] == "long" else -1
    usd = t["pnl"] if closed else ((tk.last - t["entry"]) * t["qty"] * sign if tk else 0)
    pct = ((tk.last - t["entry"]) / t["entry"] * 100 * sign) if tk else 0.0
    d = "🟢 ЛОНГ" if t["direction"] == "long" else "🔴 ШОРТ"
    out = [f"{'🏁' if closed else '💼'} <b>{t['symbol']}</b> {d} <code>#{tid}</code>",
           "━━━━━━━━━━━━━━━━━━━━",
           f"{t['qty']:g} @ <b>{fnum(t['entry'])}</b>",
           f"Объём ${t['qty'] * t['entry']:,.0f} · {t['leverage']}x · "
           f"маржа ${t['qty'] * t['entry'] / max(t['leverage'], 1):,.2f}".replace(",", " "),
           "",
           f"<pre>TP  {fnum(t['tp'])}   +{t['tp_pct']:.2f}%\n"
           f"SL  {fnum(t['sl'])}   −{t['sl_pct']:.2f}%</pre>"]
    if t["dev_sigma"]:
        out.append(f"Вход на перелёте <b>{t['dev_sigma']:+.2f}σ</b> · "
                   f"σ монеты {t['sigma_pct']:.2f}% · R:R {t['rr']:.2f}")
    if closed:
        names = {"tp": "🎯 тейк", "sl": "🛑 стоп", "manual": "✋ вручную"}
        out.append(f"\nИтог: <b>${t['pnl']:+.2f}</b> · {names.get(t['outcome'], '')} · "
                   f"закрыта {ago(t['closed_ts'])} назад")
        kb = nav([InlineKeyboardButton(text="⬅️ К позициям", callback_data="nav:pos")],
                 back=f"pos:{tid}")
    else:
        out.append(f"\nСейчас: <b>{pct:+.2f}%</b> ≈ <b>${usd:+.2f}</b>")
        kb = nav([InlineKeyboardButton(text="❌ Закрыть по рынку",
                                       callback_data=f"cls:{tid}")],
                 [InlineKeyboardButton(text="📉 Bybit",
                                       url=f"https://www.bybit.com/trade/usdt/{t['symbol']}"),
                  InlineKeyboardButton(text="⬅️ К позициям", callback_data="nav:pos")],
                 back=f"pos:{tid}")
    return "\n".join(out), kb


def _stats_block(d: dict, hours: int) -> list[str]:
    wr, need = d["winrate"], d["need_wr"]
    if d["closed"] < 20:
        verdict = f"🟡 Выборка мала ({d['closed']}) — нужно хотя бы 100 сделок"
    elif wr >= need + 3:
        verdict = "🟢 Винрейт уверенно выше безубытка"
    elif wr >= need:
        verdict = "🟡 Винрейт чуть выше безубытка — держим на грани"
    else:
        verdict = "🔴 Винрейт ниже безубытка — стратегия в минусе"
    return [
        f"<pre>Сделок      {d['closed']}\n"
        f"  🎯 тейк   {d['by_tp']}\n"
        f"  🛑 стоп   {d['by_sl']}\n"
        f"Winrate     {wr:.1f}%\n"
        f"PnL         ${d['pnl']:+.2f}\n"
        f"Средняя     ${d['avg']:+.2f}\n"
        f"Лучшая      ${d['best']:+.2f}\n"
        f"Худшая      ${d['worst']:+.2f}</pre>",
        f"{bar(wr, 100)}",
        "",
        f"<pre>средний R:R    {d['avg_rr']:.2f}\n"
        f"нужен винрейт  {need:.1f}%\n"
        f"фактический    {wr:.1f}%</pre>",
        verdict,
    ]


def screen_day() -> tuple[str, InlineKeyboardMarkup]:
    d = db.stats(24)
    tot, _ = open_pnl()
    out = ["📊 <b>СЕГОДНЯ</b>  <i>(24 часа)</i>", "━━━━━━━━━━━━━━━━━━━━"]
    out += _stats_block(d, 24)
    out += ["",
            f"Лонги {d['long_n']} (${d['long_pnl']:+.2f}) · "
            f"шорты {d['short_n']} (${d['short_pnl']:+.2f})",
            f"Открыто сейчас: {d['open']} · в них <b>${tot:+.2f}</b>"]
    return "\n".join(out), nav(
        [InlineKeyboardButton(text="📈 30 дней", callback_data="nav:month"),
         InlineKeyboardButton(text="📜 История", callback_data="nav:hist")], back="day")


def screen_month() -> tuple[str, InlineKeyboardMarkup]:
    d = db.stats(24 * 30)
    out = ["📈 <b>30 ДНЕЙ</b>", "━━━━━━━━━━━━━━━━━━━━"]
    out += _stats_block(d, 24 * 30)
    out += ["",
            f"Профит-фактор: <b>{d['pf']:.2f}</b>",
            f"Лонги {d['long_n']} (${d['long_pnl']:+.2f}) · "
            f"шорты {d['short_n']} (${d['short_pnl']:+.2f})"]
    top = db.top_symbols(24 * 30)
    worst = db.worst_symbols(24 * 30)
    if top:
        out += ["", "<b>Лучшие монеты</b>", "<pre>"]
        for r in top:
            out.append(f"{r['symbol']:<14}{r['n']:>3} сд  ${r['p']:+.2f}")
        out.append("</pre>")
    if worst and worst[0]["p"] < 0:
        out += ["<b>Худшие монеты</b>", "<pre>"]
        for r in worst:
            if r["p"] < 0:
                out.append(f"{r['symbol']:<14}{r['n']:>3} сд  ${r['p']:+.2f}")
        out.append("</pre>")
    return "\n".join(out), nav(
        [InlineKeyboardButton(text="📊 Сегодня", callback_data="nav:day"),
         InlineKeyboardButton(text="📜 История", callback_data="nav:hist")], back="month")


def screen_history() -> tuple[str, InlineKeyboardMarkup]:
    rows = db.recent_trades(12)
    if not rows:
        return "📜 <b>ИСТОРИЯ</b>\n━━━━━━━━━━━━━━━━━━━━\nПока пусто.", nav(back="hist")
    ICON = {"tp": "🎯", "sl": "🛑", "manual": "✋"}
    out = ["📜 <b>ИСТОРИЯ</b>", "━━━━━━━━━━━━━━━━━━━━"]
    btns = []
    for t in rows:
        if t["status"] == "open":
            icon, res = "⏳", "в работе"
        else:
            icon = ICON.get(t["outcome"], "•")
            res = f"${t['pnl']:+.2f}"
        d = "🟢" if t["direction"] == "long" else "🔴"
        out.append(f"{icon}{d} <b>{t['symbol']}</b> <code>#{t['id']}</code> · {res} · "
                   f"{ago(t['ts'])} назад")
        btns.append(InlineKeyboardButton(text=f"{icon} #{t['id']}",
                                         callback_data=f"pos:{t['id']}"))
    grid = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    return "\n".join(out), nav(*grid, back="hist")


def screen_account() -> tuple[str, InlineKeyboardMarkup]:
    if not S.trader:
        return ("💰 <b>СЧЁТ</b>\n━━━━━━━━━━━━━━━━━━━━\nТорговля не подключена.\n\n"
                f"<i>{S.trade_err or 'Добавь ключи API в Railway.'}</i>", nav(back="acc"))
    b = S.balance
    d, m = db.stats(24), db.stats(24 * 30)
    out = [f"💰 <b>СЧЁТ</b> · {ENV_LABEL.get(C.TRADE_ENV, C.TRADE_ENV)}",
           "━━━━━━━━━━━━━━━━━━━━",
           f"Депозит: <b>${b['equity']:,.2f}</b>",
           f"Свободно: <b>${b['available']:,.2f}</b>",
           f"Занято: <b>${b['equity'] - b['available']:,.2f}</b>",
           "",
           f"<pre>за сутки   ${d['pnl']:+.2f}  ({d['closed']} сд)\n"
           f"за 30 дней ${m['pnl']:+.2f}  ({m['closed']} сд)</pre>",
           "<b>Параметры</b>",
           f"<pre>Сделка     ${C.POSITION_USD:,.0f}\n"
           f"Плечо      {C.LEVERAGE}x  ({C.MARGIN_MODE})\n"
           f"Маржа      ${C.POSITION_USD / C.LEVERAGE:,.2f}\n"
           f"Слотов     {C.MAX_CONCURRENT} (в одну сторону {C.MAX_SAME_SIDE})\n"
           f"Вход       от {C.ENTRY_SIGMA:g}σ\n"
           f"Тейк       {C.TP_FRACTION:.0%} пути к средней\n"
           f"Стоп       {C.SL_SIGMA:g}σ дальше перелёта\n"
           f"Мин. R:R   {C.MIN_RR:g}\n"
           f"Средний R:R {db.stats(24 * 30)['avg_rr']:.2f} -> "
           f"нужен винрейт {db.stats(24 * 30)['need_wr']:.0f}%</pre>"]
    extra = []
    if C.TRADE_ENV == "demo":
        extra.append([InlineKeyboardButton(text="💵 Пополнить демо +50 000",
                                           callback_data="demo")])
    return "\n".join(out).replace(",", " "), nav(*extra, back="acc")


def screen_wipe() -> tuple[str, InlineKeyboardMarkup]:
    d = db.stats(24 * 3650)
    rows = db.open_trades()
    out = ["🧹 <b>ОЧИСТКА СТАТИСТИКИ</b>", "━━━━━━━━━━━━━━━━━━━━",
           f"<pre>Всего записей   {d['total']}\n"
           f"  закрытых      {d['closed']}\n"
           f"  открытых      {len(rows)}</pre>",
           "<b>Что делает каждая кнопка</b>",
           "",
           "🧽 <b>Очистить историю</b> — удаляет только закрытые сделки. "
           "Статистика обнуляется, открытые позиции остаются под управлением. "
           "Это то, что нужно в большинстве случаев.",
           "",
           "🙈 <b>Забыть открытые</b> — снимает с ведения открытые сделки "
           "(например, подтянутые от другого бота). "
           "<b>Позиции на бирже при этом не закрываются</b> — бот просто перестаёт "
           "за ними следить, закрывать придётся вручную в Bybit.",
           "",
           "💣 <b>Стереть всё</b> — и историю, и открытые, и кулдауны. "
           "Полный чистый лист.",
           "",
           "<i>Сами позиции на бирже ни одна кнопка не трогает.</i>"]
    kb = nav([InlineKeyboardButton(text="🧽 Очистить историю",
                                   callback_data="wipe:closed")],
             [InlineKeyboardButton(text="🙈 Забыть открытые",
                                   callback_data="wipe:open")],
             [InlineKeyboardButton(text="💣 Стереть всё", callback_data="wipe:all")],
             back="wipe")
    return "\n".join(out), kb


def screen_wipe_confirm(what: str) -> tuple[str, InlineKeyboardMarkup]:
    d = db.stats(24 * 3650)
    n_open = len(db.open_trades())
    texts = {
        "closed": (f"Удалить <b>{d['closed']}</b> "
                   f"{plural(d['closed'], 'закрытую сделку', 'закрытые сделки', 'закрытых сделок')}?\n"
                   f"Открытые ({n_open}) останутся под управлением."),
        "open":   (f"Снять с ведения <b>{n_open}</b> "
                   f"{plural(n_open, 'открытую сделку', 'открытые сделки', 'открытых сделок')}?\n"
                   "Позиции на бирже останутся — закрывать вручную."),
        "all":    (f"Стереть <b>все {d['total']}</b> записей и кулдауны?\n"
                   "Позиции на бирже останутся."),
    }
    return ("⚠️ <b>ПОДТВЕРДИ</b>\n━━━━━━━━━━━━━━━━━━━━\n" + texts[what] +
            "\n\n<i>Отменить будет нельзя.</i>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, выполнить",
                                      callback_data=f"wipe!:{what}"),
                 InlineKeyboardButton(text="❌ Отмена", callback_data="nav:wipe")]]))


def screen_ban() -> tuple[str, InlineKeyboardMarkup]:
    extra = db.extra_banned()
    base = sorted(C.blacklist())
    out = ["⛔️ <b>ЧЁРНЫЙ СПИСОК</b>", "━━━━━━━━━━━━━━━━━━━━",
           "Эти монеты не торгуем — за 5 минут они почти не ходят, "
           "и тейк висел бы часами.", "",
           "<b>Из переменной BLACKLIST</b>",
           "<pre>" + ", ".join(s.replace("USDT", "") for s in base) + "</pre>"]
    btns = []
    if extra:
        out += ["<b>Добавлено кнопками</b>",
                "<pre>" + ", ".join(s.replace("USDT", "") for s in extra) + "</pre>"]
        for s in extra:
            btns.append(InlineKeyboardButton(text=f"🔓 {s.replace('USDT', '')}",
                                             callback_data=f"unban:{s}"))
    out += ["", "<i>Чтобы забанить монету — открой её сделку в истории "
                "и нажми «⛔️ В бан».</i>"]
    grid = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    return "\n".join(out), nav(*grid, back="ban")


LABELS = {
    "POSITION_USD": "💵 Сделка", "LEVERAGE": "⚡️ Плечо",
    "MAX_CONCURRENT": "🔢 Слотов", "MAX_SAME_SIDE": "↔️ В одну сторону",
    "ENTRY_SIGMA": "📐 Порог перелёта", "TP_FRACTION": "🎯 Доля пути",
    "SL_SIGMA": "🛑 Стоп в сигмах", "MIN_RR": "⚖️ Мин. R:R",
    "MIN_TP_PCT": "📏 Мин. тейк", "MIN_SIGMA_PCT": "💨 Мин. σ монеты",
    "BTC_CALM_PCT": "₿ BTC спокоен", "VOL_FADE_MAX": "📉 Затухание объёма",
    "TIME_STOP_MIN": "⏱ Тайм-стоп", "MIN_TURNOVER": "💧 Оборот",
    "COOLDOWN_MIN": "🧊 Кулдаун",
}


def _fmt(key: str, v: float) -> str:
    if key == "MIN_TURNOVER":
        return money(v)
    if key == "POSITION_USD":
        return f"${v:g}"
    return f"{v:g}{C.SETTABLE[key][2]}"


def settings_kb(page: int = 0) -> InlineKeyboardMarkup:
    keys = list(C.SETTABLE)
    chunk = keys[page * 6:(page + 1) * 6]
    rows = [[InlineKeyboardButton(text="➖", callback_data=f"set:{k}:-:{page}"),
             InlineKeyboardButton(text=f"{LABELS.get(k, k)}  {_fmt(k, getattr(C, k))}",
                                  callback_data="noop"),
             InlineKeyboardButton(text="➕", callback_data=f"set:{k}:+:{page}")]
            for k in chunk]
    pages = (len(keys) + 5) // 6
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"sp:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"sp:{page + 1}"))
    rows.append(row)
    rows.append([InlineKeyboardButton(text=v[0], callback_data=f"preset:{k}")
                 for k, v in PRESETS.items()])
    rows.append([
        InlineKeyboardButton(text="🤖 Авто: ВКЛ" if C.AUTO_TRADE else "✋ Авто: выкл",
                             callback_data="t:auto"),
        InlineKeyboardButton(text="₿ BTC: " + ("вкл" if C.USE_BTC_FILTER else "выкл"),
                             callback_data="t:btc"),
    ])
    rows.append([
        InlineKeyboardButton(text="📉 Объём: " + ("вкл" if C.USE_VOL_FADE else "выкл"),
                             callback_data="t:vol"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔄 Подтягивать: " + ("вкл" if C.ADOPT_ORPHANS else "ВЫКЛ"),
                             callback_data="t:adopt"),
        InlineKeyboardButton(text="♻️ Сброс порогов", callback_data="preset:reset"),
    ])
    rows.append([InlineKeyboardButton(text="🧹 Очистка статистики", callback_data="nav:wipe"),
                 InlineKeyboardButton(text="⬅️ Меню", callback_data="nav:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def screen_settings() -> str:
    ex = []
    for s in (0.4, 0.6, 1.0):
        dev = C.ENTRY_SIGMA * s
        tp, sl = dev * C.TP_FRACTION, C.SL_SIGMA * s
        ex.append(f"σ {s:.1f}%  TP {tp:.2f}%  SL {sl:.2f}%  "
                  f"R:R {C.rr_after_fees(tp, sl):.2f}")
    return ("⚙️ <b>НАСТРОЙКИ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Меняются на лету, сохраняются в базе — редеплой не нужен.\n\n"
            "TP и SL у каждой сделки свои: считаются от величины перелёта.\n"
            "<pre>" + "\n".join(ex) + "</pre>\n"
            "«Больше сделок» опускает порог до 2σ, «Строже» поднимает до 3σ. "
            "Смотри число сделок за сутки и подбирай.")


HELP = (
    "❓ <b>КАК РАБОТАЕТ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Идея</b>\n"
    "Монета резко оторвалась от своей средней цены. Если при этом весь рынок "
    "спокоен — это локальный перекос: кого-то ликвидировали или вывалили объём "
    "в тонкий стакан. Такие перекосы выправляются. Входим против движения "
    "и забираем возврат к средней.\n\n"
    "<b>Четыре условия</b>\n"
    f"1. Отрыв от EMA{C.EMA_PERIOD} ≥ {C.ENTRY_SIGMA:g}σ — сигма у каждой монеты своя\n"
    f"2. BTC спокоен (≤ {C.BTC_CALM_PCT:g}% за {C.BTC_WINDOW_MIN} мин) — "
    "монета улетела одна, а не со всем рынком\n"
    f"3. Объём гаснет (≤ {C.VOL_FADE_MAX:g} от пика) — тот, кто вываливал, выдохся\n"
    f"4. Геометрия проходит: R:R ≥ {C.MIN_RR:g} после комиссий\n\n"
    "<b>Второе условие — главное.</b> Монета падает одна — отскочит. "
    "Падает вместе с рынком — это тренд, и против него входить нельзя.\n\n"
    "<b>Выход</b>\n"
    f"Тейк лимиткой на {C.TP_FRACTION:.0%} пути обратно к средней, "
    f"стоп {C.SL_SIGMA:g}σ дальше перелёта. У каждой сделки свои — считаются "
    f"от величины отрыва. Если за {C.TIME_STOP_MIN} мин не отработало, выходим по рынку.\n\n"
    "<b>Отбор монет</b>\n"
    f"Оборот ≥ {money(C.MIN_TURNOVER)}, возраст ≥ {C.MIN_AGE_DAYS} дней, "
    f"σ монеты {C.MIN_SIGMA_PCT:g}–{C.MAX_SIGMA_PCT:g}%, не в чёрном списке. "
    "Тихие монеты пропускаем: на них комиссия съедает прибыль.\n\n"
    "<b>Риск</b>\n"
    f"{C.MAX_CONCURRENT} сделки одновременно, не больше {C.MAX_SAME_SIDE} "
    f"в одну сторону, кулдаун {C.COOLDOWN_MIN} мин по монете.\n\n"
    "✅ Главное отличие от скальпинга на тонких целях: здесь тейк в 10–20 раз "
    "больше комиссии, и хватает винрейта около 44%, а не 94%."
)


# ================================================================ команды
@dp.message(Command("start", "menu", "help"))
async def cmd_start(m: Message):
    if not admin_only(m.from_user.id):
        return
    await m.answer(f"Твой chat_id: <code>{m.chat.id}</code>", reply_markup=main_kb())
    await m.answer(screen_main(), reply_markup=menu_kb())


@dp.message(F.text == "💼 Позиции")
async def b_pos(m: Message):
    if admin_only(m.from_user.id):
        t, k = screen_positions()
        await m.answer(t, reply_markup=k)


@dp.message(F.text == "📊 Сегодня")
async def b_day(m: Message):
    if admin_only(m.from_user.id):
        t, k = screen_day()
        await m.answer(t, reply_markup=k)


@dp.message(F.text == "📈 30 дней")
async def b_month(m: Message):
    if admin_only(m.from_user.id):
        t, k = screen_month()
        await m.answer(t, reply_markup=k)


@dp.message(F.text == "💰 Счёт")
async def b_acc(m: Message):
    if not admin_only(m.from_user.id):
        return
    await refresh_balance()
    t, k = screen_account()
    await m.answer(t, reply_markup=k)


@dp.message(F.text == "📜 История")
async def b_hist(m: Message):
    if admin_only(m.from_user.id):
        t, k = screen_history()
        await m.answer(t, reply_markup=k)


@dp.message(F.text == "📡 Статус")
async def b_status(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(screen_status(), reply_markup=nav(back="status"))


@dp.message(F.text == "⚙️ Настройки")
async def b_set(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(screen_settings(), reply_markup=settings_kb(0))


@dp.message(F.text == "❓ Стратегия")
async def b_help(m: Message):
    if admin_only(m.from_user.id):
        await m.answer(HELP, reply_markup=nav(back="help"))


@dp.message(Command("pause"))
async def cmd_pause(m: Message):
    if admin_only(m.from_user.id):
        S.paused = True
        await m.answer("⏸ Сканер на паузе.", reply_markup=menu_kb())


@dp.message(Command("resume"))
async def cmd_resume(m: Message):
    if admin_only(m.from_user.id):
        S.paused = False
        await m.answer("🟢 Сканер запущен.", reply_markup=menu_kb())


# ================================================================ колбэки
@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery):
    w = c.data.split(":")[1]
    if w == "main":
        await safe_edit(c.message, screen_main(), menu_kb())
    elif w == "status":
        await safe_edit(c.message, screen_status(), nav(
            [InlineKeyboardButton(text="🔬 Диагностика", callback_data="nav:diag")],
            back="status"))
    elif w == "diag":
        t, k = screen_diag()
        await safe_edit(c.message, t, k)
    elif w == "pos":
        t, k = screen_positions()
        await safe_edit(c.message, t, k)
    elif w == "day":
        t, k = screen_day()
        await safe_edit(c.message, t, k)
    elif w == "month":
        t, k = screen_month()
        await safe_edit(c.message, t, k)
    elif w == "hist":
        t, k = screen_history()
        await safe_edit(c.message, t, k)
    elif w == "acc":
        await refresh_balance()
        t, k = screen_account()
        await safe_edit(c.message, t, k)
    elif w == "ban":
        t, k = screen_ban()
        await safe_edit(c.message, t, k)
    elif w == "wipe":
        t, k = screen_wipe()
        await safe_edit(c.message, t, k)
    elif w == "settings":
        await safe_edit(c.message, screen_settings(), settings_kb(0))
    elif w == "help":
        await safe_edit(c.message, HELP, nav(back="help"))
    await c.answer()


@dp.callback_query(F.data == "diag:reset")
async def cb_diag_reset(c: CallbackQuery):
    if not admin_only(c.from_user.id):
        return await c.answer("нет доступа", show_alert=True)
    S.why.clear()
    S.gates.clear()
    S.sigma_hist.clear()
    S.best_sigma, S.best_sym, S.best_ts = 0.0, "", 0.0
    S.entry_errs, S.last_entry_err, S.last_entry_err_ts = 0, "", 0.0
    t, k = screen_diag()
    await safe_edit(c.message, t, k)
    await c.answer("🧹 Счётчики сброшены")


@dp.callback_query(F.data.startswith("pos:"))
async def cb_pos(c: CallbackQuery):
    tid = int(c.data.split(":")[1])
    t, k = screen_position(tid)
    row = db.get_trade(tid)
    if row and row["status"] == "closed":
        k.inline_keyboard.insert(0, [InlineKeyboardButton(
            text=f"⛔️ {row['symbol'].replace('USDT', '')} в бан",
            callback_data=f"ban:{row['symbol']}")])
    await safe_edit(c.message, t, k)
    await c.answer()


@dp.callback_query(F.data.startswith("sp:"))
async def cb_page(c: CallbackQuery):
    await safe_edit(c.message, screen_settings(), settings_kb(int(c.data.split(":")[1])))
    await c.answer()


@dp.callback_query(F.data == "pause")
async def cb_pause(c: CallbackQuery):
    S.paused = not S.paused
    await safe_edit(c.message, screen_main(), menu_kb())
    await c.answer("⏸ Пауза" if S.paused else "🟢 Запущен")


@dp.callback_query(F.data.startswith("set:"))
async def cb_set(c: CallbackQuery):
    _, key, sign, page = c.data.split(":")
    step = C.SETTABLE[key][1]
    val = max(0, getattr(C, key) + (step if sign == "+" else -step))
    db.save_setting(key, val)
    await safe_edit(c.message, screen_settings(), settings_kb(int(page)))
    await c.answer(f"{LABELS.get(key, key)} → {_fmt(key, val)}")


@dp.callback_query(F.data.startswith("preset:"))
async def cb_preset(c: CallbackQuery):
    name = c.data.split(":")[1]
    if name == "reset":
        db.reset_settings()
        for k, (d, _, _) in C.SETTABLE.items():
            setattr(C, k, d)
        await c.answer("♻️ Сброшено к переменным Railway")
    else:
        title, vals = PRESETS[name]
        for k, v in vals.items():
            db.save_setting(k, v)
        await c.answer(f"{title} применён")
    await safe_edit(c.message, screen_settings(), settings_kb(0))


@dp.callback_query(F.data.startswith("t:"))
async def cb_toggle(c: CallbackQuery):
    w = c.data.split(":")[1]
    msg = ""
    if w == "auto":
        C.AUTO_TRADE = not C.AUTO_TRADE
        msg = "🤖 Авто-вход включён" if C.AUTO_TRADE else "✋ Авто-вход выключен"
    elif w == "btc":
        C.USE_BTC_FILTER = not C.USE_BTC_FILTER
        msg = ("₿ Фильтр рынка включён" if C.USE_BTC_FILTER
               else "₿ Фильтр рынка выключен")
    elif w == "adopt":
        C.ADOPT_ORPHANS = not C.ADOPT_ORPHANS
        msg = ("🔄 Буду подтягивать позиции с биржи" if C.ADOPT_ORPHANS
               else "🔄 Чужие позиции больше не трогаю")
    elif w == "vol":
        C.USE_VOL_FADE = not C.USE_VOL_FADE
        msg = ("📉 Фильтр затухания включён" if C.USE_VOL_FADE
               else "📉 Фильтр затухания выключен")
    await safe_edit(c.message, screen_settings(), settings_kb(0))
    await c.answer(msg)


@dp.callback_query(F.data.startswith("wipe:"))
async def cb_wipe_ask(c: CallbackQuery):
    t, k = screen_wipe_confirm(c.data.split(":")[1])
    await safe_edit(c.message, t, k)
    await c.answer()


@dp.callback_query(F.data.startswith("wipe!:"))
async def cb_wipe_do(c: CallbackQuery):
    what = c.data.split(":")[1]
    if what == "closed":
        n = db.clear_closed()
        msg = f"🧽 Удалено закрытых сделок: {n}"
    elif what == "open":
        n = db.forget_open()
        msg = f"🙈 Снято с ведения: {n}. Позиции на бирже остались."
    else:
        n = db.clear_all()
        msg = f"💣 Стёрто записей: {n}"
    log.info("очистка %s: %d", what, n)
    t, k = screen_wipe()
    await safe_edit(c.message, t, k)
    await c.answer(msg, show_alert=True)


@dp.callback_query(F.data.startswith("ban:"))
async def cb_ban(c: CallbackQuery):
    sym = c.data.split(":")[1]
    db.ban(sym)
    await c.answer(f"⛔️ {sym} в чёрном списке", show_alert=True)


@dp.callback_query(F.data.startswith("unban:"))
async def cb_unban(c: CallbackQuery):
    sym = c.data.split(":")[1]
    db.unban(sym)
    t, k = screen_ban()
    await safe_edit(c.message, t, k)
    await c.answer(f"🔓 {sym} снова в скане")


@dp.callback_query(F.data.startswith("cls:"))
async def cb_close(c: CallbackQuery):
    if not S.trader:
        await c.answer("Торговля не подключена", show_alert=True)
        return
    try:
        msg = await S.trader.close_now(int(c.data.split(":")[1]))
    except Exception as e:      # noqa: BLE001
        await c.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    await c.answer(msg, show_alert=True)


@dp.callback_query(F.data == "adopt")
async def cb_adopt(c: CallbackQuery):
    if not S.trader:
        await c.answer("Торговля не подключена", show_alert=True)
        return
    await c.answer("Смотрю, что открыто на бирже…")
    try:
        found = await S.trader.adopt_orphans()
    except Exception as e:      # noqa: BLE001
        await c.message.answer(f"⚠️ Не вышло: {e}")
        return
    await c.message.answer(
        ("🔄 <b>Взял под управление</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(found))
        if found else "✅ Все позиции уже под управлением.")
    t, k = screen_positions()
    await safe_edit(c.message, t, k)


@dp.callback_query(F.data == "demo")
async def cb_demo(c: CallbackQuery):
    if not S.trader:
        await c.answer("Торговля не подключена", show_alert=True)
        return
    try:
        await S.trader.api.demo_money("50000")
        await refresh_balance()
    except Exception as e:      # noqa: BLE001
        await c.answer(f"Ошибка: {e}"[:200], show_alert=True)
        return
    t, k = screen_account()
    await safe_edit(c.message, t, k)
    await c.answer("💵 Демо-счёт пополнен на 50 000 USDT")


# ================================================================ сканер
def pick_universe(tickers: dict[str, Ticker], instruments: dict, now_ms: int) -> list[str]:
    banned = db.blacklist()
    out = []
    for sym, t in tickers.items():
        if sym in banned:
            continue
        inst = instruments.get(sym)
        if not inst or inst.get("status") != "Trading":
            continue
        if t.last < C.MIN_PRICE or t.turnover24 < C.MIN_TURNOVER:
            continue
        if t.spread_pct > C.MAX_SPREAD_PCT:
            continue
        age = (now_ms - inst["launch"]) / 86_400_000 if inst["launch"] else 999
        if age < C.MIN_AGE_DAYS:
            continue
        out.append(sym)
    out.sort(key=lambda s: -tickers[s].turnover24)
    return out


async def alert_errors():
    if not C.ADMIN_CHAT_ID or S.errors < C.ERROR_ALERT_AFTER:
        return
    if time.time() - S.err_alerted < C.ERROR_ALERT_COOLDOWN_MIN * 60:
        return
    S.err_alerted = time.time()
    await bot.send_message(
        C.ADMIN_CHAT_ID,
        "⚠️ <b>БОТ СЫПЛЕТСЯ</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"Ошибок: <b>{S.errors}</b> · успешно разобрано: {S.checked}\n\n"
        f"<code>{S.last_error}</code>\n\n"
        "<i>Сделок не будет, пока это не исправить.</i>")


async def handle_signal(sig: Signal):
    S.signals_seen += 1
    S.last_signal_ts = time.time()
    if not (S.trader and C.AUTO_TRADE):
        S.gates["торговля выключена"] += 1
        return
    crashed = False
    try:
        ok, msg = await S.trader.open_trade(sig)
    except Exception as e:      # noqa: BLE001
        log.exception("entry %s", sig.symbol)
        ok, msg, crashed = False, f"{type(e).__name__}: {e}"[:180], True
    if ok:
        await bot.send_message(C.ADMIN_CHAT_ID, msg, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="💼 Позиции",
                                                   callback_data="nav:pos")]]))
        return

    log.info("вход не состоялся: %s", msg)
    # Ворота (слоты, кулдаун, та же монета) — нормальная работа, просто счётчик.
    # Всё остальное — техническая поломка, о ней молчать нельзя.
    gate_words = ("уже открыта", "занято ", "уже ", "кулдаун")
    body = msg.lstrip("⛔️ ")
    technical = crashed or not any(w in body for w in gate_words)
    S.gates[body[:40]] += 1
    if technical:
        S.entry_errs += 1
        S.last_entry_err = msg
        S.last_entry_err_ts = time.time()
        if C.ADMIN_CHAT_ID:
            await bot.send_message(
                C.ADMIN_CHAT_ID,
                f"⛔️ <b>ВХОД НЕ УДАЛСЯ</b> · {sig.symbol}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"<code>{msg}</code>\n\n"
                f"Сигнал был: {abs(sig.dev_sigma):.2f}σ, R:R {sig.rr:.2f}\n"
                f"<i>Ошибок входа всего: {S.entry_errs}</i>")
    elif C.NOTIFY_BLOCKED:
        await bot.send_message(C.ADMIN_CHAT_ID, msg)


async def scan_loop():
    api = Bybit()
    instruments = await api.instruments()
    instr_ts = time.time()
    await asyncio.sleep(2)
    if C.ADMIN_CHAT_ID:
        await bot.send_message(C.ADMIN_CHAT_ID,
                               f"🟢 Сканер запущен. Контрактов: <b>{len(instruments)}</b>",
                               reply_markup=main_kb())
        await bot.send_message(C.ADMIN_CHAT_ID, screen_main(), reply_markup=menu_kb())

    cursor = 0
    while True:
        try:
            if S.paused:
                await asyncio.sleep(5)
                continue
            if time.time() - instr_ts > 6 * 3600:
                instruments = await api.instruments()
                instr_ts = time.time()

            tickers = await api.tickers()
            S.tickers, S.symbols = tickers, len(tickers)
            now_ms = int(time.time() * 1000)
            S.universe = pick_universe(tickers, instruments, now_ms)
            S.scanned = len(S.universe)
            S.last_scan, S.scans = time.time(), S.scans + 1

            try:
                btc = await api.klines("BTCUSDT", C.TIMEFRAME, 12)
                S.btc_move = btc_move_pct(btc, C.BTC_WINDOW_MIN, int(C.TIMEFRAME))
            except Exception:      # noqa: BLE001
                pass

            if not S.universe:
                S.empty_universe += 1
                # это тихая смерть: бот «работает», но смотреть ему не на что
                if S.empty_universe in (3, 30, 300) and C.ADMIN_CHAT_ID:
                    await bot.send_message(
                        C.ADMIN_CHAT_ID,
                        "⚠️ <b>НЕЧЕГО СКАНИРОВАТЬ</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"Ни одна монета не прошла отбор "
                        f"({S.empty_universe} циклов подряд).\n\n"
                        f"Оборот ≥ {money(C.MIN_TURNOVER)}\n"
                        f"Спред ≤ {C.MAX_SPREAD_PCT:g}%\n"
                        f"Возраст ≥ {C.MIN_AGE_DAYS} дней\n"
                        f"Контрактов всего: {S.symbols}\n\n"
                        "<i>Сделок не будет. Смягчи спред или оборот "
                        "в ⚙️ Настройках.</i>")
                await asyncio.sleep(C.SCAN_INTERVAL)
                continue
            S.empty_universe = 0
            if cursor >= len(S.universe):
                cursor = 0
            batch = S.universe[cursor:cursor + C.BATCH_SIZE]
            cursor += C.BATCH_SIZE

            for sym in batch:
                t = tickers.get(sym)
                if not t:
                    continue
                if db.in_cooldown(sym):
                    S.why["⏳ кулдаун по монете"] += 1
                    continue
                try:
                    candles = await api.klines(sym, C.TIMEFRAME, C.CANDLES)
                    sig, why = scan(t, candles, S.btc_move)
                    S.checked += 1
                    # ---- воронка: что именно отсекло монету
                    S.why["🎯 сигнал" if sig else why_bucket(why)] += 1
                    if probe["sigma"] > 0:
                        S.sigma_hist[sigma_key(probe["sigma"])] += 1
                    if probe["dev_sigma"] > S.best_sigma:
                        S.best_sigma = probe["dev_sigma"]
                        S.best_sym, S.best_ts = sym, time.time()
                except Exception as e:      # noqa: BLE001
                    log.warning("scan %s: %s", sym, e)
                    S.errors += 1
                    S.last_error = f"{type(e).__name__}: {e}"[:200]
                    S.last_error_ts = time.time()
                    await alert_errors()
                    continue
                if sig and C.ADMIN_CHAT_ID:
                    await handle_signal(sig)
                await asyncio.sleep(C.REQ_PAUSE)

        except Exception as e:      # noqa: BLE001
            S.errors += 1
            S.last_error = f"{type(e).__name__}: {e}"[:200]
            S.last_error_ts = time.time()
            log.exception("scan loop: %s", e)
        await asyncio.sleep(C.SCAN_INTERVAL)


# ================================================================ служебное
async def notify(text: str):
    if C.ADMIN_CHAT_ID:
        await bot.send_message(C.ADMIN_CHAT_ID, text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="💼 Позиции", callback_data="nav:pos"),
                InlineKeyboardButton(text="📊 Сегодня", callback_data="nav:day")]]))


async def refresh_balance():
    if not S.trader:
        return
    try:
        S.balance = await S.trader.api.balance()
    except Exception as e:      # noqa: BLE001
        log.warning("balance: %s", e)


async def trade_loop():
    while True:
        await asyncio.sleep(C.SYNC_INTERVAL)
        if not S.trader:
            continue
        try:
            await S.trader.sync()
        except Exception as e:      # noqa: BLE001
            log.warning("sync: %s", e)


async def balance_loop():
    while True:
        await refresh_balance()
        await asyncio.sleep(120)


async def init_trader():
    if not (C.BYBIT_API_KEY and C.BYBIT_API_SECRET):
        S.trade_err = "ключи API не заданы"
        log.warning("Торговля выключена: нет ключей")
        return
    api = BybitTrade(C.BYBIT_API_KEY, C.BYBIT_API_SECRET, C.TRADE_ENV)
    try:
        bal = await api.balance()
    except Exception as e:      # noqa: BLE001
        S.trade_err = str(e)[:120]
        log.error("Торговля недоступна: %s", e)
        await api.close()
        return
    S.balance = bal
    S.trader = TradeManager(api, notify)
    log.info("Торговля подключена: env=%s equity=%.2f", C.TRADE_ENV, bal["equity"])
    found = []
    if C.ADOPT_ORPHANS:
        try:
            found = await S.trader.adopt_orphans()
        except Exception as e:      # noqa: BLE001
            log.warning("adopt on start: %s", e)
    else:
        log.info("подтягивание чужих позиций выключено (ADOPT_ORPHANS=0)")
    if C.ADMIN_CHAT_ID:
        extra = ("\n\n🔄 Подхватил открытые позиции:\n" + "\n".join(found)) if found else ""
        await bot.send_message(
            C.ADMIN_CHAT_ID,
            f"{ENV_LABEL.get(C.TRADE_ENV, C.TRADE_ENV)} <b>счёт подключён</b>\n"
            f"Депозит <b>${bal['equity']:,.2f}</b> · свободно ${bal['available']:,.2f}\n"
            f"Сделка ${C.POSITION_USD:,.0f} · {C.LEVERAGE}x · {C.MARGIN_MODE}\n"
            f"Вход от {C.ENTRY_SIGMA:g}σ · тейк {C.TP_FRACTION:.0%} пути · "
            f"стоп {C.SL_SIGMA:g}σ{extra}".replace(",", " "))


async def warn_overrides():
    """Говорит вслух, если кнопки/пресет перебивают переменные Railway."""
    if not S.overrides or not C.ADMIN_CHAT_ID:
        return
    rows = "\n".join(
        f"{LABELS.get(k, k)}: {_fmt(k, env)} → <b>{_fmt(k, cur)}</b>"
        for k, (env, cur) in S.overrides.items())
    await bot.send_message(
        C.ADMIN_CHAT_ID,
        "ℹ️ <b>НАСТРОЙКИ ИЗ КНОПОК ПЕРЕБИВАЮТ RAILWAY</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{rows}\n\n"
        "<i>Это сохранено в базе на Volume и живёт дольше редеплоя. "
        "Чтобы вернуть значения из Railway — ⚙️ Настройки → ♻️ Сброс.</i>")


async def main():
    if not C.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    S.overrides = db.load_settings()
    if S.overrides:
        log.warning("настройки из базы перебивают Railway: %s", S.overrides)
    await init_trader()
    asyncio.create_task(warn_overrides())
    asyncio.create_task(scan_loop())
    asyncio.create_task(trade_loop())
    asyncio.create_task(balance_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
