"""Entry point v5 MULTI-COIN. Siklus:
account -> journal snapshot -> reconcile native -> guardian -> latch -> sweep flat-aware
-> hitung posisi terbuka (>= MAX_CONCURRENT? berhenti) -> SCREENER (tanpa AI)
-> MSE+PTE hanya kandidat teratas -> pilih approved terbaik (conf x RR) -> execute 1."""
import time, calendar, asyncio, contextlib, logging
from config import CONFIG
from data import collect_market_data, build_snapshot
from llm import classify_regime, analyze_trade
from risk import evaluate
from screener import screen
from exchange import Exchange, kill_latched, latch_kill, profit_latched, latch_profit
from notify import (send, format_trade, format_notrade, format_guardian, format_online,
                    format_kill_switch, format_sleep, format_resume, format_profit_lock,
                    format_stale_cancel)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pte-bot")


def _seconds_until_resume():
    now = int(time.time()); tm = time.gmtime(now)
    today = calendar.timegm((tm.tm_year, tm.tm_mon, tm.tm_mday, CONFIG.resume_hour, 0, 0))
    return max(60, (today if now < today else today + 86400) - now)

def _resume_str():
    h = CONFIG.resume_hour % 24
    return f"{(h + 7) % 24:02d}:00 WIB ({h:02d}:00 UTC)"


async def kill_flow(ex, account):
    latch_kill(float(account.get("daily_pnl_pct") or 0.0))
    res = await ex.close_all_positions(account)
    await send(format_kill_switch(account, res, _resume_str()))
    if res.get("flat") is False:
        await send("⚠️ <b>Flatten TIDAK terkonfirmasi — CEK MANUAL.</b>"); return
    secs = _seconds_until_resume()
    await send(format_sleep(_resume_str(), secs))
    await asyncio.sleep(secs)
    await send(format_resume(await ex.get_account()))


async def run_cycle(ex):
    account = await ex.get_account()
    try:
        import journal, reconcile
        journal.record_snapshot(equity=account.get("equity_usd"),
                                initial_capital=account.get("base_capital_usd"),
                                unrealized=account.get("unrealized_pnl_usd"),
                                daily_pnl=account.get("realized_pnl_today_usd"))
        for _sym in CONFIG.symbols:
            await reconcile.reconcile_native_closes(ex.c, _sym)
    except Exception:
        pass

    guard = await ex.ensure_protection(account)
    if guard:
        await send(format_guardian(guard))

    dp = float(account.get("daily_pnl_pct") or 0.0)
    if not CONFIG.dry_run:
        if kill_latched(): return
        if dp <= -CONFIG.daily_loss_limit_pct * 100:
            await kill_flow(ex, account); return
        if profit_latched(): return
        if CONFIG.daily_profit_target_pct > 0 and dp >= CONFIG.daily_profit_target_pct * 100:
            latch_profit(dp); await send(format_profit_lock(account)); return

    positions = ex.open_positions(account)
    open_syms = {p.get("market") for p in positions}

    # sweep flat-aware per simbol (SL/TP yatim + limit basi)
    if CONFIG.cancel_stale_entries and not CONFIG.dry_run and CONFIG.binance_api_key:
        swept = 0
        for sym in CONFIG.symbols:
            if sym in open_syms:
                await ex.cancel_entry_orders(sym)
            else:
                orders = []
                with contextlib.suppress(Exception):
                    orders = await ex.open_orders(sym)
                if orders:
                    await ex.sweep_all_orders(sym); swept += len(orders)
        if swept:
            await send(format_stale_cancel(swept))

    if len(positions) >= CONFIG.max_concurrent_positions and not CONFIG.dry_run:
        log.info("posisi terbuka %d >= max %d -> tanpa entry baru",
                 len(positions), CONFIG.max_concurrent_positions)
        return

    # TAHAP 1: screener tanpa AI
    cands = await screen([s for s in CONFIG.symbols if s not in open_syms])
    log.info("screener: %s", [(c['symbol'], c['direction'], c['score']) for c in cands])
    if not cands:
        if CONFIG.notify_every_cycle:
            await send("⏸️ <b>NO-TRADE</b> · screener: tidak ada simbol trending "
                       f"({len(CONFIG.symbols) - len(open_syms)} dipindai, 0 kandidat)")
        return

    # TAHAP 2: AI hanya untuk kandidat
    best = None
    for c in cands:
        sym = c["symbol"]
        raw = await collect_market_data(sym)
        snap = build_snapshot(raw, account, sym)
        snap["min_notional"] = (ex.filters.get(sym) or {}).get("min_notional")
        snap["screener"] = c
        mse = await classify_regime(snap)
        pte = await analyze_trade(snap, mse)
        d = evaluate(pte, mse, snap)
        log.info("%s: regime=%s signal=%s conf=%s approved=%s | %s", sym, d.get("regime"),
                 d.get("signal"), d.get("confidence_pct"), d["approved"], d["reasons"][0])
        if d["approved"]:
            score = (float(d.get("confidence_pct") or 0)) * (float(d.get("rr") or 0))
            if best is None or score > best[0]:
                best = (score, d)
        if not CONFIG.dry_run and d.get("kill_switch"):
            await kill_flow(ex, account); return

    if best is None:
        if CONFIG.notify_every_cycle:
            await send(f"⏸️ <b>NO-TRADE</b> · {len(cands)} kandidat screener, 0 lolos gerbang AI")
        return

    decision = best[1]
    result = await ex.execute(decision)
    log.info("execute %s -> %s", decision["symbol"], result)
    await send(format_trade(decision, account, result))
    fresh = await ex.get_account()
    g2 = await ex.ensure_protection(fresh)
    if g2:
        await send(format_guardian(g2, phase="pasca-entry"))


async def main():
    ex = Exchange()
    await ex.start()
    log.info("BOT v5 MULTI-COIN | %d simbol | max %d posisi | dry_run=%s",
             len(CONFIG.symbols), CONFIG.max_concurrent_positions, CONFIG.dry_run)
    await send(format_online())
    await send(f"🧭 <b>Multi-coin aktif</b>: {', '.join(CONFIG.symbols)}\n"
               f"• Screener top-{CONFIG.screener_top_n} → AI → 1 entry/siklus · "
               f"maks {CONFIG.max_concurrent_positions} posisi (risiko agregat ≤"
               f"{CONFIG.max_concurrent_positions * CONFIG.risk_pct * 100:g}%)")
    if not CONFIG.dry_run and kill_latched():
        await asyncio.sleep(_seconds_until_resume())
    try:
        while True:
            try:
                await run_cycle(ex)
            except Exception as e:
                log.exception("cycle error")
                with contextlib.suppress(Exception):
                    await send(f"Zupin Bot ERROR: {type(e).__name__}: {e}")
            await asyncio.sleep(CONFIG.loop_minutes * 60)
    finally:
        await ex.close()

if __name__ == "__main__":
    asyncio.run(main())
