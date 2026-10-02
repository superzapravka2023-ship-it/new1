"""Исполнение на счёте Bybit.

Вход по рынку, тейк лимиткой (мейкер), стоп на позицию. TP и SL у каждой сделки
свои — считаются от величины перелёта, поэтому берутся из сигнала, а не из конфига.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from urllib.parse import urlencode

import httpx

import config as C
import storage as db

log = logging.getLogger("trade")

HOSTS = {"demo": "https://api-demo.bybit.com",
         "testnet": "https://api-testnet.bybit.com",
         "live": "https://api.bybit.com"}
IGNORABLE = {110025, 110026, 110043, 34036, 10001, 110017}


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 сделка, 3 сделки, 7 сделок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def q_step(value: float, step: float) -> str:
    s = Decimal(str(step))
    return format((Decimal(str(value)) / s).to_integral_value(ROUND_DOWN) * s, "f")


def p_tick(value: float, tick: float) -> str:
    s = Decimal(str(tick))
    return format((Decimal(str(value)) / s).to_integral_value(ROUND_HALF_UP) * s, "f")


class BybitTrade:
    def __init__(self, key: str, secret: str, env: str = "demo", recv: int = 10_000):
        self.key, self.secret, self.env = key, secret, env
        self.base = HOSTS.get(env, HOSTS["demo"])
        self.recv = str(recv)
        self.cli = httpx.AsyncClient(base_url=self.base, timeout=20.0)
        self.filters: dict[str, dict] = {}

    async def close(self):
        await self.cli.aclose()

    def _headers(self, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        raw = ts + self.key + self.recv + payload
        sign = hmac.new(self.secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": self.recv, "X-BAPI-SIGN": sign,
                "Content-Type": "application/json"}

    async def get(self, path: str, params: dict | None = None):
        qs = urlencode(params or {})
        r = await self.cli.get(f"{path}?{qs}" if qs else path, headers=self._headers(qs))
        return self._check(path, r.json())

    async def post(self, path: str, body: dict, soft: bool = False):
        payload = json.dumps(body, separators=(",", ":"))
        r = await self.cli.post(path, content=payload, headers=self._headers(payload))
        return self._check(path, r.json(), soft)

    @staticmethod
    def _check(path: str, data: dict, soft: bool = False):
        code = data.get("retCode")
        if code != 0:
            if soft and code in IGNORABLE:
                log.info("%s: %s (пропускаем)", path, data.get("retMsg"))
                return {}
            raise RuntimeError(f"{path} [{code}] {data.get('retMsg')}")
        return data.get("result", {}) or {}

    # ------------------------------------------------------------ данные
    async def instrument(self, symbol: str) -> dict:
        if symbol in self.filters:
            return self.filters[symbol]
        res = await self.get("/v5/market/instruments-info",
                             {"category": "linear", "symbol": symbol})
        it = res["list"][0]
        f = {"qty_step": float(it["lotSizeFilter"]["qtyStep"]),
             "min_qty": float(it["lotSizeFilter"]["minOrderQty"]),
             "tick": float(it["priceFilter"]["tickSize"]),
             "max_lev": float(it["leverageFilter"]["maxLeverage"])}
        self.filters[symbol] = f
        return f

    async def balance(self) -> dict:
        res = await self.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        if not res.get("list"):
            return {"equity": 0.0, "available": 0.0}
        a = res["list"][0]
        return {"equity": float(a.get("totalEquity") or 0),
                "available": float(a.get("totalAvailableBalance") or 0)}

    async def positions(self) -> dict[str, dict]:
        res = await self.get("/v5/position/list",
                             {"category": "linear", "settleCoin": "USDT"})
        out = {}
        for p in res.get("list", []):
            size = float(p.get("size") or 0)
            if size > 0:
                out[p["symbol"]] = {"size": size, "side": p.get("side"),
                                    "avg": float(p.get("avgPrice") or 0),
                                    "sl": float(p.get("stopLoss") or 0),
                                    "lev": float(p.get("leverage") or 0)}
        return out

    async def closed_pnl(self, symbol: str) -> float:
        res = await self.get("/v5/position/closed-pnl",
                             {"category": "linear", "symbol": symbol, "limit": 1})
        rows = res.get("list", [])
        return float(rows[0]["closedPnl"]) if rows else 0.0

    async def open_orders(self, symbol: str) -> list[dict]:
        res = await self.get("/v5/order/realtime",
                             {"category": "linear", "symbol": symbol})
        return [{"qty": float(o.get("qty") or 0), "price": float(o.get("price") or 0),
                 "reduce": bool(o.get("reduceOnly"))} for o in res.get("list", [])]

    async def demo_money(self, amount: str = "50000"):
        return await self.post("/v5/account/demo-apply-money",
                               {"adjustType": 0,
                                "utaDemoApplyMoney": [{"coin": "USDT",
                                                       "amountStr": amount}]})

    # ------------------------------------------------------------ ордера
    async def prepare(self, symbol: str, lev: int):
        await self.post("/v5/position/switch-mode",
                        {"category": "linear", "symbol": symbol, "mode": 0}, soft=True)
        if C.MARGIN_MODE == "isolated":
            await self.post("/v5/position/switch-isolated",
                            {"category": "linear", "symbol": symbol, "tradeMode": 1,
                             "buyLeverage": str(lev), "sellLeverage": str(lev)}, soft=True)
        await self.post("/v5/position/set-leverage",
                        {"category": "linear", "symbol": symbol,
                         "buyLeverage": str(lev), "sellLeverage": str(lev)}, soft=True)

    async def market(self, symbol: str, side: str, qty: str):
        return await self.post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side,
            "orderType": "Market", "qty": qty, "timeInForce": "IOC", "positionIdx": 0})

    async def set_stop(self, symbol: str, price: str):
        return await self.post("/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol, "positionIdx": 0,
            "tpslMode": "Full", "stopLoss": price,
            "slTriggerBy": "LastPrice", "slOrderType": "Market"}, soft=True)

    async def take_profit(self, symbol: str, side: str, qty: str, price: str):
        return await self.post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side,
            "orderType": "Limit", "qty": qty, "price": price,
            "reduceOnly": True, "timeInForce": "GTC", "positionIdx": 0})

    async def close_market(self, symbol: str, side: str, qty: str):
        return await self.post("/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": side,
            "orderType": "Market", "qty": qty, "reduceOnly": True,
            "timeInForce": "IOC", "positionIdx": 0})

    async def cancel_all(self, symbol: str):
        return await self.post("/v5/order/cancel-all",
                               {"category": "linear", "symbol": symbol}, soft=True)


class TradeManager:
    def __init__(self, api: BybitTrade, notify):
        self.api = api
        self.notify = notify

    # ------------------------------------------------------------ ворота
    def gate(self, symbol: str, direction: str) -> str:
        opened = db.open_trades()
        if any(t["symbol"] == symbol for t in opened):
            return f"{symbol}: позиция уже открыта"
        if len(opened) >= C.MAX_CONCURRENT:
            return f"занято {len(opened)} из {C.MAX_CONCURRENT} слотов"
        same = [t for t in opened if t["direction"] == direction]
        if len(same) >= C.MAX_SAME_SIDE:
            word = "лонгов" if direction == "long" else "шортов"
            return f"уже {len(same)} {word} — больше в одну сторону не берём"
        if db.in_cooldown(symbol):
            return f"{symbol}: кулдаун {C.COOLDOWN_MIN} мин"
        return ""

    # ------------------------------------------------------------ вход
    async def open_trade(self, sig) -> tuple[bool, str]:
        sym, direction = sig.symbol, sig.direction
        blocked = self.gate(sym, direction)
        if blocked:
            return False, f"⛔️ {blocked}"

        f = await self.api.instrument(sym)
        lev = min(int(C.LEVERAGE), int(f["max_lev"]))
        bal = await self.api.balance()
        need = C.POSITION_USD / lev
        if bal["available"] < need:
            return False, (f"⛔️ Мало маржи: нужно ${need:.2f}, "
                           f"свободно ${bal['available']:.2f}")

        qty = q_step(C.POSITION_USD / sig.entry, f["qty_step"])
        if float(qty) < f["min_qty"]:
            return False, f"⛔️ {sym}: объём {qty} меньше минимума {f['min_qty']}"

        side = "Buy" if direction == "long" else "Sell"
        exit_side = "Sell" if direction == "long" else "Buy"

        await self.api.prepare(sym, lev)
        await self.api.market(sym, side, qty)

        pos = None
        for _ in range(10):
            await asyncio.sleep(1.0)
            pos = (await self.api.positions()).get(sym)
            if pos:
                break
        if not pos:
            return False, f"⚠️ {sym}: ордер ушёл, позиция не появилась"

        entry, size = pos["avg"] or sig.entry, pos["size"]
        sign = 1 if direction == "long" else -1
        # цели пересчитываем от фактической цены входа, проценты берём из сигнала
        tp_p = p_tick(entry * (1 + sign * sig.tp_pct / 100), f["tick"])
        sl_p = p_tick(entry * (1 - sign * sig.sl_pct / 100), f["tick"])

        await self.api.set_stop(sym, sl_p)
        qty_all = q_step(size, f["qty_step"])
        try:
            if float(qty_all) >= f["min_qty"]:
                await self.api.take_profit(sym, exit_side, qty_all, tp_p)
        except Exception as e:      # noqa: BLE001
            log.warning("TP %s: %s", sym, e)

        tid = db.add_trade(sym, direction, size, entry, float(tp_p), float(sl_p), lev,
                           sig.dev_sigma, sig.sigma_pct, sig.rr, sig.tp_pct, sig.sl_pct)
        db.touch_cooldown(sym, C.COOLDOWN_MIN)

        margin = size * entry / lev
        notional = size * entry
        txt = (
            f"✅ <b>ВХОД</b>  ·  {sig.arrow}\n"
            f"<b>{sym}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{size:g} @ <b>{entry:.8g}</b>\n"
            f"Объём ${notional:,.0f} · {lev}x · маржа <b>${margin:,.2f}</b>\n\n"
            f"<pre>TP  {tp_p}   +{sig.tp_pct:.2f}%   +${C.net_win(sig.tp_pct, notional):.2f}\n"
            f"SL  {sl_p}   −{sig.sl_pct:.2f}%   −${C.net_loss(sig.sl_pct, notional):.2f}</pre>\n"
            f"Перелёт <b>{sig.dev_sigma:+.2f}σ</b> · σ монеты {sig.sigma_pct:.2f}% · "
            f"BTC {sig.btc_move:+.2f}%\n"
            f"R:R <b>{sig.rr:.2f}</b> — хватит винрейта {sig.breakeven:.0f}%\n"
            f"<code>сделка #{tid}</code>"
        ).replace(",", " ")
        return True, txt

    # ------------------------------------------------------------ ведение
    async def sync(self):
        rows = db.open_trades()
        if not rows:
            return
        try:
            positions = await self.api.positions()
        except Exception as e:      # noqa: BLE001
            log.warning("positions: %s", e)
            return

        for t in rows:
            sym = t["symbol"]
            if positions.get(sym):
                # перелёт не выправился за отведённое время — выходим по рынку
                if C.TIME_STOP_MIN and time.time() - t["ts"] > C.TIME_STOP_MIN * 60:
                    try:
                        await self.close_now(t["id"], reason="time")
                    except Exception as e:      # noqa: BLE001
                        log.warning("time stop %s: %s", sym, e)
                continue
            try:
                await self.api.cancel_all(sym)
                pnl = await self.api.closed_pnl(sym)
            except Exception:      # noqa: BLE001
                pnl = 0.0
            outcome = "tp" if pnl > 0 else "sl"
            if t["status"] == "open" and t["outcome"] == "time":
                outcome = "time"
            db.update_trade(t["id"], status="closed", outcome=outcome,
                            closed_ts=int(time.time()), pnl=pnl)
            held = int(time.time() - t["ts"])
            held_txt = (f"{held // 3600}ч {held % 3600 // 60}м" if held >= 3600
                        else f"{held // 60}м {held % 60}с")
            d = db.stats(24)
            icon = "🟢" if pnl >= 0 else "🔴"
            await self.notify(
                f"{icon} <b>ВЫХОД</b> · {sym}\n"
                f"{'🟢 ЛОНГ' if t['direction'] == 'long' else '🔴 ШОРТ'}  ·  "
                f"{'тейк' if outcome == 'tp' else 'стоп'}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Вход {t['entry']:.8g} → выход "
                f"{t['tp'] if outcome == 'tp' else t['sl']:.8g}\n"
                f"PnL: <b>${pnl:+.2f}</b>  ·  в позиции {held_txt}\n\n"
                f"За сутки: <b>${d['pnl']:+.2f}</b> "
                f"({d['closed']} {plural(d['closed'], 'сделка', 'сделки', 'сделок')}, "
                f"{d['winrate']:.0f}% в плюс)\n"
                f"<code>сделка #{t['id']}</code>")

    async def adopt_orphans(self) -> list[str]:
        """Берёт под управление позиции с биржи, которых нет в базе."""
        try:
            positions = await self.api.positions()
        except Exception as e:      # noqa: BLE001
            log.warning("adopt: %s", e)
            return []
        known = {t["symbol"] for t in db.open_trades()}
        out = []
        for sym, p in positions.items():
            if sym in known or p["size"] <= 0 or p["avg"] <= 0:
                continue
            direction = "long" if p["side"] == "Buy" else "short"
            entry, size = p["avg"], p["size"]
            f = await self.api.instrument(sym)
            sign = 1 if direction == "long" else -1
            tp_p = p_tick(entry * (1 + sign * C.MIN_TP_PCT / 100), f["tick"])
            sl_p = p_tick(entry * (1 - sign * C.MAX_SL_PCT / 100), f["tick"])
            try:
                orders = await self.api.open_orders(sym)
            except Exception:      # noqa: BLE001
                orders = []
            if not any(o["reduce"] for o in orders):
                exit_side = "Sell" if direction == "long" else "Buy"
                q = q_step(size, f["qty_step"])
                if float(q) >= f["min_qty"]:
                    try:
                        await self.api.take_profit(sym, exit_side, q, tp_p)
                    except Exception as e:      # noqa: BLE001
                        log.warning("adopt tp %s: %s", sym, e)
            if p["sl"] <= 0:
                try:
                    await self.api.set_stop(sym, sl_p)
                except Exception as e:      # noqa: BLE001
                    log.warning("adopt sl %s: %s", sym, e)
            tid = db.add_trade(sym, direction, size, entry, float(tp_p), float(sl_p),
                               int(p["lev"]) or C.LEVERAGE)
            out.append(f"#{tid} {sym} {'лонг' if direction == 'long' else 'шорт'} "
                       f"{size:g} @ {entry:.8g}")
        return out

    async def close_now(self, trade_id: int, reason: str = "manual") -> str:
        t = db.get_trade(trade_id)
        if not t or t["status"] == "closed":
            return "Сделка уже закрыта"
        sym = t["symbol"]
        pos = (await self.api.positions()).get(sym)
        if not pos:
            db.update_trade(trade_id, status="closed", outcome=reason,
                            closed_ts=int(time.time()))
            return f"{sym}: позиции нет, помечена закрытой"
        f = await self.api.instrument(sym)
        exit_side = "Sell" if t["direction"] == "long" else "Buy"
        await self.api.close_market(sym, exit_side, q_step(pos["size"], f["qty_step"]))
        await self.api.cancel_all(sym)
        await asyncio.sleep(1.5)
        try:
            pnl = await self.api.closed_pnl(sym)
        except Exception:      # noqa: BLE001
            pnl = 0.0
        db.update_trade(trade_id, status="closed", outcome=reason,
                        closed_ts=int(time.time()), pnl=pnl)
        if reason == "time":
            icon = "🟢" if pnl >= 0 else "🔴"
            await self.notify(
                f"{icon} <b>ВЫХОД ПО ВРЕМЕНИ</b> · {sym}\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"За {C.TIME_STOP_MIN} мин перелёт не выправился.\n"
                f"PnL: <b>${pnl:+.2f}</b> · за сутки ${db.stats(24)['pnl']:+.2f}\n"
                f"<code>сделка #{trade_id}</code>")
        return f"❌ {sym}: закрыто ({reason}), ${pnl:+.2f}"
