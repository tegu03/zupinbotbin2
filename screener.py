"""Screener tahap-1: murni data (TANPA AI). Ranking simbol trending dari klines mainnet.
Skor per simbol: alignment SMA (close>sma20>sma50 atau kebalikan) + momentum 6 jam +
ekspansi volume. Simbol dgn ATR% < MIN_STOP_PCT dibuang (geometri stop mustahil)."""
import asyncio
import httpx
from config import CONFIG

def _sma(a, n): return sum(a[-n:]) / n if len(a) >= n else None

def score_symbol(closes, highs, lows, vols):
    if len(closes) < 60:
        return None
    last = closes[-1]
    s20, s50 = _sma(closes, 20), _sma(closes, 50)
    if not (s20 and s50):
        return None
    # ATR% sederhana (14 bar)
    trs = [max(h - l, abs(h - c), abs(l - c)) for h, l, c in zip(highs[-15:], lows[-15:], closes[-16:-1])]
    atr_pct = (sum(trs) / len(trs)) / last * 100 if trs else 0
    if atr_pct < CONFIG.min_stop_pct * 100:
        return None  # terlalu tenang: stop >= min tidak masuk akal
    mom = (last - closes[-24]) / closes[-24] * 100  # ~6 jam @15m
    v_now = sum(vols[-24:]); v_prev = sum(vols[-48:-24]) or 1
    vol_x = v_now / v_prev
    up = last > s20 > s50
    down = last < s20 < s50
    if not (up or down):
        return None  # bukan trending -> bukan kandidat
    direction = "long" if up else "short"
    score = 2.0 + min(abs(mom) / 2, 1.5) + min(max(vol_x - 1, 0), 1.0)
    return {"direction": direction, "score": round(score, 2), "mom_pct": round(mom, 2),
            "vol_x": round(vol_x, 2), "atr_pct": round(atr_pct, 3), "last": last}

async def _klines(c, sym):
    try:
        r = await c.get(f"{CONFIG.binance_data_base}/fapi/v1/klines",
                        params={"symbol": sym, "interval": CONFIG.interval, "limit": 100}, timeout=20)
        r.raise_for_status()
        return sym, r.json()
    except Exception:
        return sym, None

async def screen(symbols):
    """-> list kandidat terurut skor desc, maks SCREENER_TOP_N."""
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as c:
        rows = await asyncio.gather(*[_klines(c, s) for s in symbols])
    out = []
    for sym, kl in rows:
        if not kl:
            continue
        closes = [float(k[4]) for k in kl]
        highs = [float(k[2]) for k in kl]
        lows = [float(k[3]) for k in kl]
        vols = [float(k[5]) for k in kl]
        sc = score_symbol(closes, highs, lows, vols)
        if sc:
            out.append({"symbol": sym, **sc})
    out.sort(key=lambda x: -x["score"])
    return out[:CONFIG.screener_top_n]
