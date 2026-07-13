"""Screener tahap-1: murni data (TANPA AI). Ranking simbol trending dari klines mainnet.

v6 MULTI-TIMEFRAME + ANTI-NOISE:
  - Trend 1H (EMA50/EMA200 + slope) WAJIB searah dgn arah kandidat (#6: tidak entry
    melawan trend). long hanya jika 1H up; short hanya jika 1H down.
  - ADX entry-TF >= ADX_MIN (#7: buang chop/range yang menghasilkan noise & SL beruntun).
  - Skor: alignment SMA + momentum + ekspansi volume + bonus ADX + bonus searah 1H.
Simbol dgn ATR% < MIN_STOP_PCT dibuang (geometri stop mustahil)."""
import asyncio
import httpx
from config import CONFIG


def _sma(a, n): return sum(a[-n:]) / n if len(a) >= n else None


def _ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    e = sum(data[:period]) / period
    for p in data[period:]:
        e = p * k + e * (1 - k)
    return e


def _adx(highs, lows, closes, period=14):
    """Wilder ADX ringkas -> kekuatan trend (float) atau None."""
    n = len(closes)
    if n < period * 2 + 1:
        return None
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    atr = sum(tr[:period]); sp = sum(plus_dm[:period]); sm = sum(minus_dm[:period])
    dxs = []
    for i in range(period, len(tr)):
        atr = atr - atr / period + tr[i]
        sp = sp - sp / period + plus_dm[i]
        sm = sm - sm / period + minus_dm[i]
        if atr <= 0:
            continue
        pdi = 100 * sp / atr; mdi = 100 * sm / atr
        d = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / d if d > 0 else 0.0)
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def _htf_dir(closes):
    """Arah trend 1H: 'up'/'down'/'mixed' dari EMA50 vs EMA200 + slope EMA50."""
    if len(closes) < 60:
        return "mixed"
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200) if len(closes) >= 200 else _ema(closes, min(len(closes) - 1, 100))
    prev = _ema(closes[:-10], 50) if len(closes) > 60 else ema50
    slope = ((ema50 - prev) / prev * 100) if (ema50 and prev) else 0.0
    last = closes[-1]
    sep = ((ema50 - ema200) / ema200 * 100) if (ema50 and ema200) else 0.0
    SEP_MIN, SLOPE_MIN = 0.10, 0.02  # #7 anti-noise: pasar datar bukan trend
    if ema50 and ema200:
        if last > ema50 and sep > SEP_MIN and slope > SLOPE_MIN:
            return "up"
        if last < ema50 and sep < -SEP_MIN and slope < -SLOPE_MIN:
            return "down"
    return "mixed"


def score_symbol(closes, highs, lows, vols, htf_dir="mixed"):
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
    adx = _adx(highs, lows, closes, 14)
    if adx is not None and adx < CONFIG.adx_min:
        return None  # #7: ADX rendah = tanpa trend (chop) -> sumber noise -> buang
    mom = (last - closes[-24]) / closes[-24] * 100  # ~6 jam @15m
    v_now = sum(vols[-24:]); v_prev = sum(vols[-48:-24]) or 1
    vol_x = v_now / v_prev
    up = last > s20 > s50
    down = last < s20 < s50
    if not (up or down):
        return None  # bukan trending -> bukan kandidat
    direction = "long" if up else "short"
    # #6: TREND 1H WAJIB SEARAH — kandidat melawan trend 1H dibuang total
    if CONFIG.htf_align_required and htf_dir in ("up", "down"):
        if (direction == "long" and htf_dir != "up") or (direction == "short" and htf_dir != "down"):
            return None
    score = 2.0 + min(abs(mom) / 2, 1.5) + min(max(vol_x - 1, 0), 1.0)
    if adx is not None:
        score += min(max((adx - CONFIG.adx_min) / 20, 0), 1.0)  # bonus kekuatan trend
    if htf_dir in ("up", "down") and (
            (direction == "long" and htf_dir == "up") or (direction == "short" and htf_dir == "down")):
        score += 0.5  # bonus searah trend 1H
    return {"direction": direction, "score": round(score, 2), "mom_pct": round(mom, 2),
            "vol_x": round(vol_x, 2), "atr_pct": round(atr_pct, 3), "last": last,
            "adx": adx, "htf_dir": htf_dir}


async def _klines(c, sym, interval, limit):
    try:
        r = await c.get(f"{CONFIG.binance_data_base}/fapi/v1/klines",
                        params={"symbol": sym, "interval": interval, "limit": limit}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def _pair(c, sym):
    """Ambil klines entry-TF (15m) + trend-TF (1H) untuk satu simbol."""
    entry_kl = await _klines(c, sym, CONFIG.entry_interval, 100)
    htf_kl = await _klines(c, sym, CONFIG.trend_interval, 300)
    return sym, entry_kl, htf_kl


def _closes_hlc(kl):
    good = [k for k in kl if isinstance(k, (list, tuple)) and len(k) >= 6]
    return ([float(k[4]) for k in good], [float(k[2]) for k in good],
            [float(k[3]) for k in good], [float(k[5]) for k in good])


async def screen(symbols):
    """-> list kandidat terurut skor desc, maks SCREENER_TOP_N. Sudah searah trend 1H."""
    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as c:
        rows = await asyncio.gather(*[_pair(c, s) for s in symbols])
    out = []
    for sym, kl, htf_kl in rows:
        if not isinstance(kl, list) or not kl:
            continue  # respons error (dict) / kosong -> bukan kandidat, bukan crash
        try:
            closes, highs, lows, vols = _closes_hlc(kl)
            htf_dir = "mixed"
            if isinstance(htf_kl, list) and htf_kl:
                hc, _hh, _hl, _hv = _closes_hlc(htf_kl)
                htf_dir = _htf_dir(hc)
            sc = score_symbol(closes, highs, lows, vols, htf_dir)
        except Exception:
            continue
        if sc:
            out.append({"symbol": sym, **sc})
    out.sort(key=lambda x: -x["score"])
    return out[:CONFIG.screener_top_n]
