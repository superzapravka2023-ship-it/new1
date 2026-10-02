"""Публичный клиент Bybit V5: свечи и тикеры. Ничего лишнего."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("bybit")
BASE = "https://api.bybit.com"


@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def body_pct(self) -> float:
        """Тело свечи в процентах от цены открытия, со знаком."""
        return (self.c - self.o) / self.o * 100 if self.o else 0.0

    @property
    def green(self) -> bool:
        return self.c > self.o


@dataclass
class Ticker:
    symbol: str
    last: float
    pcnt24: float
    turnover24: float
    bid: float
    ask: float

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 99.0
        return (self.ask - self.bid) / self.bid * 100


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class Bybit:
    def __init__(self, timeout: float = 15.0):
        self.cli = httpx.AsyncClient(
            base_url=BASE, timeout=timeout,
            headers={"User-Agent": "momentum-scalper/1.0"})

    async def close(self):
        await self.cli.aclose()

    async def _get(self, path: str, params: dict | None = None, retries: int = 3):
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                r = await self.cli.get(path, params=params)
                r.raise_for_status()
                data = r.json()
                if data.get("retCode") != 0:
                    raise RuntimeError(f"{path} [{data.get('retCode')}] {data.get('retMsg')}")
                return data["result"]
            except Exception as e:      # noqa: BLE001
                last = e
                await asyncio.sleep(1.0 * (attempt + 1))
        raise last  # type: ignore[misc]

    async def instruments(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cursor = None
        while True:
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = await self._get("/v5/market/instruments-info", params)
            for it in res.get("list", []):
                if it.get("quoteCoin") != "USDT" or it.get("contractType") != "LinearPerpetual":
                    continue
                out[it["symbol"]] = {
                    "launch": int(it.get("launchTime") or 0),
                    "status": it.get("status", ""),
                    "tick": _f(it.get("priceFilter", {}).get("tickSize"), 0.0001),
                    "qty_step": _f(it.get("lotSizeFilter", {}).get("qtyStep"), 0.001),
                    "min_qty": _f(it.get("lotSizeFilter", {}).get("minOrderQty"), 0.001),
                    "max_lev": _f(it.get("leverageFilter", {}).get("maxLeverage"), 10),
                }
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        log.info("instruments loaded: %d", len(out))
        return out

    async def tickers(self) -> dict[str, Ticker]:
        res = await self._get("/v5/market/tickers", {"category": "linear"})
        out: dict[str, Ticker] = {}
        for t in res.get("list", []):
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            out[sym] = Ticker(
                symbol=sym, last=_f(t.get("lastPrice")),
                pcnt24=_f(t.get("price24hPcnt")) * 100.0,
                turnover24=_f(t.get("turnover24h")),
                bid=_f(t.get("bid1Price")), ask=_f(t.get("ask1Price")))
        return out

    async def klines(self, symbol: str, interval: str, limit: int = 120) -> list[Candle]:
        """Свечи хронологически. Последняя (незакрытая) отбрасывается."""
        res = await self._get("/v5/market/kline", {
            "category": "linear", "symbol": symbol,
            "interval": interval, "limit": min(limit, 1000)})
        rows = res.get("list", [])
        out = [Candle(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]))
               for r in rows]
        out.sort(key=lambda c: c.ts)
        return out[:-1] if len(out) > 1 else out
