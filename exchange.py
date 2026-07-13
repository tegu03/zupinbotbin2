"""Exchange v5 MULTI-COIN (Binance USDT-M). Semua mekanisme v4.3 dipertahankan,
di-thread per-simbol: filter live, proteksi (native->reduceOnly->sintetis),
guardian idempoten, fill-watcher, kill-flatten.
v6: proteksi NATIVE-first (STOP_MARKET/TAKE_PROFIT_MARKET) dgn fallback SINTETIS; ladder
TP1 50% @1R -> SL sisa ke break-even -> TP2 @2R. Journal hook di _synth_close (1 baris/trade)."""
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
def brake_latched(): return time.time() < float(_load_state().get("brake_until") or 0)
def latch_brake(hours, streak=0):
    s = _load_state(); s["brake_until"] = time.time() + hours * 3600
    s["brake_streak"] = streak; _save_state(s)


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

    # ==== LADDER helper (dipakai native & sintetis) ====
    @staticmethod
    def _ladder(sl, tp1, tp2=None, tp1_frac=1.0, move_be=False):
        tp2 = float(tp2) if tp2 is not None else float(tp1)
        frac = float(tp1_frac or 1.0)
        is_ladder = frac < 0.999 and abs(tp2 - float(tp1)) > 1e-12
        return {"sl": float(sl), "tp1": float(tp1), "tp2": tp2,
                "tp1_frac": frac, "move_be": bool(move_be), "ladder": is_ladder}

    async def close_partial_market(self, sym, qty):
        """Tutup SEBAGIAN posisi (reduceOnly) — dipakai TP1 50%."""
        qty_str, q = self.fmt_qty(qty, sym)
        if q <= 0:
            return {"ok": False, "error": "partial qty rounds to 0"}
        acc = await self.get_account()
        pos = next((p for p in self.open_positions(acc) if p.get("market") == sym), None)
        if not pos:
            return {"ok": True, "note": "no position"}
        side = "SELL" if self._position_is_long(pos) else "BUY"
        try:
            await self.c.spost("/fapi/v1/order", symbol=sym, side=side, type="MARKET",
                               quantity=qty_str, reduceOnly="true")
            return {"ok": True, "qty": q}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ==== PROTEKSI NATIVE (STOP_MARKET / TAKE_PROFIT_MARKET di exchange) ====
    async def _place_cond(self, sym, side, stop_price, qty=None, close_position=False, kind="STOP_MARKET"):
        p = {"symbol": sym, "side": side, "type": kind,
             "stopPrice": self.fmt_price(stop_price, sym), "workingType": "MARK_PRICE"}
        if close_position:
            p["closePosition"] = "true"
        else:
            qty_str, _q = self.fmt_qty(qty, sym)
            p["quantity"] = qty_str; p["reduceOnly"] = "true"
        return await self.c.spost("/fapi/v1/order", **p)

    async def _arm_native(self, sym, lad, close_side):
        """Coba pasang SL/TP1/TP2 NATIVE. Return dict; ok=False memicu fallback sintetis.
        Testnet menolak (-4120) -> caller fallback. Mainnet: order tersimpan di exchange."""
        amt, entry, _mark = await self._wait_position(sym)
        if amt == 0:
            return {"ok": False, "last_error": "position_not_ready"}
        qty = abs(amt)
        placed, ids = [], {}
        try:
            r = await self._place_cond(sym, close_side, lad["sl"], close_position=True, kind="STOP_MARKET")
            ids["sl"] = r.get("orderId"); placed.append(r.get("orderId"))
            if lad["ladder"]:
                q1_str, q1 = self.fmt_qty(qty * lad["tp1_frac"], sym)
                r1 = await self._place_cond(sym, close_side, lad["tp1"], qty=q1, kind="TAKE_PROFIT_MARKET")
                ids["tp1"] = r1.get("orderId"); placed.append(r1.get("orderId"))
                r2 = await self._place_cond(sym, close_side, lad["tp2"], close_position=True, kind="TAKE_PROFIT_MARKET")
                ids["tp2"] = r2.get("orderId"); placed.append(r2.get("orderId"))
            else:
                r1 = await self._place_cond(sym, close_side, lad["tp1"], close_position=True, kind="TAKE_PROFIT_MARKET")
                ids["tp1"] = r1.get("orderId"); placed.append(r1.get("orderId"))
        except Exception as e:
            for oid in placed:
                with contextlib.suppress(Exception):
                    await self.c.sdelete("/fapi/v1/order", symbol=sym, orderId=oid)
            return {"ok": False, "mode": "native_failed", "last_error": f"{type(e).__name__}: {e}",
                    "code": getattr(e, "code", None)}
        log.info("PROTECT NATIVE ON %s sl=%s tp1=%s tp2=%s ids=%s", sym,
                 self.fmt_price(lad["sl"], sym), self.fmt_price(lad["tp1"], sym),
                 self.fmt_price(lad["tp2"], sym), ids)
        if lad["ladder"] and lad["move_be"]:
            # watcher: saat TP1 native FILLED -> geser SL native ke break-even
            t = self._synth_tasks.get(sym)
            if t and not t.done(): t.cancel()
            self._synth_tasks[sym] = asyncio.create_task(
                self._native_be_watcher(sym, lad, close_side, entry, ids))
        return {"ok": True, "mode": "native", "sl": lad["sl"], "tp1": lad["tp1"], "tp2": lad["tp2"], "ids": ids}

    async def _native_be_watcher(self, sym, lad, close_side, entry, ids):
        """Mainnet: pantau TP1 native; begitu FILLED, batalkan SL lama & pasang SL di BE."""
        try:
            while True:
                await asyncio.sleep(CONFIG.synth_poll_sec)
                amt, _e, _m = await self._position_risk(sym)
                if amt == 0:
                    return
                st = ""
                with contextlib.suppress(Exception):
                    o = await self.order_status(ids.get("tp1"), sym)
                    st = str(o.get("status") or "")
                if st == "FILLED":
                    off = CONFIG.be_offset_pct
                    be = entry * (1 + off) if close_side == "SELL" else entry * (1 - off)
                    with contextlib.suppress(Exception):
                        await self.c.sdelete("/fapi/v1/order", symbol=sym, orderId=ids.get("sl"))
                    with contextlib.suppress(Exception):
                        await self._place_cond(sym, close_side, be, close_position=True, kind="STOP_MARKET")
                    log.info("NATIVE %s TP1 FILLED -> SL native digeser ke BE %s", sym, self.fmt_price(be, sym))
                    with contextlib.suppress(Exception):
                        from notify import send
                        await send(f"🎯 <b>TP1 kena (native) → {sym}</b> · SL sisa digeser ke BE ${be:,.4f}")
                    return
        except asyncio.CancelledError:
            pass

    # ---- proteksi sintetis per simbol (fallback bila native ditolak, mis. -4120 testnet) ----
    def _start_synth(self, sym, lad, close_side, meta=None):
        self._synth[sym] = {"sl": lad["sl"], "tp1": lad["tp1"], "tp2": lad["tp2"],
                            "tp1_frac": lad["tp1_frac"], "move_be": lad["move_be"],
                            "ladder": lad["ladder"], "close_side": close_side,
                            "is_long": close_side == "SELL", "meta": meta or {},
                            "tp1_done": False, "moved_be": False, "orig_qty": None, "realized": 0.0}
        t = self._synth_tasks.get(sym)
        if t and not t.done(): t.cancel()
        self._synth_tasks[sym] = asyncio.create_task(self._synth_loop(sym))

    async def _arm_synthetic(self, sym, lad, close_side, reason="", meta=None):
        self._start_synth(sym, lad, close_side, meta)
        log.info("PROTECT SYNTH ON %s sl=%s tp1=%s tp2=%s ladder=%s (%s)", sym,
                 self.fmt_price(lad["sl"], sym), self.fmt_price(lad["tp1"], sym),
                 self.fmt_price(lad["tp2"], sym), lad["ladder"], reason)
        return {"ok": True, "mode": "synthetic", "reason": reason,
                "sl": lad["sl"], "tp1": lad["tp1"], "tp2": lad["tp2"]}

    async def _synth_loop(self, sym):
        """Kelola LADDER sintetis: fase-1 (SL / TP1) lalu fase-2 (BE / TP2).
        Satu trade -> SATU baris jurnal (PnL diakumulasi) supaya WR tidak dobel-hitung."""
        s = self._synth.get(sym)
        if not s: return
        try:
            while True:
                await asyncio.sleep(CONFIG.synth_poll_sec)
                try: amt, entry_px, mark = await self._position_risk(sym)
                except Exception: continue
                if amt == 0:
                    self._synth.pop(sym, None); return
                if mark is None: continue
                if s["orig_qty"] is None and amt != 0:
                    s["orig_qty"] = abs(amt)
                is_long = s["is_long"]

                if not s["tp1_done"]:
                    if self._breached("sl", is_long, mark, s["sl"]):
                        r = await self.close_position_market(sym)
                        pnl = round((mark - entry_px) * amt, 4) if entry_px else None
                        await self._synth_close(sym, s, mark, "SL", r, entry_px, pnl)
                        self._synth.pop(sym, None); return
                    if self._breached("tp", is_long, mark, s["tp1"]):
                        if not s["ladder"]:
                            r = await self.close_position_market(sym)
                            pnl = round((mark - entry_px) * amt, 4) if entry_px else None
                            await self._synth_close(sym, s, mark, "TP2", r, entry_px, pnl)
                            self._synth.pop(sym, None); return
                        # ladder: tutup sebagian di TP1, sisanya lanjut ke TP2
                        close_qty = (s["orig_qty"] or abs(amt)) * s["tp1_frac"]
                        r = await self.close_partial_market(sym, close_qty)
                        pnl1 = round((mark - entry_px) * (amt * s["tp1_frac"]), 4) if entry_px else 0.0
                        s["realized"] += pnl1 or 0.0
                        s["tp1_done"] = True
                        if s["move_be"] and entry_px:
                            off = CONFIG.be_offset_pct
                            s["sl"] = entry_px * (1 + off) if is_long else entry_px * (1 - off)
                            s["moved_be"] = True
                            log.info("SYNTH %s TP1 -> SL pindah BE %s", sym, self.fmt_price(s["sl"], sym))
                        with contextlib.suppress(Exception):
                            from notify import send
                            be_txt = f" · SL sisa → BE ${s['sl']:,.4f}" if s["moved_be"] else ""
                            await send(f"🎯 <b>TP1 kena → {sym} 50% ditutup</b> (sintetis)\n"
                                       f"• mark ${mark:,.4f}{be_txt} · target TP2 ${s['tp2']:,.4f}")
                        continue
                else:
                    if self._breached("tp", is_long, mark, s["tp2"]):
                        r = await self.close_position_market(sym)
                        pnl = round((mark - entry_px) * amt, 4) if entry_px else 0.0
                        await self._synth_close(sym, s, mark, "TP2", r, entry_px, (s["realized"] + (pnl or 0.0)))
                        self._synth.pop(sym, None); return
                    if self._breached("sl", is_long, mark, s["sl"]):
                        r = await self.close_position_market(sym)
                        pnl = round((mark - entry_px) * amt, 4) if entry_px else 0.0
                        # moved_be -> trade tetap WIN (TP1 sudah dibank); jika tidak, SL sisa
                        outcome = "TP1" if s["moved_be"] else "SL"
                        await self._synth_close(sym, s, mark, outcome, r, entry_px, (s["realized"] + (pnl or 0.0)))
                        self._synth.pop(sym, None); return
        except asyncio.CancelledError:
            pass

    async def _synth_close(self, sym, s, mark, which, r, entry=None, pnl_est=None):
        """Catat SATU baris jurnal terminal + notifikasi Telegram."""
        if r.get("ok"):
            try:
                import journal
                meta = s.get("meta") or {}
                journal.record_trade(symbol=sym, outcome=which,
                                     side=("long" if s["is_long"] else "short"),
                                     entry=entry, exit_price=mark, sl=s["sl"], tp=s["tp2"],
                                     pnl_usd=pnl_est, mode="synthetic",
                                     regime=meta.get("regime"), confidence=meta.get("confidence"),
                                     rr=meta.get("rr"))
            except Exception:
                pass
        with contextlib.suppress(Exception):
            from notify import send
            label = {"TP2": "TP2 (target maks)", "TP1": "TP1 (runner di BE)",
                     "SL": "STOP LOSS", "TP": "TAKE PROFIT"}.get(which, which)
            if r.get("ok"):
                await send(f"{'🎯' if which.startswith('TP') else '🛑'} <b>{label} → {sym} DITUTUP</b> (sintetis)\n"
                           f"• mark ${mark:,.4f} · PnL~${(pnl_est or 0):+,.2f}")
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

    async def _arm_protection(self, sym, lad, close_side, meta=None):
        """lad = dict dari self._ladder(...). Orkestrasi: cek pre-breach -> NATIVE (auto/native)
        -> fallback SINTETIS. #5: TP/SL asli di exchange bila venue mendukung, sintetis bila tidak."""
        is_long = close_side == "SELL"
        amt, entry, mark = await self._wait_position(sym)
        if amt == 0:
            return {"ok": False, "last_error": "position_not_ready"}
        log.info("PROTECT %s entry=%.6f qty=%s mark=%s SL=%s TP1=%s TP2=%s", sym, entry, amt, mark,
                 self.fmt_price(lad["sl"], sym), self.fmt_price(lad["tp1"], sym), self.fmt_price(lad["tp2"], sym))
        if self._breached("sl", is_long, mark, lad["sl"]):
            r = await self.close_position_market(sym)
            return {"ok": bool(r.get("ok")), "closed": True, "reason": "sl_breached_preplace"}
        if self._breached("tp", is_long, mark, lad["tp2"]):
            r = await self.close_position_market(sym)
            return {"ok": bool(r.get("ok")), "closed": True, "reason": "tp_breached_preplace"}
        mode = CONFIG.protection_mode
        if mode in ("auto", "native"):
            res = await self._arm_native(sym, lad, close_side)
            if res.get("ok"):
                return res
            if mode == "native":
                return res  # native dipaksa: jangan fallback, laporkan gagal
            log.warning("PROTECT %s native gagal (%s) -> fallback sintetis", sym, res.get("last_error"))
        return await self._arm_synthetic(sym, lad, close_side, reason=("fallback" if mode == "auto" else "config"), meta=meta)

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
            R = abs(entry - sl)
            tp1 = entry + CONFIG.tp1_rr * R if is_long else entry - CONFIG.tp1_rr * R
            tp2 = entry + CONFIG.tp2_rr * R if is_long else entry - CONFIG.tp2_rr * R
            lad = self._ladder(sl, tp1, tp2, CONFIG.tp1_close_frac, CONFIG.move_sl_to_be)
            res = await self._arm_protection(sym, lad, side)
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
                        prot = await self._arm_protection(sym, self._decision_ladder(decision), close_side,
                                                          meta=self._decision_meta(decision))
                        with contextlib.suppress(Exception):
                            from notify import send
                            if prot.get("ok"):
                                await send(f"🛰️ <b>{sym} limit terisi → SL/TP {prot.get('mode')} armed</b> (watcher)\n"
                                           f"• SL ${decision['stop']:,.4f} · TP1 ${decision['tp1']:,.4f} · TP2 ${decision.get('tp2') or decision['tp1']:,.4f}")
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

    @staticmethod
    def _decision_meta(decision):
        """Metadata keputusan yang ikut dicatat ke jurnal saat posisi ditutup —
        bahan baku modul pembelajaran (lessons.py)."""
        return {"regime": decision.get("regime"),
                "confidence": decision.get("confidence_pct"),
                "rr": decision.get("rr")}

    def _decision_ladder(self, decision):
        """Bangun LADDER (SL / TP1 50% / TP2 / BE) dari keputusan risk governor."""
        return self._ladder(decision["stop"], decision["tp1"],
                            decision.get("tp2"), decision.get("tp1_close_frac"),
                            decision.get("move_sl_to_be"))

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
                    out["protection"] = await self._arm_protection(sym, self._decision_ladder(decision), close_side,
                                                                   meta=self._decision_meta(decision))
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
                out["protection"] = await self._arm_protection(sym, self._decision_ladder(decision), close_side,
                                                               meta=self._decision_meta(decision))
        elif want and CONFIG.limit_fill_watcher:
            self.start_fill_watcher(decision, oid)
            out["protection"] = {"deferred": True, "mode": "watcher", "poll_sec": CONFIG.watch_poll_sec}
        return out
