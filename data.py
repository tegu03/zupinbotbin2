"""Market-data v4.1: Binance mainnet + orderbook + technical indicators.
Perubahan dari v4:
  - TAMBAH: orderbook depth (top 20 bid/ask) → layer orderbook AI sekarang punya data
  - TAMBAH: RSI-14 → momentum oscillator
  - TAMBAH: EMA-12 / EMA-26 → trend strength (MACD-like)
  - TAMBAH: Bollinger Bands (20, 2σ) → volatility + mean reversion
  - TAMBAH: support/resistance levels dari recent swing highs/lows
  - TAMBAH: ATR-14 → volatility measure untuk stop distance reference
v5.1: hardening anti-NoneType — semua respons API dinormalisasi (_as_list);
respons error Binance berbentuk dict tidak lagi membuat crash siklus.

Endpoint Binance (semua publik mainnet, tanpa API key):
  - /fapi/v1/klines       OHLCV
  - /fapi/v1/premiumIndex  mark price + lastFundingRate
  - /fapi/v1/depth         orderbook depth
  - /futures/data/openInterestHist
  - /futures/data/globalLongShortAccountRatio
  - /futures/data/takerlongshortRatio
  - alternative.me         Fear & Greed
"""
import time
import datetime
import httpx
from config import CONFIG

FNG = "https://api.alternative.me/fng/?limit=1"
_HEADERS = {"User-Agent": "Mozilla/5.0 (zupin-bot)"}
_PERIOD_OK = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
_RES_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
            "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def _ema(data, period):
    """Exponential Moving Average."""
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(closes, period=14):
    """Relative Strength Index."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-(period):]
    gains = [d if d > 0 else 0 for d in recent]
    losses = [-d if d < 0 else 0 for d in recent]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _atr(highs, lows, closes, period=14):
    """Average True Range."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h, l, cp = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - cp), abs(l - cp))
        trs.append(tr)
    return sum(trs[-period:]) / period if len(trs) >= period else None


def _adx(highs, lows, closes, period=14):
    """Wilder's ADX + directional index (+DI/-DI). v6: pengukur KEKUATAN trend.
    ADX rendah (<20) = pasar tanpa trend (chop/range) -> saring noise entry.
    Return (adx, plus_di, minus_di) atau (None, None, None) jika data kurang."""
    n = len(closes)
    if n < period * 2 + 1:
        return None, None, None
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    # Wilder smoothing awal
    atr = sum(tr[:period])
    sp = sum(plus_dm[:period])
    sm = sum(minus_dm[:period])
    dxs = []
    for i in range(period, len(tr)):
        atr = atr - atr / period + tr[i]
        sp = sp - sp / period + plus_dm[i]
        sm = sm - sm / period + minus_dm[i]
        if atr <= 0:
            continue
        pdi = 100 * sp / atr
        mdi = 100 * sm / atr
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    if len(dxs) < period:
        return None, None, None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    last_pdi = 100 * sp / atr if atr > 0 else None
    last_mdi = 100 * sm / atr if atr > 0 else None
    return (round(adx, 2),
            round(last_pdi, 2) if last_pdi is not None else None,
            round(last_mdi, 2) if last_mdi is not None else None)


def _htf_trend(closes):
    """v6: klasifikasi trend timeframe tinggi (1H) — deterministik, tanpa AI.
    Pakai EMA50 vs EMA200 + kemiringan EMA50. up/down/mixed."""
    if len(closes) < 60:
        return {"trend": "mixed", "ema50": None, "ema200": None, "slope_pct": None}
    ema50 = _ema(closes, 50)
    ema200 = _ema(closes, 200) if len(closes) >= 200 else _ema(closes, min(len(closes) - 1, 100))
    # kemiringan EMA50 atas ~10 bar
    prev = _ema(closes[:-10], 50) if len(closes) > 60 else ema50
    slope = ((ema50 - prev) / prev * 100) if (ema50 and prev) else 0.0
    last = closes[-1]
    # #7 ANTI-NOISE: pasar datar (EMA50~EMA200, slope~0) TIDAK boleh dilabel trending.
    # Butuh pemisahan EMA & kemiringan minimal -> jauh dari sekadar noise floating point.
    sep = ((ema50 - ema200) / ema200 * 100) if (ema50 and ema200) else 0.0
    SEP_MIN, SLOPE_MIN = 0.10, 0.02
    trend = "mixed"
    if ema50 and ema200:
        if last > ema50 and sep > SEP_MIN and slope > SLOPE_MIN:
            trend = "up"
        elif last < ema50 and sep < -SEP_MIN and slope < -SLOPE_MIN:
            trend = "down"
    return {"trend": trend,
            "ema50": round(ema50, 6) if ema50 else None,
            "ema200": round(ema200, 6) if ema200 else None,
            "slope_pct": round(slope, 3)}


def _bollinger(closes, period=20, std_mult=2):
    """Bollinger Bands: upper, middle, lower."""
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return round(mid + std_mult * std, 2), round(mid, 2), round(mid - std_mult * std, 2)


def _swing_levels(highs, lows, lookback=5):
    """Find recent swing high/low levels for support/resistance."""
    supports, resistances = [], []
    if len(highs) < lookback * 2 + 1:
        return supports, resistances
    for i in range(lookback, len(highs) - lookback):
        # swing high: higher than lookback bars on both sides
        if highs[i] == max(highs[i - lookback:i + lookback + 1]):
            resistances.append(round(highs[i], 2))
        # swing low: lower than lookback bars on both sides
        if lows[i] == min(lows[i - lookback:i + lookback + 1]):
            supports.append(round(lows[i], 2))
    # Return most recent 5 levels, deduplicated
    supports = sorted(set(supports))[-5:]
    resistances = sorted(set(resistances))[-5:]
    return supports, resistances


async def _try(client, url, params=None):
    try:
        r = await client.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def collect_market_data(symbol=None):
    base = CONFIG.binance_data_base.rstrip("/")
    itv = CONFIG.entry_interval if CONFIG.entry_interval in _RES_SEC else "15m"
    htf = CONFIG.trend_interval if CONFIG.trend_interval in _RES_SEC else "1h"
    period = itv if itv in _PERIOD_OK else "1h"
    sym = symbol or CONFIG.symbol
    async with httpx.AsyncClient(headers=_HEADERS) as c:
        klines = await _try(c, f"{base}/fapi/v1/klines",
                            {"symbol": sym, "interval": itv, "limit": 200})
        # v6: klines timeframe tinggi (trend) — konfirmasi arah supaya tidak entry melawan trend
        klines_htf = await _try(c, f"{base}/fapi/v1/klines",
                                {"symbol": sym, "interval": htf, "limit": 300})
        prem = await _try(c, f"{base}/fapi/v1/premiumIndex", {"symbol": sym})
        oi = await _try(c, f"{base}/futures/data/openInterestHist",
                        {"symbol": sym, "period": period, "limit": 48})
        ls = await _try(c, f"{base}/futures/data/globalLongShortAccountRatio",
                        {"symbol": sym, "period": period, "limit": 24})
        taker = await _try(c, f"{base}/futures/data/takerlongshortRatio",
                           {"symbol": sym, "period": period, "limit": 24})
        # NEW: orderbook depth
        book = await _try(c, f"{base}/fapi/v1/depth",
                          {"symbol": sym, "limit": 20})
        fng = await _try(c, FNG)
    return {"klines": klines, "klines_htf": klines_htf, "prem": prem, "oi": oi, "ls": ls,
            "taker": taker, "book": book, "fng": fng}


def _as_list(x):
    """Binance kadang mengembalikan dict error ({"code":..,"msg":..}) alih-alih list.
    Normalisasi ke list agar downstream tidak crash NoneType/KeyError."""
    return x if isinstance(x, list) else []


def build_snapshot(raw, account, symbol=None):
    raw = raw or {}
    sym_t = symbol or CONFIG.symbol
    gaps = []

    # ---- klines: list of [openTime, o, h, l, c, v, ...] ----
    kl = _as_list(raw.get("klines"))
    closes = [v for v in (_num(k[4]) for k in kl if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    highs = [v for v in (_num(k[2]) for k in kl if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    lows = [v for v in (_num(k[3]) for k in kl if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    vols = [v for v in (_num(k[5]) for k in kl if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    if not closes:
        gaps.append("binance_klines")

    last = closes[-1] if closes else None
    per_day = max(1, int(86400 / _RES_SEC.get(CONFIG.entry_interval, 3600)))
    n24 = min(per_day, len(closes)) if closes else 0
    h24 = max(highs[-n24:]) if n24 and highs else None
    l24 = min(lows[-n24:]) if n24 and lows else None
    c24 = closes[-n24 - 1] if len(closes) > n24 else (closes[0] if closes else None)
    chg24 = ((last - c24) / c24 * 100) if (last is not None and c24) else None
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    rng = ((last - l24) / (h24 - l24) * 100) if (last is not None and h24 and l24 and h24 > l24) else None
    trend = "mixed"
    if last is not None and sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "up"
        elif last < sma20 < sma50:
            trend = "down"
    vol_now = sum(vols[-n24:]) if n24 and vols else None
    vol_prev = sum(vols[-2 * n24:-n24]) if vols and len(vols) >= 2 * n24 else None
    volchg = ((vol_now - vol_prev) / vol_prev * 100) if (vol_now is not None and vol_prev) else None

    # ---- NEW: Technical Indicators ----
    rsi_14 = _rsi(closes, 14)
    ema_12 = _ema(closes, 12)
    ema_26 = _ema(closes, 26)
    macd_line = round(ema_12 - ema_26, 2) if (ema_12 is not None and ema_26 is not None) else None
    atr_14 = _atr(highs, lows, closes, 14)
    bb_upper, bb_mid, bb_lower = _bollinger(closes, 20, 2)
    # v6: ADX entry-TF (kekuatan trend, saring chop)
    adx_14, plus_di, minus_di = _adx(highs, lows, closes, 14)

    # ---- v6: HIGHER-TIMEFRAME TREND (1H) ----
    kl_htf = _as_list(raw.get("klines_htf"))
    closes_htf = [v for v in (_num(k[4]) for k in kl_htf if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    highs_htf = [v for v in (_num(k[2]) for k in kl_htf if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    lows_htf = [v for v in (_num(k[3]) for k in kl_htf if isinstance(k, (list, tuple)) and len(k) > 5) if v is not None]
    htf = _htf_trend(closes_htf)
    htf_adx, _hp, _hm = _adx(highs_htf, lows_htf, closes_htf, 14)
    htf["adx"] = htf_adx
    htf["interval"] = CONFIG.trend_interval
    if not closes_htf:
        gaps.append("htf_klines")

    # ---- NEW: Support/Resistance levels ----
    supports, resistances = _swing_levels(highs, lows, 5)

    # ---- premiumIndex: mark + funding riil ----
    prem = raw.get("prem") if isinstance(raw.get("prem"), dict) else {}
    frate = _num(prem.get("lastFundingRate"))
    mark = _num(prem.get("markPrice"))
    if frate is None:
        gaps.append("funding_rate")

    # ---- open interest hist ----
    oi_list = [x for x in _as_list(raw.get("oi")) if isinstance(x, dict)]
    oi_last = _num(oi_list[-1].get("sumOpenInterest")) if oi_list else None
    oi_first = _num(oi_list[0].get("sumOpenInterest")) if oi_list else None
    oichg = ((oi_last - oi_first) / oi_first * 100) if (oi_last is not None and oi_first) else None
    if not oi_list:
        gaps.append("open_interest")

    # ---- global long/short account ratio ----
    ls_list = [x for x in _as_list(raw.get("ls")) if isinstance(x, dict)]
    ls_last = ls_list[-1] if ls_list else {}
    long_pct = _num(ls_last.get("longAccount"))
    ls_ratio = _num(ls_last.get("longShortRatio"))
    if not ls_list:
        gaps.append("long_short_ratio")

    # ---- taker buy/sell ratio ----
    tk_list = [x for x in _as_list(raw.get("taker")) if isinstance(x, dict)]
    taker_ratio = _num(tk_list[-1].get("buySellRatio")) if tk_list else None
    if taker_ratio is None:
        gaps.append("taker_ratio")

    # ---- NEW: orderbook depth ----
    book_raw = raw.get("book") if isinstance(raw.get("book"), dict) else {}
    bids = [b for b in _as_list(book_raw.get("bids")) if isinstance(b, (list, tuple)) and len(b) >= 2]
    asks = [a for a in _as_list(book_raw.get("asks")) if isinstance(a, (list, tuple)) and len(a) >= 2]
    bid_depth = sum(_num(b[1]) or 0 for b in bids[:10]) if bids else None
    ask_depth = sum(_num(a[1]) or 0 for a in asks[:10]) if asks else None
    bid_ask_ratio = round(bid_depth / ask_depth, 3) if (bid_depth and ask_depth and ask_depth > 0) else None
    best_bid = _num(bids[0][0]) if bids else None
    best_ask = _num(asks[0][0]) if asks else None
    spread_pct = round((best_ask - best_bid) / best_bid * 100, 4) if (best_bid and best_ask) else None
    if not bids:
        gaps.append("orderbook")

    fng_raw = raw.get("fng") if isinstance(raw.get("fng"), dict) else {}
    fng = next((x for x in _as_list(fng_raw.get("data")) if isinstance(x, dict)), {})
    if _num(fng.get("value")) is None:
        gaps.append("fear_greed")

    venue = "MAINNET" if "fapi.binance.com" in CONFIG.binance_base else "TESTNET/DEMO"
    return {
        "symbol": f"{sym_t} Perp (Binance)", "symbol_trade": sym_t,
        "execution_venue": venue,
        "interval": CONFIG.entry_interval,
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "data_sources": {"all_market_data": "binance_mainnet_public (crowd riil)"},
        "data_gaps": gaps,
        "price": {
            "last": last, "mark": mark, "change_24h_pct": chg24, "high_24h": h24, "low_24h": l24,
            "range_pos_pct": rng, "sma20": sma20, "sma50": sma50, "trend": trend,
            "volume_24h_base": vol_now, "volume_change_pct": volchg,
        },
        "technicals": {
            "rsi_14": rsi_14,
            "ema_12": round(ema_12, 2) if ema_12 else None,
            "ema_26": round(ema_26, 2) if ema_26 else None,
            "macd_line": macd_line,
            "atr_14": round(atr_14, 2) if atr_14 else None,
            "adx_14": adx_14, "plus_di": plus_di, "minus_di": minus_di,
            "bollinger": {"upper": bb_upper, "mid": bb_mid, "lower": bb_lower},
            "support_levels": supports,
            "resistance_levels": resistances,
        },
        # v6: trend timeframe tinggi (1H) untuk gerbang anti-counter-trend
        "htf_trend": htf,
        "orderbook": {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_depth_top10": round(bid_depth, 4) if bid_depth else None,
            "ask_depth_top10": round(ask_depth, 4) if ask_depth else None,
            "bid_ask_ratio": bid_ask_ratio,
        },
        "funding": {"rate": frate,
                    "rate_pct_8h": (frate * 100) if frate is not None else None,
                    "annualized_pct": (frate * 3 * 365 * 100) if frate is not None else None},
        "open_interest": {"current_base": oi_last, "change_window_pct": oichg},
        "long_short": {"account_long_pct": long_pct, "account_ratio": ls_ratio,
                       "taker_buy_sell_ratio": taker_ratio},
        "sentiment": {"fear_greed": _num(fng.get("value")), "label": fng.get("value_classification")},
        # v6 FIX #1: jangan pernah simpan None -> downstream .get("account").get(...) aman
        "account": account or {},
    }
