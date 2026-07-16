"""Zupin Bot — Dashboard Performa (Streamlit). READ-ONLY: hanya membaca trades.db,
TIDAK menyentuh logika/eksekusi bot. Aman dijalankan kapan saja.

Jalankan:
    pip install streamlit pandas
    streamlit run dashboard.py
Buka di browser: http://localhost:8501

DI VPS (jangan expose publik — data trading rahasia):
    # di VPS:  streamlit run dashboard.py --server.address 127.0.0.1
    # di laptop:  ssh -L 8501:localhost:8501 user@ip_vps
    # lalu buka http://localhost:8501 di laptop
"""
import os
import sqlite3
import datetime as dt

import pandas as pd
import streamlit as st

DB_PATH = os.getenv("JOURNAL_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.db"))
WIB = dt.timezone(dt.timedelta(hours=7))
WIN = ("TP", "TP1", "TP2")
LOSS = ("SL",)

st.set_page_config(page_title="Zupin Bot — Performa", page_icon="📊", layout="wide")


def _coin(sym):
    s = str(sym or "").upper()
    for suf in ("USDT", "USDC", "USD", "PERP", "-", "_"):
        s = s.replace(suf, "")
    return s or str(sym)


@st.cache_data(ttl=20)
def _read(query):
    try:
        con = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query(query, con)
        con.close()
        return df
    except Exception:
        return pd.DataFrame()


def _wr(df):
    w = df["outcome"].isin(WIN).sum()
    l = df["outcome"].isin(LOSS).sum()
    return (round(w / (w + l) * 100) if (w + l) else None), int(w), int(l)


# ---------------- Sidebar ----------------
st.sidebar.title("📊 Zupin Bot")
st.sidebar.caption(f"DB: {os.path.basename(DB_PATH)}")
if st.sidebar.button("🔄 Refresh data"):
    st.cache_data.clear()
    st.rerun()
days = st.sidebar.slider("Rentang hari", 1, 90, 30)
since = (dt.datetime.now(WIB) - dt.timedelta(days=days)).strftime("%Y-%m-%d")

trades = _read(f"SELECT * FROM trades WHERE date >= '{since}' ORDER BY ts ASC")
snaps = _read(f"SELECT * FROM snapshots WHERE date >= '{since}' ORDER BY ts ASC")
opens = _read("SELECT * FROM open_positions ORDER BY symbol")

st.title("📊 Dashboard Performa Trading")
st.caption(f"Rentang {days} hari terakhir · sejak {since}")

if trades.empty and snaps.empty:
    st.info("Belum ada data di trades.db. Dashboard akan terisi setelah bot merekam trade/snapshot.")
    st.stop()

# ---------------- Ringkasan (metrics) ----------------
decided = trades[trades["outcome"].isin(WIN + LOSS)] if not trades.empty else pd.DataFrame()
wr, wins, losses = _wr(decided) if not decided.empty else (None, 0, 0)
be = int((trades["outcome"] == "BE").sum()) if not trades.empty else 0
realized = float(trades["pnl_usd"].dropna().sum()) if not trades.empty else 0.0
equity = float(snaps["equity"].iloc[-1]) if not snaps.empty else None
init_cap = float(snaps["initial_capital"].iloc[-1]) if not snaps.empty else None
total_pnl = (equity - init_cap) if (equity is not None and init_cap) else None

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Equity", f"${equity:,.2f}" if equity is not None else "—",
          f"{(total_pnl):+,.2f}" if total_pnl is not None else None)
c2.metric("Win Rate", f"{wr}%" if wr is not None else "—", f"{wins}W / {losses}L")
c3.metric("Trade selesai", f"{len(decided)}", f"BE {be}" if be else None)
c4.metric("Realized PnL", f"${realized:,.2f}")
avg_mfe = float(trades["mfe_r"].dropna().mean()) if ("mfe_r" in trades and trades["mfe_r"].notna().any()) else None
c5.metric("Rata-rata MFE", f"{avg_mfe:.2f} R" if avg_mfe is not None else "—")

st.divider()

# ---------------- Posisi terbuka ----------------
st.subheader("📌 Posisi Terbuka")
if opens.empty:
    st.write("Tidak ada posisi terbuka.")
else:
    o = opens.copy()
    o["coin"] = o["symbol"].map(_coin)
    o = o[["coin", "side", "entry", "size", "unrealized"]].rename(
        columns={"coin": "Coin", "side": "Arah", "entry": "Harga Masuk",
                 "size": "Ukuran", "unrealized": "Unrealized $"})
    st.dataframe(o, use_container_width=True, hide_index=True)

st.divider()
colA, colB = st.columns(2)

# ---------------- Kurva Equity ----------------
with colA:
    st.subheader("💰 Kurva Equity")
    if snaps.empty:
        st.write("Belum ada snapshot equity.")
    else:
        eq = snaps.copy()
        eq["waktu"] = pd.to_datetime(eq["ts"], unit="s", utc=True).dt.tz_convert(WIB)
        st.line_chart(eq.set_index("waktu")[["equity"]])

# ---------------- PnL per coin ----------------
with colB:
    st.subheader("🪙 PnL per Coin")
    if trades.empty or trades["pnl_usd"].dropna().empty:
        st.write("Belum ada PnL tercatat.")
    else:
        t = trades.copy()
        t["coin"] = t["symbol"].map(_coin)
        pc = t.groupby("coin")["pnl_usd"].sum().sort_values()
        st.bar_chart(pc)

st.divider()
colC, colD = st.columns(2)

# ---------------- Distribusi MFE (utk keputusan trailing) ----------------
with colC:
    st.subheader("🎯 Distribusi MFE (R)")
    st.caption("Seberapa jauh harga bergerak ke arah kita sebelum ditutup. "
               "Banyak batang di 3R+ → trailing layak; jarang → fixed TP2 sudah optimal.")
    if "mfe_r" in trades and trades["mfe_r"].notna().any():
        m = trades["mfe_r"].dropna()
        bins = [-99, -1, 0, 1, 2, 3, 4, 99]
        labels = ["<-1R", "-1..0", "0..1R", "1..2R", "2..3R", "3..4R", "4R+"]
        hist = pd.cut(m, bins=bins, labels=labels).value_counts().reindex(labels).fillna(0)
        st.bar_chart(hist)
    else:
        st.write("Belum ada data MFE (muncul setelah trade selesai dengan versi baru).")

# ---------------- Outcome breakdown ----------------
with colD:
    st.subheader("📈 Rincian Outcome")
    if trades.empty:
        st.write("Belum ada trade.")
    else:
        oc = trades["outcome"].value_counts()
        st.bar_chart(oc)

st.divider()

# ---------------- Tabel trade terakhir ----------------
st.subheader("🧾 Trade Terakhir")
if trades.empty:
    st.write("Belum ada trade.")
else:
    tt = trades.sort_values("ts", ascending=False).head(50).copy()
    tt["waktu"] = pd.to_datetime(tt["ts"], unit="s", utc=True).dt.tz_convert(WIB).dt.strftime("%Y-%m-%d %H:%M")
    tt["coin"] = tt["symbol"].map(_coin)
    cols = ["waktu", "coin", "side", "outcome", "entry", "exit_price", "pnl_usd", "rr", "mfe_r", "mode"]
    cols = [c for c in cols if c in tt.columns]
    st.dataframe(tt[cols].rename(columns={
        "waktu": "Waktu (WIB)", "coin": "Coin", "side": "Arah", "outcome": "Hasil",
        "entry": "Masuk", "exit_price": "Keluar", "pnl_usd": "PnL $", "rr": "R:R",
        "mfe_r": "MFE R", "mode": "Mode"}), use_container_width=True, hide_index=True)

st.caption(f"Diperbarui: {dt.datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S')} WIB · "
           "data auto-cache 20 detik · klik 🔄 di sidebar untuk paksa refresh")
