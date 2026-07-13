"""Modul PEMBELAJARAN v1 — belajar dari data jurnal NYATA, tanpa AI, tanpa asumsi.

Prinsip anti-halusinasi: semua "lesson" adalah agregat deterministik dari tabel
trades (TP/SL riil). Tidak ada prediksi, tidak ada narasi. Tiga keluaran:

  1. sl_streak()      -> SL beruntun terakhir (lintas hari) utk rem darurat
  2. build_memory()   -> teks statistik utk di-inject ke snapshot AI
                         + penyesuaian deterministik per simbol:
                         - blocked : simbol dgn >=N sampel dan 0 TP -> skip dulu
                         - extra_conf : simbol WR buruk -> syarat conf dinaikkan
                         - keep : pola yg terbukti berhasil -> dipertahankan
Sampel kecil TIDAK dihukum (min CONFIG.lessons_min_trades) — menghukum simbol
karena 1-2 trade adalah noise, bukan belajar.
"""
import journal
from config import CONFIG

WIN = journal.WIN_OUTCOMES     # ("TP","TP1","TP2")
LOSS = journal.LOSS_OUTCOMES   # ("SL",)


def sl_streak(days=None):
    """SL beruntun di ekor riwayat (lintas simbol & hari). WIN/BE memutus streak.
    Ini inti #8: bot MEMUTUS pola gagal & tidak mengulanginya (rem entry baru)."""
    rows = journal.recent_trades(days or CONFIG.lessons_lookback_days)
    streak = 0
    for r in reversed(rows):          # r = (ts,date,symbol,outcome,side,regime,conf,rr,pnl)
        if r[3] in LOSS:
            streak += 1
        else:
            break                     # TP/TP1/TP2/BE = bukan kekalahan -> streak berhenti
    return streak


def _bucket(rows, key_idx):
    out = {}
    for r in rows:
        k = r[key_idx] or "?"
        b = out.setdefault(k, {"n": 0, "tp": 0, "sl": 0, "pnl": 0.0})
        b["n"] += 1
        b["tp"] += 1 if r[3] in WIN else 0
        b["sl"] += 1 if r[3] in LOSS else 0
        if r[8] is not None:
            b["pnl"] += float(r[8])
    return out


def _wr(b):
    """Win-rate dari trade yang DIPUTUSKAN (win+loss); BE netral tidak dihitung."""
    decided = b["tp"] + b["sl"]
    return round(b["tp"] / decided * 100) if decided else 0


def build_memory(days=None):
    days = days or CONFIG.lessons_lookback_days
    rows = journal.recent_trades(days)
    min_n = CONFIG.lessons_min_trades
    by_sym = _bucket(rows, 2)
    by_side = _bucket(rows, 4)
    by_regime = _bucket(rows, 5)

    blocked, extra_conf, keep, lines = [], {}, [], []
    for sym, b in sorted(by_sym.items()):
        wr = _wr(b)
        decided = b["tp"] + b["sl"]
        lines.append(f"{sym}: {b['n']} trade, WIN {b['tp']}/SL {b['sl']} (WR {wr}%), PnL~{b['pnl']:+.2f}")
        # #8: belajar dari data GAGAL -> istirahatkan simbol yg berulang kalah tanpa satupun menang
        if decided >= min_n and b["tp"] == 0:
            blocked.append(sym)          # semua trade diputuskan = kalah -> istirahatkan
        elif decided >= min_n and wr < 40:
            extra_conf[sym] = 10.0       # masih boleh, tapi bukti harus lebih kuat
        elif decided >= min_n and wr >= 60:
            keep.append(sym)             # #8: pola BERHASIL -> pertahankan (jangan dihukum)

    for side, b in sorted(by_side.items()):
        if side == "?" or (b["tp"] + b["sl"]) < min_n:
            continue
        lines.append(f"arah {side}: {b['n']} trade, WR {_wr(b)}%")
    for reg, b in sorted(by_regime.items()):
        if reg == "?" or (b["tp"] + b["sl"]) < min_n:
            continue
        lines.append(f"regime {reg}: {b['n']} trade, WR {_wr(b)}%")

    streak = sl_streak(days)
    if streak >= 2:
        lines.append(f"PERINGATAN: {streak} SL beruntun terakhir — standar entry HARUS lebih tinggi")
    if blocked:
        lines.append(f"DIISTIRAHATKAN (0 TP dari >={min_n} sampel): {', '.join(blocked)}")
    if keep:
        lines.append(f"PERTAHANKAN (WR>=60%): {', '.join(keep)}")

    text = (f"Statistik riil {days} hari terakhir ({len(rows)} trade selesai):\n- "
            + "\n- ".join(lines)) if lines else \
        f"Belum ada trade selesai dalam {days} hari terakhir — belum ada data pembelajaran."
    return {"text": text, "blocked": blocked, "extra_conf": extra_conf,
            "keep": keep, "sl_streak": streak, "sample": len(rows)}
