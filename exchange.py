"""Exchange v5 MULTI-COIN (Binance USDT-M). Semua mekanisme v4.3 dipertahankan,
di-thread per-simbol: filter live, proteksi (native->reduceOnly->sintetis),
guardian idempoten, fill-watcher, kill-flatten. Journal hook native di _synth_notify."""
import time, json, math, asyncio, contextlib, logging
from config import CONFIG
from binance_client import BinanceClient, BinanceError

log = logging.getLogger("pte-bot.exchange")

def _today(): return time.strftime("%Y-%m-%d", time.gmtime())
def _load_state():
    try:
        with open(CONFIG.state_file) as f: return json.load(f)
    except Exception: return {}
def _save_state(s):
    with contextlib.suppress(Exception):
        with open(CONFIG.state_file, "w") as f: json.dump(s, f)
def _daily_baseline(equity):
    s = _load_state()
    if s.get("date") != _today():
        s = {"date": _today(), "baseline_equity": equity}; _save_state(s)
    return float(s.get("baseline_equity", equity))
def kill_latched(): return bool(_load_state().get("killed_on") == _today())
def latch_kill(p):
    s = _load_state(); s["killed_on"] = _today(); s["killed_at_pnl_pct"] = p; _save_state(s)
def profit_latched(): return bool(_load_state().get("profit_on") == _today())
def latch_profit(p):
    s = _load_state(); s["profit_on"] = _today(); s["profit_at_pnl_pct"] = p; _save_state(s)


class Exchange:
    def __init__(self):
        self.c = None
        self._watch_tasks = {}
        self._synth = {}
        self._synth_tasks = {}
        self.filters = {}

    async def start(self):
        self.c = BinanceClient(); await self.c.start()
        info = await self.c.get("/fapi/v1/exchangeInfo")
        by = {s.get("symbol"): s for s in (info.get("symbols") or [])}
        missing = [s for s in CONFIG.symbols if s not in by]
        if missing: raise RuntimeError(f"Simbol tidak ada di venue: {missing}")
        for sym in CONFIG.symbols:
            f = {"tick": None, "step": None, "min_qty": None, "min_notional": None}
            for flt in by[sym].get("filters", []):
                ft = flt.get("filterType")
                if ft == "PRICE_FILTER": f["tick"] = float(flt.get("tickSize"))
                elif ft == "LOT_SIZE": f["step"] = float(flt.get("stepSize")); f["min_qty"] = float(flt.get("minQty"))
                elif ft in ("MIN_NOTIONAL", "NOTIONAL"): f["min_notional"] = float(flt.get("notional") or flt.get("minNotional") or 0)
            if not (f["tick"] and f["step"]): raise RuntimeError(f"filter {sym} tak terbaca")
            self.filters[sym] = f
            log.info("filters %s: %s", sym, f)
        if CONFIG.binance_api_key and not CONFIG.dry_run:
            dual = await self.c.sget("/fapi/v1/positionSide/dual")
            if str(dual.get("dualSidePosition")).lower() == "true":
                raise RuntimeError("Akun HEDGE mode. Ubah ke One-way lalu restart.")
            for sym in CONFIG.symbols:
                with contextlib.suppress(Exception):
                    await self.c.spost("/fapi/v1/leverage", symbol=sym, leverage=int(CONFIG.max_leverage))

    async def close(self):
        for t in list(self._watch_tasks.values()) + list(self._synth_tasks.values()):
            if t and not t.done(): t.cancel()
        if self.c: await self.c.close()

    @staticmethod
    def _dec(step):
        s = f"{step:.10f}".rstrip("0"); return len(s.split(".")[1]) if "." in s else 0
    def fmt_price(self, p, sym):
        t = self.filters[sym]["tick"]; v = math.floor(float(p) / t + 1e-9) * t
        return f"{v:.{self._dec(t)}f}"
    def fmt_qty(self, q, sym):
        st = self.filters[sym]["step"]; v = math.floor(float(q) / st + 1e-9) * st
        return f"{v:.{self._dec(st)}f}", v

    async def get_account(self):
        fb = {"base_capital_usd": CONFIG.initial_capital, "equity_usd": CONFIG.initial_capital,
              "available_usd": CONFIG.initial_capital, "unrealized_pnl_usd": 0.0,
              "realized_pnl_today_usd": 0.0, "daily_pnl_pct": 0.0, "positions": [], "source": "fallback"}
        try:
            acc = await self.c.sget("/fapi/v2/account")
            eq = float(acc.get("totalMarginBalance") or 0); av = float(acc.get("availableBalance") or 0)
            pos, up = [], 0.0
            for p in (acc.get("positions") or []):
                if p.get("symbol") not in CONFIG.symbols: continue
                size = float(p.get("positionAmt") or 0)
                if size == 0: continue
                u = float(p.get("unrealizedProfit") or 0); up += u
                pos.append({"market": p.get("symbol"), "size": size,
                            "entry_price": float(p.get("entryPrice") or 0),
                            "sign": "long" if size > 0 else "short", "unrealized_pnl_usd": u})
            base = _daily_baseline(eq); today = eq - base
            return {"base_capital_usd": CONFIG.initial_capital, "equity_usd": round(eq, 2),
                    "available_usd": round(av, 2), "unrealized_pnl_usd": round(up, 2),
                    "realized_pnl_today_usd": round(today, 2),
                    "daily_pnl_pct": round((today / base * 100) if base else 0.0, 2),
                    "total_pnl_usd": round(eq - CONFIG.initial_capital, 2),
                    "positions": pos, "source": "binance"}
        except Exception as e:
            log.warning("get_account fallback: %s", e); fb["error"] = str(e); return fb

    @staticmethod
    def open_positions(account):
        out = []
        for p in (account or {}).get("positions", []) or []:
            with contextlib.suppress(Exception):
                if abs(float(p.get("size") or 0)) > 0: out.append(p)
        return out
    @staticmethod
    def open_position(account):
        ps = Exchange.open_positions(account); return ps[0] if ps else None
    @staticmethod
    def _position_is_long(pos):
        with contextlib.suppress(Exception): return float(pos.get("size") or 0) > 0
        return str(pos.get("sign", "")).lower() == "long"

    async def open_orders(self, sym): return await self.c.sget("/fapi/v1/openOrders", symbol=sym) or []
    async def order_status(self, oid, sym): return await self.c.sget("/fapi/v1/order", symbol=sym, orderId=oid)
    async def mark_price(self, sym):
        with contextlib.suppress(Exception):
            r = await self.c.get("/fapi/v1/premiumIndex", symbol=sym)
            m = float(r.get("markPrice") or 0); return m or None
        return None
    async def _position_risk(self, sym):
        data = await self.c.sget("/fapi/v2/positionRisk", symbol=sym)
        row = next((r for r in data if r.get("symbol") == sym), None) if isinstance(data, list) else data
        if not row: return 0.0, 0.0, None
        return (float(row.get("positionAmt") or 0), float(row.get("entryPrice") or 0),
                float(row.get("markPrice") or 0) or None)
    async def _wait_position(self, sym):
        dl = time.time() + CONFIG.position_wait_timeout_sec
        amt = entry = 0.0; mark = None
        while time.time() < dl:
            with contextlib.suppress(Exception):
                amt, entry, mark = await self._position_risk(sym)
            if amt != 0 and entry > 0: return amt, entry, (mark or await self.mark_price(sym))
            await asyncio.sleep(CONFIG.position_wait_interval_sec)
        return amt, entry, (mark or await self.mark_price(sym))

    @staticmethod
    def _is_protective(o):
        typ = str(o.get("type") or o.get("origType") or "")
        cp = str(o.get("closePosition")).lower() == "true"
        ro = str(o.get("reduceOnly")).lower() == "true" or o.get("reduceOnly") is True
        return typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT") and (cp or ro)
    @staticmethod
    def _order_kind(o):
        typ = str(o.get("type") or o.get("origType") or "")
        return "sl" if typ in ("STOP_MARKET", "STOP") else ("tp" if typ in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT") else None)
    async def _open_protective_map(self, sym):
        out = {"sl": None, "tp": None}
        for o in await self.open_orders(sym):
            if not self._is_protective(o): continue
            k = self._order_kind(o)
            if k and out.get(k) is None: out[k] = o
        return out
    @staticmethod
    def _breached(kind, is_long, mark, trigger):
        if mark is None: return False
        if kind == "sl": return mark <= trigger if is_long else mark >= trigger
        return mark >= trigger if is_long else mark <= trigger

    async def cancel_entry_orders(self, sym):
        res = []
        for o in await self.open_orders(sym):
            if self._is_protective(o): continue
            with contextlib.suppress(Exception):
                await self.c.sdelete("/fapi/v1/order", symbol=sym, orderId=o.get("orderId"))
                res.append(o.get("orderId"))
        return res
    async def sweep_all_orders(self, sym):
        with contextlib.suppress(Exception):
            await self.c.sdelete("/fapi/v1/allOpenOrders", symbol=sym); return True
        return False

    async def close_position_market(self, sym):
        acc = await self.get_account()
        pos = next((p for p in self.open_positions(acc) if p.get("market") == sym), None)
        if not pos: return {"ok": True, "note": "no position"}
        qty_str, qty = self.fmt_qty(abs(float(pos["size"])), sym)
        if qty <= 0: return {"ok": False, "error": "size rounds to 0"}
        side = "SELL" if self._position_is_long(pos) else "BUY"
        try:
            await self.c.spost("/fapi/v1/order", symbol=sym, side=side, type="MARKET",
                               quantity=qty_str, reduceOnly="true")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- proteksi sintetis per simbol (default testnet; -4120 membuat native mustahil) ----
    def _start_synth(self, sym, sl, tp, close_side):
        self._synth[sym] = {"sl": float(sl), "tp": float(tp), "close_side": close_side,
                            "is_long": close_side == "SELL"}
        t = self._synth_tasks.get(sym)
        if t and not t.done(): t.cancel()
        self._synth_tasks[sym] = asyncio.create_task(self._synth_loop(sym))
    async def _arm_synthetic(self, sym, sl, tp, close_side, reason=""):
        self._start_synth(sym, sl, tp, close_side)
        log.info("PROTECT SYNTH ON %s sl=%s tp=%s (%s)", sym, self.fmt_price(sl, sym), self.fmt_price(tp, sym), reason)
        return {"ok": True, "mode": "synthetic", "reason": reason, "sl": float(sl), "tp": float(tp)}
    async def _synth_loop(self, sym):
        s = self._synth.get(sym)
        if not s: return
        try:
            while True:
                await asyncio.sleep(CONFIG.synth_poll_sec)
                try: amt, _e, mark = await self._position_risk(sym)
                except Exception: continue
                if amt == 0: self._synth.pop(sym, None); return
                if mark is None: continue
                hit_sl = self._breached("sl", s["is_long"], mark, s["sl"])
                hit_tp = self._breached("tp", s["is_long"], mark, s["tp"])
                if hit_sl or hit_tp:
                    which = "SL" if hit_sl else "TP"
                    log.warning("SYNTH %s %s kena mark=%s -> MARKET close", sym, which, mark)
                    r = await self.close_position_market(sym)
                    await self._synth_notify(sym, s, mark, which, r)
                    self._synth.pop(sym, None); return
        except asyncio.CancelledError:
            pass
    async def _synth_notify(self, sym, s, mark, which, r):
        if r.get("ok"):
            try:
                import journal
                journal.record_trade(symbol=sym, outcome=which,
                                     side=("long" if s["is_long"] else "short"),
                                     exit_price=mark, sl=s["sl"], tp=s["tp"], mode="synthetic")
            except Exception:
                pass
        with contextlib.suppress(Exception):
            from notify import send
            if r.get("ok"):
                await send(f"🎯 <b>{which} kena (sintetis) → {sym} DITUTUP MARKET</b>\n"
                           f"• mark ${mark:,.4f} · SL ${s['sl']:,.4f} · TP ${s['tp']:,.4f}")
            else:
                await send(f"⚠️ <b>{which} {sym} kena tapi CLOSE GAGAL — CEK MANUAL</b>\n• {r.get('error')}")

    async def _is_protected(self, sym):
        try:
            pm = await self._open_protective_map(sym)
            if pm.get("sl") and pm.get("tp"): return "native_full"
            if pm.get("sl") or pm.get("tp"): return "native_partial"
        except Exception:
            return "UNVERIFIED_ERR"
        t = self._synth_tasks.get(sym)
        if self._synth.get(sym) and t and not t.done(): return "synthetic"
        return None
    def synth_status(self, sym):
        t = self._synth_tasks.get(sym)
        if self._synth.get(sym) and t and not t.done(): return dict(self._synth[sym])
        return None

    async def _arm_protection(self, sym, sl_t, tp_t, close_side):
        is_long = close_side == "SELL"
        amt, entry, mark = await self._wait_position(sym)
        if amt == 0:
            return {"ok": False, "last_error": "position_not_ready"}
        log.info("PROTECT %s entry=%.6f qty=%s mark=%s SL=%s TP=%s", sym, entry, amt, mark,
                 self.fmt_price(sl_t, sym), self.fmt_price(tp_t, sym))
        if self._breached("sl", is_long, mark, sl_t):
            r = await self.close_position_market(sym)
            return {"ok": bool(r.get("ok")), "closed": True, "reason": "sl_breached_preplace"}
        if self._breached("tp", is_long, mark, tp_t):
            r = await self.close_position_market(sym)
            return {"ok": bool(r.get("ok")), "closed": True, "reason": "tp_breached_preplace"}
        # testnet: langsung sintetis (native -4120 terkonfirmasi); mainnet nanti: native
        return await self._arm_synthetic(sym, sl_t, tp_t, close_side,
                                         "config" if CONFIG.protection_mode == "synthetic" else "auto")

    async def ensure_protection(self, account):
        actions = []
        if not CONFIG.guardian_enabled or CONFIG.dry_run or not CONFIG.binance_api_key:
            return actions
        for pos in self.open_positions(account):
            sym = pos.get("market")
            if sym not in CONFIG.symbols: continue
            status = await self._is_protected(sym)
            if status in ("native_full", "synthetic"): continue
            entry = float(pos.get("entry_price") or 0)
            if entry <= 0:
                actions.append({"market": sym, "status": "NAKED_NO_ENTRY_PRICE"}); continue
            is_long = self._position_is_long(pos)
            sp = CONFIG.guardian_stop_pct
            side = "SELL" if is_long else "BUY"
            sl = entry * (1 - sp) if is_long else entry * (1 + sp)
            tp = entry * (1 + sp * CONFIG.min_rr) if is_long else entry * (1 - sp * CONFIG.min_rr)
            res = await self._arm_protection(sym, sl, tp, side)
            st = ("PROTECTED_SYNTH" if res.get("mode") == "synthetic"
                  else "PROTECTED" if res.get("ok") else
                  "CLOSED_BREACH" if res.get("closed") else "STILL_NAKED")
            actions.append({"market": sym, "placed": "both", "status": st})
        return actions

    async def close_all_positions(self, account=None):
        out = {"canceled": False, "closed": [], "flat": None}
        if CONFIG.dry_run or not CONFIG.binance_api_key:
            out["flat"] = True; return out
        acc = account or await self.get_account()
        for sym in CONFIG.symbols:
            await self.sweep_all_orders(sym)
        out["canceled"] = True
        for pos in self.open_positions(acc):
            r = await self.close_position_market(pos.get("market"))
            out["closed"].append({"market": pos.get("market"), **r})
        with contextlib.suppress(Exception):
            await asyncio.sleep(2)
            out["flat"] = len(self.open_positions(await self.get_account())) == 0
        return out

    def start_fill_watcher(self, decision, order_id):
        sym = decision["symbol"]
        t = self._watch_tasks.get(sym)
        if t and not t.done(): t.cancel()
        self._watch_tasks[sym] = asyncio.create_task(self._watch_fill(dict(decision), order_id))
    async def _watch_fill(self, decision, oid):
        sym = decision["symbol"]
        dl = time.time() + CONFIG.loop_minutes * 60
        close_side = "BUY" if decision["side"] == "sell" else "SELL"
        try:
            while time.time() < dl:
                await asyncio.sleep(CONFIG.watch_poll_sec)
                try:
                    o = await self.order_status(oid, sym)
                    st = str(o.get("status") or "")
                    if st in ("FILLED", "PARTIALLY_FILLED"):
                        prot = await self._arm_protection(sym, decision["stop"], decision["tp1"], close_side)
                        with contextlib.suppress(Exception):
                            from notify import send
                            if prot.get("ok"):
                                await send(f"🛰️ <b>{sym} limit terisi → SL/TP sintetis armed</b> (watcher)\n"
                                           f"• SL ${decision['stop']:,.4f} · TP ${decision['tp1']:,.4f}")
                            else:
                                await send(f"⚠️ <b>{sym} terisi tapi proteksi GAGAL — CEK MANUAL</b>: {prot.get('last_error')}")
                        return
                    if st in ("CANCELED", "EXPIRED", "REJECTED"):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("watcher %s err: %s", sym, e)
        except asyncio.CancelledError:
            pass

    async def execute(self, decision):
        sym = decision.get("symbol") or CONFIG.symbol
        out = {"ok": False, "dry_run": decision["dry_run"], "side": decision["side"],
               "symbol": sym, "protection": None, "warning": None}
        if decision["dry_run"]:
            out.update({"ok": True, "tx_hash": f"DRYRUN-{int(time.time())}"}); return out
        if not CONFIG.binance_api_key:
            out["error"] = "BINANCE_API_KEY belum di-set."; return out
        f = self.filters.get(sym) or {}
        qty_str, qty = self.fmt_qty(decision["base_amount"], sym)
        if qty <= 0 or (f.get("min_qty") and qty < f["min_qty"]):
            out["error"] = f"qty {qty_str} < minQty {f.get('min_qty')}"; return out
        entry = decision["entry"]
        mn = f.get("min_notional") or CONFIG.binance_min_notional
        if mn and qty * entry < mn:
            out["error"] = f"notional ${qty*entry:,.2f} < minNotional ${mn:,.0f}"; return out
        side = "BUY" if decision["side"] == "buy" else "SELL"
        close_side = "SELL" if side == "BUY" else "BUY"
        want = bool(CONFIG.place_sl_tp and decision.get("stop") and decision.get("tp1"))
        try:
            if decision["entry_type"] == "market":
                r = await self.c.spost("/fapi/v1/order", symbol=sym, side=side, type="MARKET", quantity=qty_str)
                out.update({"ok": True, "tx_hash": str(r.get("orderId")), "entry_status": "filled"})
                if want:
                    out["protection"] = await self._arm_protection(sym, decision["stop"], decision["tp1"], close_side)
                return out
            r = await self.c.spost("/fapi/v1/order", symbol=sym, side=side, type="LIMIT",
                                   timeInForce="GTC", price=self.fmt_price(entry, sym), quantity=qty_str)
            oid = r.get("orderId"); out.update({"ok": True, "tx_hash": str(oid)})
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"; return out
        out["entry_status"] = "unknown"
        with contextlib.suppress(Exception):
            await asyncio.sleep(2)
            st = str((await self.order_status(oid, sym)).get("status") or "")
            out["entry_status"] = ("filled" if st == "FILLED" else "partial" if st == "PARTIALLY_FILLED"
                                   else "resting" if st == "NEW" else st.lower() or "unknown")
        if out["entry_status"] in ("filled", "partial"):
            if want:
                out["protection"] = await self._arm_protection(sym, decision["stop"], decision["tp1"], close_side)
        elif want and CONFIG.limit_fill_watcher:
            self.start_fill_watcher(decision, oid)
            out["protection"] = {"deferred": True, "mode": "watcher", "poll_sec": CONFIG.watch_poll_sec}
        return out
