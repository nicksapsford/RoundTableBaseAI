"""
trading_mode.py -- AlbionBase PAPER/LIVE mode + the ALLOW_LIVE_TRADING safety flag.
================================================================================
Vendored BYTE-IDENTICAL across the 3 traders + RoundTableBase. The mode lives in ONE file at the
AlbionBase root (trading_mode.json), shared by every system on a machine. Atomic writes; PAPER default.

THREE-LAYER SAFETY CHAIN for LIVE (real-money) trading:
  Layer 1 -- ALLOW_LIVE_TRADING=False in .env  -> the mode can NEVER become LIVE (Dell: permanent).
  Layer 2 -- Nick physically flips the RoundTableBase switch to LIVE (K1 only).
  Layer 3 -- Capital.com 2FA on the live account.
A trader places a REAL LIVE order only when: account is LIVE  AND  mode == LIVE  AND  ALLOW_LIVE_TRADING.
A DEMO account always trades (demo is never real money), so the Dell keeps trading its demo account.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# trading_mode.json sits at the AlbionBase root (parent of each repo dir), shared by all systems.
_MODE_FILE = Path(__file__).resolve().parent.parent / "trading_mode.json"

# Layer 1 safety flag -- read once at import from THIS system's .env (Dell: always False).
ALLOW_LIVE_TRADING = os.getenv("ALLOW_LIVE_TRADING", "False").strip().lower() in ("1", "true", "yes", "on")

# Part 4c -- P&L epoch. Real Capital.com demo orders began on the Stage-B cutover (2026-08-10). Trade-CSV
# rows dated BEFORE this are the retired "Stanley" paper-trading simulation and are EXCLUDED from every
# P&L figure shown on the dashboards + reporter. The CSV files are NEVER modified, so Gaius commission
# analysis still sees the full history. Tune per machine with PNL_START_DATE in .env if needed.
PNL_START_DATE = os.getenv("PNL_START_DATE", "2026-08-10").strip()


def cum_pnl_since_epoch(csv_path, date_col="date", pnl_col="pnl_gbp", start=None):
    """Sum `pnl_gbp` over trade-CSV rows dated on/after PNL_START_DATE (ISO YYYY-MM-DD compares
    lexicographically). Returns a float, 0.0 if the file is missing/unreadable. Read-only -- never writes,
    so Gaius is unaffected. This is the single honest 'cum P&L since go-live' used everywhere."""
    import csv as _csv
    if start is None:
        start = PNL_START_DATE
    total = 0.0
    try:
        with open(str(csv_path), newline="", encoding="utf-8", errors="replace") as f:
            for r in _csv.DictReader(f):
                d = (r.get(date_col) or "").strip()
                if d and start and d < start:
                    continue
                try:
                    total += float(r.get(pnl_col))
                except (TypeError, ValueError):
                    continue
    except Exception:
        return 0.0
    return round(total, 2)


def read_mode() -> str:
    """Return 'PAPER' or 'LIVE'. Safe default PAPER on any error. If ALLOW_LIVE_TRADING is False the mode
    is FORCED to PAPER regardless of the file -- a locked machine can never read as LIVE."""
    if not ALLOW_LIVE_TRADING:
        return "PAPER"
    try:
        m = json.loads(_MODE_FILE.read_text(encoding="utf-8")).get("mode", "PAPER")
        return "LIVE" if str(m).upper() == "LIVE" else "PAPER"
    except Exception:
        return "PAPER"


def write_mode(mode: str) -> bool:
    """Set the mode (atomic tmp-write + rename). REFUSES to write LIVE unless ALLOW_LIVE_TRADING is True
    (Layer 1). PAPER is always allowed (always safe). Returns True on success."""
    mode = "LIVE" if str(mode).upper() == "LIVE" else "PAPER"
    if mode == "LIVE" and not ALLOW_LIVE_TRADING:
        return False
    try:
        data = {"mode": mode, "switched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        tmp = _MODE_FILE.with_name("trading_mode.json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_MODE_FILE))
        return True
    except Exception:
        return False


def mode_label(account_type=None):
    """Header badge text for the current mode. LIVE -> 'LIVE — REAL MONEY' (green); else 'PAPER — DEMO' (amber)."""
    return "LIVE — REAL MONEY" if read_mode() == "LIVE" else "PAPER — DEMO"
