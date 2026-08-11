"""
trading_mode.py -- AlbionBase DEMO/LIVE account switch.
================================================================================
Vendored BYTE-IDENTICAL across the 3 traders + RoundTableBase + Reporter. The mode lives in ONE file at
the AlbionBase root (trading_mode.json), shared by every system on a machine. Atomic writes; DEMO default.

DEMO/LIVE picks which Capital.com ACCOUNT the traders connect to:
  DEMO -> the Capital.com demo account (fake money, safe to test).
  LIVE -> the Capital.com live account (REAL money). Nick flips this on the RoundTableBase switch only.

SAFETY MODEL (both machines, Dell + K1 -- there is no permanent lock any more):
  * Startup ALWAYS begins in DEMO (see reset_to_demo) regardless of the file's last value.
  * Nick is the only one who flips to LIVE, via the RoundTableBase switch, behind a confirmation dialog.
  * The switch is blocked while any position is open, and refused if the live credentials are blank.
  * Any read error / missing file -> DEMO. Fail safe, always.
Cody never flips the switch. Nick controls it. Always.
"""
import csv as _csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# trading_mode.json sits at the AlbionBase root (parent of each repo dir), shared by all systems.
_MODE_FILE = Path(__file__).resolve().parent.parent / "trading_mode.json"

VALID_MODES = ("DEMO", "LIVE")


def _norm(m) -> str:
    return "LIVE" if str(m).strip().upper() == "LIVE" else "DEMO"


def read_mode() -> str:
    """Return 'DEMO' or 'LIVE' from trading_mode.json. Safe default DEMO on any error / missing file."""
    try:
        return _norm(json.loads(_MODE_FILE.read_text(encoding="utf-8")).get("mode", "DEMO"))
    except Exception:
        return "DEMO"


def write_mode(mode: str) -> bool:
    """Set the mode (atomic tmp-write + os.replace). Returns True on success. Callers (RoundTableBase) are
    responsible for the LIVE guards (live creds present, no open position) BEFORE calling this."""
    mode = _norm(mode)
    try:
        data = {"mode": mode, "switched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        tmp = _MODE_FILE.with_name("trading_mode.json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(_MODE_FILE))
        return True
    except Exception:
        return False


def reset_to_demo() -> bool:
    """Force DEMO -- called once at desk startup so the system NEVER auto-resumes LIVE after a restart
    (Part 3f). Nick manually flips back to LIVE once he has confirmed everything is running correctly."""
    return write_mode("DEMO")


def has_live_credentials() -> bool:
    """True only when THIS system's live Capital.com credentials are populated in .env (key + password).
    Used to block a LIVE switch when the live account is not configured yet (Part 4)."""
    key = os.getenv("CAPITALCOM_LIVE_KEY", "").strip()
    pwd = os.getenv("CAPITALCOM_LIVE_PASSWORD", "").strip()
    return bool(key and pwd)


def mode_label(account_type=None) -> str:
    """Header badge text. LIVE -> 'LIVE — REAL MONEY' (render RED); DEMO -> 'DEMO' (render amber)."""
    return "LIVE — REAL MONEY" if read_mode() == "LIVE" else "DEMO"


# ── P&L epoch (Stage-C Part 4c -- unchanged) ──────────────────────────────────
# Real Capital.com orders began on the Stage-B cutover (2026-08-10). Trade-CSV rows before this are the
# retired "Stanley" paper-trading simulation and are EXCLUDED from every P&L figure on the dashboards +
# reporter. The CSV files are NEVER modified, so Gaius commission analysis still sees the full history.
PNL_START_DATE = os.getenv("PNL_START_DATE", "2026-08-10").strip()


def cum_pnl_since_epoch(csv_path, date_col="date", pnl_col="pnl_gbp", start=None):
    """Sum `pnl_gbp` over trade-CSV rows dated on/after PNL_START_DATE (ISO YYYY-MM-DD compares
    lexicographically). Returns a float, 0.0 if the file is missing/unreadable. Read-only -- never writes,
    so Gaius is unaffected. This is the single honest 'cum P&L since go-live' used everywhere."""
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
