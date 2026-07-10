"""Rekonsiliasi close NATIVE: deteksi SL/TP native yang terisi DI EXCHANGE.
Dorman di testnet (native -4120); aktif di mainnet nanti."""
import journal

_COND = ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT")

def _num(x):
    try:
        v = float(x); return v if v else None
    except (TypeError, ValueError):
        return None

async def reconcile_native_closes(client, symbol, limit=50):
    try:
        orders = await client.sget("/fapi/v1/allOrders", symbol=symbol, limit=limit)
    except Exception:
        return 0
    new = 0
    for o in (orders or []):
        if str(o.get("status")) != "FILLED": continue
        typ = str(o.get("type") or o.get("origType") or "")
        if typ not in _COND: continue
        cp = str(o.get("closePosition")).lower() == "true"
        ro = str(o.get("reduceOnly")).lower() in ("true", "1")
        if not (cp or ro): continue
        outcome = "SL" if "STOP" in typ else "TP"
        trig = _num(o.get("stopPrice"))
        ts = int(int(o.get("updateTime") or o.get("time") or 0) / 1000) or None
        if journal.record_trade(symbol=symbol, outcome=outcome,
                                sl=(trig if outcome == "SL" else None),
                                tp=(trig if outcome == "TP" else None),
                                exit_price=_num(o.get("avgPrice")), mode="native",
                                order_id=o.get("orderId"), ts=ts):
            new += 1
    return new
