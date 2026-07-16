"""PnL Bot v2 — laporan harian + mingguan + bulanan.

Perintah:
  /pnl                 -> laporan hari ini
  /pnl 07-07-2026      -> laporan tanggal tertentu
  /week                -> 7 hari terakhir (agregat)
  /month               -> 30 hari terakhir (agregat)
  /today               -> alias /pnl hari ini
  /help                -> bantuan

Jalankan: PNL_BOT_TOKEN=xxxx python pnl_bot.py
"""
import os
import re
import time
import datetime
import httpx
from dotenv import load_dotenv

import journal

# v6 FIX: pnl_bot dulu TIDAK membaca .env (beda dgn main.py yg load via config.py) -> token
# di .env terabaikan & muncul "Set PNL_BOT_TOKEN_V5 dulu". Sekarang .env dibaca eksplisit.
load_dotenv()

# v5.1: token KHUSUS v5. Telegram hanya mengizinkan SATU poller getUpdates per token —
# memakai token yang sama dengan journal bot v4.1 PASTI bertabrakan (409 / update dicuri),
# folder berbeda tidak berpengaruh. Buat bot baru di @BotFather utk v5.
TOKEN = os.getenv("PNL_BOT_TOKEN_V5") or os.getenv("PNL_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
BOT_USERNAME = ""  # diisi via getMe saat start
DISPLAY_COINS = [c.strip().upper() for c in os.getenv("PNL_COINS", "SOL,BTC,ETH").split(",") if c.strip()]
_DEFAULT_ICONS = {"BTC": "🟠", "ETH": "🔷", "SOL": "🟣", "BNB": "🟡",
                  "XRP": "⚪", "DOGE": "🟡", "ADA": "🔵", "AVAX": "🔴"}


def _load_icons():
    icons = dict(_DEFAULT_ICONS)
    for pair in os.getenv("PNL_ICONS", "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            icons[k.strip().upper()] = v.strip()
    return icons


COIN_ICONS = _load_icons()
WIB_OFFSET = 7 * 3600


def _today_wib():
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + WIB_OFFSET))


def _date_n_days_ago(n):
    ts = time.time() + WIB_OFFSET - n * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _ddmmyyyy_to_iso(s):
    m = re.match(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})\s*$", s)
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _iso_to_display(iso):
    y, mo, d = iso.split("-")
    return f"{d}-{mo}-{y}"


def _f(x):
    try:
        return f"{float(x):,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _sgn(x):
    try:
        v = float(x)
        return ("+" if v >= 0 else "") + f"{v:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _dot(x):
    try:
        return "🟢" if float(x) >= 0 else "🔴"
    except (TypeError, ValueError):
        return "⚪"


def _coin_line(per_coin):
    parts, shown = [], set()
    for coin in DISPLAY_COINS:
        parts.append(f"{COIN_ICONS.get(coin, '🪙')} {coin}: {per_coin.get(coin, 0)}")
        shown.add(coin)
    for coin, n in sorted(per_coin.items(), key=lambda kv: -kv[1]):
        if coin not in shown:
            parts.append(f"{COIN_ICONS.get(coin, '🪙')} {coin}: {n}")
    return "• " + " · ".join(parts) if parts else "• -"


def _open_positions_block():
    """v6.1: daftar coin yang SEDANG open posisi + unrealized (ditulis bot trading tiap siklus)."""
    try:
        positions, last_ts = journal.get_open_positions()
    except Exception:
        return None
    lines = ["📌 <b>Posisi Terbuka</b>"]
    if not positions:
        lines.append("• Tidak ada posisi terbuka")
        return "\n".join(lines)
    for p in positions:
        coin = journal._coin(p.get("symbol"))
        icon = COIN_ICONS.get(coin, "🪙")
        arrow = "📈" if str(p.get("side")) == "long" else "📉"
        u = p.get("unrealized")
        entry = p.get("entry")
        lines.append(f"{icon} {coin} {arrow} {str(p.get('side')).upper()} · "
                     f"masuk ${_f(entry)} · {_dot(u)} ${_sgn(u)}")
    if last_ts:
        age = time.time() - last_ts
        if age > 20 * 60:
            lines.append(f"• <i>⚠️ data {int(age / 60)} mnt lalu — bot trading mungkin berhenti</i>")
    return "\n".join(lines)


# ---- RENDER LAPORAN HARIAN ----
def render_daily(iso_date):
    s = journal.stats_for_date(iso_date)
    snap = s.get("snapshot")
    wr = s.get("wr")
    lines = [f"📊 <b>Laporan PnL — {_iso_to_display(iso_date)}</b>", ""]

    lines.append("💰 <b>Akun</b>")
    if snap:
        lines.append(f"• Modal: <b>${_f(snap.get('initial_capital'))}</b>")
        lines.append(f"• Equity: <b>${_f(snap.get('equity'))}</b>")
        lines.append(f"• Unrealized: {_dot(snap.get('unrealized'))} ${_sgn(snap.get('unrealized'))}")
        lines.append(f"• Hari ini: {_dot(snap.get('daily_pnl'))} ${_sgn(snap.get('daily_pnl'))}")
    else:
        lines.append("• <i>Belum ada snapshot akun untuk tanggal ini</i>")
    lines.append("")

    be = s.get("be", 0)
    be_txt = f" · BE ⚪ {be}" if be else ""
    lines.append(f"📈 <b>Trade {_iso_to_display(iso_date)}</b>")
    lines.append(f"• Total: <b>{s['total']}</b> · WIN ✅ {s['tp']} · SL 🛑 {s['sl']}{be_txt} · WR <b>{wr}%</b>" if wr is not None
                 else f"• Total: <b>{s['total']}</b> · WIN ✅ {s['tp']} · SL 🛑 {s['sl']}{be_txt} · WR —")
    lines.append("")

    _op = _open_positions_block()
    if _op:
        lines.append(_op)
        lines.append("")

    lines.append("🪙 <b>Per Coin</b>")
    lines.append(_coin_line(s.get("per_coin", {})))

    if s["total"] == 0 and not snap:
        lines.append("")
        lines.append("<i>Tidak ada data pada tanggal ini.</i>")
    return "\n".join(lines)


# ---- RENDER LAPORAN PERIODE (minggu/bulan) ----
def render_period(label, start_iso, end_iso):
    s = journal.stats_for_range(start_iso, end_iso)
    snap = s.get("snapshot")
    wr = s.get("wr")
    eq_start = s.get("equity_start")
    eq_end = snap.get("equity") if snap else None
    period_pnl = round(eq_end - eq_start, 2) if (eq_end is not None and eq_start is not None) else None
    period_pct = round(period_pnl / eq_start * 100, 2) if (period_pnl is not None and eq_start) else None

    lines = [f"📊 <b>Laporan {label}</b>", f"📅 {_iso_to_display(start_iso)} → {_iso_to_display(end_iso)}", ""]

    lines.append("💰 <b>Akun (terkini)</b>")
    if snap:
        lines.append(f"• Modal: <b>${_f(snap.get('initial_capital'))}</b>")
        lines.append(f"• Equity: <b>${_f(eq_end)}</b>")
        lines.append(f"• Unrealized: {_dot(snap.get('unrealized'))} ${_sgn(snap.get('unrealized'))}")
    else:
        lines.append("• <i>Belum ada snapshot akun</i>")
    lines.append("")

    lines.append(f"📈 <b>Performa {label}</b>")
    if eq_start is not None:
        lines.append(f"• Equity awal: ${_f(eq_start)} → akhir: ${_f(eq_end)}")
    if period_pnl is not None:
        lines.append(f"• PnL periode: {_dot(period_pnl)} <b>${_sgn(period_pnl)}</b> ({_sgn(period_pct)}%)")
    lines.append(f"• Hari aktif: {s.get('active_days', 0)}")
    lines.append("")

    be = s.get("be", 0)
    be_txt = f" · BE ⚪ {be}" if be else ""
    lines.append(f"📋 <b>Trade {label}</b>")
    lines.append(f"• Total: <b>{s['total']}</b> · WIN ✅ {s['tp']} · SL 🛑 {s['sl']}{be_txt} · WR <b>{wr}%</b>" if wr is not None
                 else f"• Total: <b>{s['total']}</b> · WIN ✅ {s['tp']} · SL 🛑 {s['sl']}{be_txt} · WR —")
    if s.get("realized_usd"):
        lines.append(f"• Realized PnL: {_dot(s['realized_usd'])} ${_sgn(s['realized_usd'])}")
    lines.append("")

    _op = _open_positions_block()
    if _op:
        lines.append(_op)
        lines.append("")

    lines.append("🪙 <b>Per Coin</b>")
    lines.append(_coin_line(s.get("per_coin", {})))

    if s["total"] == 0:
        lines.append("")
        lines.append("<i>Tidak ada trade dalam periode ini.</i>")
    return "\n".join(lines)


# ---- COMMAND HANDLER ----
def handle_command(text):
    t = (text or "").strip()
    first = t.split()[0] if t else ""
    # filter mention: /pnl@BotV41 di grup bersama BUKAN untuk bot ini -> abaikan
    if "@" in first and BOT_USERNAME:
        if first.split("@", 1)[1].lower() != BOT_USERNAME.lower():
            return None
    cmd = first.lower().split("@")[0]

    if cmd in ("/pnl", "/today"):
        arg = t.split(maxsplit=1)[1].strip() if (cmd == "/pnl" and len(t.split()) > 1) else ""
        if not arg:
            return render_daily(_today_wib())
        iso = _ddmmyyyy_to_iso(arg)
        if not iso:
            return ("⚠️ Format tanggal salah. Gunakan <b>DD-MM-YYYY</b>.\n"
                    "Contoh: <code>/pnl 07-07-2026</code>")
        return render_daily(iso)

    if cmd == "/week":
        return render_period("Mingguan (7 hari)", _date_n_days_ago(6), _today_wib())

    if cmd == "/month":
        return render_period("Bulanan (30 hari)", _date_n_days_ago(29), _today_wib())

    if cmd in ("/help", "/start"):
        return ("🤖 <b>PnL Bot</b>\n\n"
                "📅 <b>Harian</b>\n"
                "• <code>/pnl</code> — hari ini\n"
                "• <code>/pnl 07-07-2026</code> — tanggal tertentu\n"
                "• <code>/today</code> — hari ini\n\n"
                "📆 <b>Periode</b>\n"
                "• <code>/week</code> — 7 hari terakhir (WR + PnL)\n"
                "• <code>/month</code> — 30 hari terakhir (WR + PnL)\n\n"
                "<i>Data dari jurnal trade ZupinBot (akurat, bukan parsing chat).</i>")
    return None


def send(chat_id, text, reply_to=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        r = httpx.post(f"{API}/sendMessage", json=payload, timeout=20)
        if r.status_code != 200:
            payload.pop("parse_mode", None)
            httpx.post(f"{API}/sendMessage", json=payload, timeout=20)
    except Exception as e:
        print("[pnl_bot] send failed:", e)


def main():
    global BOT_USERNAME
    if not TOKEN:
        raise SystemExit("Set PNL_BOT_TOKEN_V5 (atau PNL_BOT_TOKEN) dulu — token BARU dari "
                         "@BotFather, JANGAN pakai token journal bot v4.1.")
    journal.init_db()
    try:
        me = httpx.get(f"{API}/getMe", timeout=20).json()
        BOT_USERNAME = (me.get("result") or {}).get("username") or ""
    except Exception as e:
        print("[pnl_bot] getMe gagal:", e)
    print(f"[pnl_bot] v2.1 online sebagai @{BOT_USERNAME or '?'} — polling…")
    print("[pnl_bot] catatan: di grup bersama bot v4.1, panggil dengan mention: "
          f"/pnl@{BOT_USERNAME or '<bot_v5>'}")
    offset = None
    conflict_warned = False
    while True:
        try:
            r = httpx.get(f"{API}/getUpdates",
                          params={"offset": offset, "timeout": 30}, timeout=40)
            if r.status_code == 409:
                if not conflict_warned:
                    print("[pnl_bot] ⚠️ KONFLIK 409: token ini SEDANG dipakai proses lain "
                          "(kemungkinan journal bot v4.1). Telegram hanya mengizinkan 1 poller "
                          "per token. Solusi: buat bot BARU di @BotFather dan set PNL_BOT_TOKEN_V5.")
                    conflict_warned = True
                time.sleep(10)
                continue
            conflict_warned = False
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("channel_post")
                if not msg:
                    continue
                text = msg.get("text", "")
                if not text.startswith("/"):
                    continue
                reply = handle_command(text)
                if reply:
                    send(msg["chat"]["id"], reply, reply_to=msg.get("message_id"))
        except Exception as e:
            print("[pnl_bot] loop error:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
