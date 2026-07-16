"""DETERMINISTIC Risk Governor v5.1 (Binance). AI mengusulkan; kode ini memutuskan.

Gerbang (SEMUA harus lolos):
  1. Sinyal actionable (long/short)
  2. Regime alignment: trending_up->long, trending_down->short,
     ranging->long/short (mean reversion diizinkan), chop->NO-TRADE
  3. Confidence >= MIN_CONFIDENCE (+ penalti histori per simbol dari lessons)
  4. Geometry valid + R:R >= MIN_RR (2.0)
  5. Stop >= max(MIN_STOP_PCT, ATR_STOP_MULT x ATR%) -- v5.1: floor statis terbukti
     terlalu sempit utk alt 15m; stop di dalam noise ATR = SL kena wick beruntun
  6. Sizing dari STOP (fixed fractional); notional <= max_leverage x equity
  7. NOTIONAL >= minimum Binance (live dari exchangeInfo)
  8. Kill switch: daily <= -3% -> kill_switch=True
  9. Profit lock: daily >= +target -> profit_lock=True
Plus: estimasi fee round-trip (worst case 2x taker) dilaporkan; fee yang memakan
>25% risk diberi peringatan eksplisit -- di akun kecil, fee ADALAH strategi.
"""
from config import CONFIG


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def evaluate(pte, mse, snapshot):
    cfg = CONFIG
    # v6 FIX #1: snapshot["account"] bisa bernilai None (key ADA tapi value None) — pola
    # .get("account", {}) HANYA memberi default saat key HILANG, bukan saat value None ->
    # 'NoneType' object has no attribute 'get'. Pakai `or {}` untuk menutup kedua kasus.
    snapshot = snapshot or {}
    pte = pte if isinstance(pte, dict) else {}
    mse = mse if isinstance(mse, dict) else {}
    acc = snapshot.get("account") or {}
    equity = _num(acc.get("equity_usd")) or cfg.initial_capital
    reasons, approved = [], True
    kill_switch = False
    profit_lock = False

    signal = pte.get("signal")
    if signal not in ("long", "short"):
        approved = False
        reasons.append(f"Signal not actionable: {signal}")

    regime = mse.get("pte_layer1_input") or pte.get("regime")
    pte_regime = pte.get("regime")
    if pte_regime and regime and pte_regime != regime:
        reasons.append(f"Catatan: regime PTE ({pte_regime}) != MSE ({regime}); dipakai MSE")
    if signal in ("long", "short"):
        if regime == "trending_up" and signal != "long":
            approved = False
            reasons.append("Sinyal SHORT berlawanan regime trending_up -> DITOLAK")
        elif regime == "trending_down" and signal != "short":
            approved = False
            reasons.append("Sinyal LONG berlawanan regime trending_down -> DITOLAK")
        elif regime == "chop":
            approved = False
            reasons.append("Regime chop -> NO-TRADE (market tidak terbaca)")
        elif regime == "ranging":
            reasons.append(f"Regime ranging -> mean reversion DIIZINKAN ({signal})")

    # v6 GERBANG #6: TREND 1H (higher-timeframe) — DILARANG entry melawan trend 1H.
    # Ini gerbang deterministik (bukan bergantung AI): sumber utama SL beruntun adalah
    # entry counter-trend pada pullback yang ternyata reversal.
    htf = snapshot.get("htf_trend") or {}
    htf_dir = str(htf.get("trend") or "mixed")
    if cfg.htf_align_required and signal in ("long", "short") and htf_dir in ("up", "down"):
        if signal == "long" and htf_dir != "up":
            approved = False
            reasons.append(f"LONG melawan trend 1H ({htf_dir}) -> DITOLAK (anti counter-trend)")
        elif signal == "short" and htf_dir != "down":
            approved = False
            reasons.append(f"SHORT melawan trend 1H ({htf_dir}) -> DITOLAK (anti counter-trend)")

    # v6.1 GERBANG #6b: BARRIER EMA200(1H) — celah "mixed" ditutup (kasus VANRY).
    # DILARANG long di bawah EMA200 1H, DILARANG short di atasnya. Deterministik & tegas:
    # tidak peduli 15m lokal terlihat naik, kalau harga di bawah trend besar -> bukan long.
    last_px_htf = _num((snapshot.get("price") or {}).get("last"))
    ema200_htf = _num(htf.get("ema200"))
    if signal in ("long", "short") and last_px_htf and ema200_htf:
        if signal == "long" and last_px_htf < ema200_htf:
            approved = False
            reasons.append(f"LONG DITOLAK: harga di BAWAH EMA200(1H) ({last_px_htf:g} < {ema200_htf:g}) "
                           "-- di bawah trend besar, bukan zona beli (anti pullback-trap)")
        elif signal == "short" and last_px_htf > ema200_htf:
            approved = False
            reasons.append(f"SHORT DITOLAK: harga di ATAS EMA200(1H) ({last_px_htf:g} > {ema200_htf:g}) "
                           "-- di atas trend besar, bukan zona jual")

    # v6 GERBANG #7: ADX entry-TF — buang chop (pasar tanpa trend = noise & whipsaw).
    adx = _num((snapshot.get("technicals") or {}).get("adx_14"))
    if signal in ("long", "short") and adx is not None and adx < cfg.adx_min:
        approved = False
        reasons.append(f"ADX {adx:.1f} < {cfg.adx_min:g} -> pasar chop/tanpa trend (noise) -> NO-TRADE")

    # v5.1 PEMBELAJARAN: simbol dgn rekam jejak buruk (data jurnal riil) butuh bukti lebih kuat
    mem = snapshot.get("memory_adjust") or {}
    sym_trade = snapshot.get("symbol_trade") or CONFIG.symbol
    extra_conf = _num((mem.get("extra_conf") or {}).get(sym_trade)) or 0.0
    min_conf_eff = cfg.min_confidence + extra_conf

    conf = _num(pte.get("confidence_pct"))
    if signal in ("long", "short"):
        if conf is None or conf < min_conf_eff:
            note = f" (dasar {cfg.min_confidence:.0f}% + penalti histori {extra_conf:.0f}%)" if extra_conf else ""
            approved = False
            reasons.append(f"Confidence {conf if conf is not None else 0:.0f}% < minimum {min_conf_eff:.0f}%{note}")

    entry_obj = pte.get("entry") or {}
    entry = _num(entry_obj.get("price"))
    if entry is None:
        zone = entry_obj.get("zone") or [None]
        entry = _num(zone[0])
    stop = _num(pte.get("invalidation"))

    if signal in ("long", "short") and (entry is None or stop is None):
        approved = False
        reasons.append("Missing entry or invalidation")

    # v6 #9: TP LADDER DETERMINISTIK dari geometri risk (R = |entry - stop|).
    # TP1 = entry +/- TP1_RR*R (RR 1:1), TP2 = entry +/- TP2_RR*R (RR 1:2).
    # Target AI TIDAK dipakai untuk ladder -> geometri konsisten, tidak bisa "digelembungkan".
    rr = stop_dist = risk_usd = notional = base_amount = side = fee_est = None
    tp1 = tp2 = None
    if entry is not None and stop is not None and entry > 0:
        risk_dist = abs(entry - stop)
        if signal == "long" and not (stop < entry):
            approved = False
            reasons.append("Long geometry invalid (need stop < entry)")
        if signal == "short" and not (stop > entry):
            approved = False
            reasons.append("Short geometry invalid (need stop > entry)")
        if risk_dist <= 0:
            approved = False
            reasons.append("Risk distance nol (entry == invalidation)")
        else:
            if signal == "long":
                tp1 = entry + cfg.tp1_rr * risk_dist
                tp2 = entry + cfg.tp2_rr * risk_dist
            elif signal == "short":
                tp1 = entry - cfg.tp1_rr * risk_dist
                tp2 = entry - cfg.tp2_rr * risk_dist
        # RR sistem diukur ke TP2 (target maksimum). Dgn ladder ini rr == tp2_rr.
        rr = cfg.tp2_rr
        if rr < cfg.min_rr:
            approved = False
            reasons.append(f"R:R ke TP2 {rr:.2f} < min {cfg.min_rr} (cek TP2_RR/MIN_RR)")

        # v6.1 GERBANG ORDER BLOCK: jangan entry MENEMBUS zona S/R lawan sebelum TP1.
        # Long tepat di bawah resistance (order block) = R:R semu, kemungkinan besar ditolak market.
        # Level S/R kini akurat utk coin murah (fix pembulatan di data.py).
        tech = snapshot.get("technicals") or {}
        res_levels = sorted(r for r in (_num(x) for x in (tech.get("resistance_levels") or [])) if r)
        sup_levels = sorted(s for s in (_num(x) for x in (tech.get("support_levels") or [])) if s)
        if signal == "long" and tp1 is not None:
            wall = next((r for r in res_levels if r > entry), None)
            if wall is not None and wall < tp1:
                approved = False
                reasons.append(f"LONG ditolak: resistance {wall:g} di antara entry & TP1 "
                               f"({entry:g}→{tp1:g}) -- long menembus ORDER BLOCK, R:R semu")
        elif signal == "short" and tp1 is not None:
            floor_lvl = next((s for s in reversed(sup_levels) if s < entry), None)
            if floor_lvl is not None and floor_lvl > tp1:
                approved = False
                reasons.append(f"SHORT ditolak: support {floor_lvl:g} di antara entry & TP1 "
                               f"({entry:g}→{tp1:g}) -- short menembus ORDER BLOCK")
        stop_dist = risk_dist / entry if entry > 0 else 0
        # v5.1 KALIBRASI: floor stop ATR-aware. Floor statis 0.5% terbukti terlalu SEMPIT
        # utk alt 15m (ATR sering >1%) -> stop di dalam noise -> SL kena wick beruntun.
        atr = _num((snapshot.get("technicals") or {}).get("atr_14"))
        last_px = _num((snapshot.get("price") or {}).get("last"))
        atr_pct = (atr / last_px) if (atr and last_px) else None
        min_stop_eff = max(cfg.min_stop_pct, cfg.atr_stop_mult * atr_pct) if atr_pct else cfg.min_stop_pct
        if stop_dist < min_stop_eff:
            approved = False
            src = (f"{cfg.atr_stop_mult:g}xATR ({atr_pct * 100:.2f}%)" if (atr_pct and min_stop_eff > cfg.min_stop_pct)
                   else f"floor statis {cfg.min_stop_pct * 100:.2f}%")
            reasons.append(f"Stop {stop_dist * 100:.3f}% < minimum {min_stop_eff * 100:.3f}% [{src}] -- "
                           "stop di dalam noise = SL kena wick, bukan invalidasi tesis")
        risk_usd = equity * cfg.risk_pct
        notional = risk_usd / stop_dist if stop_dist > 0 else 0
        cap = equity * cfg.max_leverage
        if notional > cap:
            notional = cap
            reasons.append(f"Notional capped at {cfg.max_leverage}x equity")

        # GERBANG MIN-NOTIONAL BINANCE (nilai live menimpa default saat start)
        mn = snapshot.get("min_notional") or getattr(CONFIG, "live_min_notional", None) or cfg.binance_min_notional
        if signal in ("long", "short") and mn and notional < mn:
            approved = False
            need = mn * stop_dist / cfg.risk_pct if stop_dist else 0
            reasons.append(f"Notional ${notional:,.2f} < minimum Binance ${mn:,.0f} -- "
                           f"equity minimal untuk stop {stop_dist * 100:.2f}% @ risk {cfg.risk_pct * 100:g}%: "
                           f"${need:,.0f}")

        base_amount = notional / entry if entry > 0 else 0
        side = "buy" if signal == "long" else "sell"
        fee_est = 2 * cfg.taker_fee_pct * notional  # worst case: taker masuk + taker keluar
        if risk_usd and fee_est > 0.25 * risk_usd:
            reasons.append(f"Peringatan fee: est. round-trip ${fee_est:.2f} = "
                           f"{fee_est / risk_usd * 100:.0f}% dari risk -- perlebar stop / pakai limit (maker)")
    elif signal in ("long", "short"):
        approved = False
        reasons.append("Entry/invalidation tidak valid untuk geometri & sizing")

    ev = str(pte.get("event_risk") or "")
    if ev and any(w in ev.lower() for w in ("high-impact", "imminent", "within hours", "fomc", "cpi", "nfp", "expiry")):
        reasons.append(f"Event risk noted: {ev}")

    dp = _num(acc.get("daily_pnl_pct"))
    if dp is not None and dp <= -(cfg.daily_loss_limit_pct * 100):
        approved = False
        kill_switch = True
        reasons.append(f"KILL SWITCH: daily {dp:.2f}% <= -{cfg.daily_loss_limit_pct * 100:.1f}%")

    if dp is not None and cfg.daily_profit_target_pct > 0 and dp >= cfg.daily_profit_target_pct * 100:
        approved = False
        profit_lock = True
        reasons.append(f"PROFIT LOCK: daily +{dp:.2f}% >= target {cfg.daily_profit_target_pct * 100:.1f}% -> stop entry hari ini")

    if not reasons:
        reasons.append("All gates passed")

    return {
        "approved": bool(approved and signal in ("long", "short")),
        "kill_switch": kill_switch,
        "profit_lock": profit_lock,
        "symbol": snapshot.get("symbol_trade") or CONFIG.symbol,
        "signal": signal,
        "side": side,
        "regime": regime,
        "confidence_pct": pte.get("confidence_pct"),
        "entry": entry,
        "stop": stop,
        "tp1": tp1,                       # RR 1:1 — ditutup sebagian (tp1_close_frac)
        "tp2": tp2,                       # RR 1:2 — target maksimum sisa posisi
        "tp1_rr": cfg.tp1_rr,
        "tp2_rr": cfg.tp2_rr,
        "tp1_close_frac": cfg.tp1_close_frac,
        "move_sl_to_be": cfg.move_sl_to_be,
        "htf_trend": htf_dir,
        "adx": adx,
        "entry_type": ("market" if cfg.force_market_entry else (entry_obj.get("type") or "limit")),
        "rr": round(rr, 2) if rr is not None else None,
        "stop_distance_pct": round(stop_dist * 100, 3) if stop_dist is not None else None,
        "risk_usd": round(risk_usd, 2) if risk_usd is not None else None,
        "notional_usd": round(notional, 2) if notional is not None else None,
        "base_amount": round(base_amount, 6) if base_amount is not None else None,
        "fee_est_usd": round(fee_est, 4) if fee_est is not None else None,
        "equity_usd": round(equity, 2),
        "dry_run": cfg.dry_run,
        "reasons": reasons,
        "abstain_reason": pte.get("abstain_reason") or "",
        "flip_if": pte.get("flip_if") or "",
        "counter_thesis": pte.get("counter_thesis") or "",
        "funding_note": pte.get("funding_note") or "",
    }
