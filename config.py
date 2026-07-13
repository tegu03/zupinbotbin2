"""Config v5 MULTI-COIN. 9 simbol terverifikasi (check_symbols 10-07-2026).
Screener dua-tahap: tahap-1 murni data (tanpa AI) -> tahap-2 MSE+PTE hanya kandidat.
Batas keras: MAX_CONCURRENT=2 posisi (risiko agregat <=2% via 1%/trade), 1 entry/siklus."""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()
def _f(k, d): return float(os.getenv(k, d))
def _i(k, d): return int(os.getenv(k, d))
def _b(k, d): return os.getenv(k, d).strip().lower() in ("1", "true", "yes", "on")

_DEFAULT_SYMBOLS = "SUIUSDT,VIRTUALUSDT,PENGUUSDT,NEARUSDT,HYPEUSDT,ENAUSDT,MONUSDT,VANRYUSDT,CAKEUSDT"

@dataclass
class Config:
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    thinking: bool = _b("DEEPSEEK_THINKING", "true")

    binance_base: str = os.getenv("BINANCE_FUTURES_BASE", "https://testnet.binancefuture.com")
    binance_data_base: str = os.getenv("BINANCE_DATA_BASE", "https://fapi.binance.com")
    binance_api_key: str = os.getenv("BINANCE_API_KEY", "")
    binance_api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    symbols: list = field(default_factory=lambda: [s.strip().upper() for s in os.getenv("SYMBOLS", _DEFAULT_SYMBOLS).split(",") if s.strip()])
    symbol: str = os.getenv("SYMBOL", "SUIUSDT")  # fallback tampilan
    recv_window: int = _i("BINANCE_RECV_WINDOW", "5000")
    binance_min_notional: float = _f("BINANCE_MIN_NOTIONAL", "5")
    taker_fee_pct: float = _f("TAKER_FEE_PCT", "0.0005")
    maker_fee_pct: float = _f("MAKER_FEE_PCT", "0.0002")

    max_concurrent_positions: int = _i("MAX_CONCURRENT_POSITIONS", "2")
    screener_top_n: int = _i("SCREENER_TOP_N", "3")

    initial_capital: float = _f("INITIAL_CAPITAL", "5000")
    place_sl_tp: bool = _b("PLACE_SL_TP", "true")
    protect_max_retries: int = _i("PROTECT_MAX_RETRIES", "4")
    protect_retry_backoff_sec: float = _f("PROTECT_RETRY_BACKOFF_SEC", "3")
    guardian_enabled: bool = _b("GUARDIAN_ENABLED", "true")
    guardian_stop_pct: float = _f("GUARDIAN_STOP_PCT", "0.01")
    emergency_close_if_unprotected: bool = _b("EMERGENCY_CLOSE_IF_UNPROTECTED", "true")

    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    interval: str = os.getenv("INTERVAL", "15m")              # = entry_interval (backward-compat)
    risk_pct: float = _f("RISK_PCT", "0.01")
    # --- v6: MULTI-TIMEFRAME (trend 1H, entry 15m) ---
    entry_interval: str = os.getenv("ENTRY_INTERVAL", os.getenv("INTERVAL", "15m"))
    trend_interval: str = os.getenv("TREND_INTERVAL", "1h")   # timeframe konfirmasi trend
    htf_align_required: bool = _b("HTF_ALIGN_REQUIRED", "true")  # #6: dilarang entry melawan trend 1H
    adx_min: float = _f("ADX_MIN", "20")                      # #7: buang chop (ADX rendah = tanpa trend)
    # --- v6: TP LADDER (TP1 @1R, TP2 @2R, scale-out + SL->BE) ---
    tp1_rr: float = _f("TP1_RR", "1.0")                       # #9: TP1 di RR 1:1
    tp2_rr: float = _f("TP2_RR", "2.0")                       # #9: TP Max di RR 1:2
    tp1_close_frac: float = _f("TP1_CLOSE_FRAC", "0.5")       # 50% ditutup di TP1
    move_sl_to_be: bool = _b("MOVE_SL_TO_BE", "true")         # setelah TP1: SL sisa -> break-even
    be_offset_pct: float = _f("BE_OFFSET_PCT", "0.0005")      # BE sedikit di atas/bawah entry (tutup fee)
    # --- v5.1: stop ATR-aware + rem SL beruntun + modul pembelajaran ---
    atr_stop_mult: float = _f("ATR_STOP_MULT", "1.0")          # stop minimal = 1.0 x ATR% (anti stop-di-dalam-noise)
    max_consec_sl: int = _i("MAX_CONSEC_SL", "3")              # rem darurat: N SL beruntun -> jeda entry
    brake_cooldown_hours: float = _f("BRAKE_COOLDOWN_HOURS", "12")
    lessons_lookback_days: int = _i("LESSONS_LOOKBACK_DAYS", "14")
    lessons_min_trades: int = _i("LESSONS_MIN_TRADES", "3")    # minimal sampel sebelum lesson dianggap valid
    max_leverage: float = _f("MAX_LEVERAGE", "10")
    min_rr: float = _f("MIN_RR", "2.0")                        # gate RR diukur ke TP2 (=tp2_rr)
    min_confidence: float = _f("MIN_CONFIDENCE", "65")
    min_stop_pct: float = _f("MIN_STOP_PCT", "0.005")
    daily_loss_limit_pct: float = _f("DAILY_LOSS_LIMIT_PCT", "0.03")
    daily_profit_target_pct: float = _f("DAILY_PROFIT_TARGET_PCT", "0.10")
    resume_hour: int = _i("RESUME_HOUR", "0")
    block_if_position_open: bool = _b("BLOCK_IF_POSITION_OPEN", "true")
    cancel_stale_entries: bool = _b("CANCEL_STALE_ENTRIES", "true")
    limit_fill_watcher: bool = _b("LIMIT_FILL_WATCHER", "true")
    watch_poll_sec: float = _f("WATCH_POLL_SEC", "5")
    position_wait_timeout_sec: float = _f("POSITION_WAIT_TIMEOUT_SEC", "6")
    position_wait_interval_sec: float = _f("POSITION_WAIT_INTERVAL_SEC", "0.4")
    verify_timeout_sec: float = _f("PROTECT_VERIFY_TIMEOUT_SEC", "3")
    verify_interval_sec: float = _f("PROTECT_VERIFY_INTERVAL_SEC", "0.5")
    leg_retry: int = _i("PROTECT_LEG_RETRY", "4")
    # v6: "auto" = coba NATIVE dulu (STOP_MARKET/TAKE_PROFIT_MARKET), fallback SINTETIS bila
    # ditolak (-4120 di testnet). "synthetic" = paksa sintetis. "native" = paksa native.
    protection_mode: str = os.getenv("PROTECTION_MODE", "auto")
    synth_poll_sec: float = _f("SYNTH_POLL_SEC", "3")
    force_market_entry: bool = _b("FORCE_MARKET_ENTRY", "false")
    dry_run: bool = _b("DRY_RUN", "true")
    loop_minutes: int = _i("LOOP_MINUTES", "15")
    notify_every_cycle: bool = _b("NOTIFY_EVERY_CYCLE", "true")
    state_file: str = os.getenv("STATE_FILE", "bot_state.json")

CONFIG = Config()
