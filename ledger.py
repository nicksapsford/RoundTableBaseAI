"""
ledger.py -- AlbionBase investment ledger (deposits & withdrawals).
================================================================================
Append-only record of every capital movement to/from the trading account, stored in ONE file at the
AlbionBase root (investment_ledger.csv), shared by every system on a machine. Vendored byte-identical to
RoundTableBase + Reporter.

  TRUE Trading P&L = Capital.com balance - net invested   (net invested = deposits - withdrawals)

This is immune to deposit/withdrawal contamination and always matches the broker exactly, unlike the
price-based per-trade estimate (which is kept in the trade CSVs for Gaius only). The file is NEVER
modified or deleted -- rows are only appended.
"""
import csv
import os
from datetime import datetime, timezone
from pathlib import Path

_LEDGER_FILE = Path(__file__).resolve().parent.parent / "investment_ledger.csv"
HEADERS = ["date", "time", "type", "amount_gbp", "notes", "running_total"]


def _read_rows():
    rows = []
    try:
        with open(_LEDGER_FILE, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    except Exception:
        pass
    return rows


def seed_if_missing(amount, notes="Initial capital (seed -- edit to your real net invested)",
                    date=None, time=None):
    """Create investment_ledger.csv with one opening DEPOSIT row if it does not exist. Returns True if it
    seeded, False if the file was already there. Never overwrites an existing ledger."""
    if _LEDGER_FILE.exists():
        return False
    now = datetime.now(timezone.utc)
    d = date or now.strftime("%Y-%m-%d")
    t = time or now.strftime("%H:%M")
    try:
        amt = round(abs(float(amount)), 2)
        with open(_LEDGER_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            w.writeheader()
            w.writerow({"date": d, "time": t, "type": "DEPOSIT", "amount_gbp": "%.2f" % amt,
                        "notes": notes, "running_total": "%.2f" % amt})
        return True
    except Exception:
        return False


def totals():
    """{'deposited', 'withdrawn', 'net_invested'} across the whole ledger."""
    dep = wd = 0.0
    for r in _read_rows():
        try:
            a = abs(float(r.get("amount_gbp") or 0))
        except (TypeError, ValueError):
            a = 0.0
        if (r.get("type") or "").strip().upper() == "WITHDRAWAL":
            wd += a
        else:
            dep += a
    return {"deposited": round(dep, 2), "withdrawn": round(wd, 2), "net_invested": round(dep - wd, 2)}


def net_invested():
    return totals()["net_invested"]


def append(txn_type, amount, notes=""):
    """Append one DEPOSIT or WITHDRAWAL. Returns the written row dict, or None on bad input / write error.
    running_total = net invested AFTER this row (deposits add, withdrawals subtract)."""
    txn_type = "WITHDRAWAL" if str(txn_type).strip().upper() == "WITHDRAWAL" else "DEPOSIT"
    try:
        amt = round(float(amount), 2)
    except (TypeError, ValueError):
        return None
    if amt <= 0:          # reject zero/negative -- the sign is carried by txn_type, amounts are positive
        return None
    running = round(net_invested() + (amt if txn_type == "DEPOSIT" else -amt), 2)
    now = datetime.now(timezone.utc)
    row = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "type": txn_type,
           "amount_gbp": "%.2f" % amt, "notes": (notes or "").replace("\n", " ")[:200],
           "running_total": "%.2f" % running}
    try:
        new = not _LEDGER_FILE.exists()
        with open(_LEDGER_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADERS)
            if new:
                w.writeheader()
            w.writerow(row)
        return row
    except Exception:
        return None


def trading_pnl(balance):
    """TRUE trading P&L = Capital.com balance - net invested. None if balance is unknown."""
    if balance is None:
        return None
    try:
        return round(float(balance) - net_invested(), 2)
    except (TypeError, ValueError):
        return None


def summary(balance=None):
    """Everything the dashboards need in one call."""
    t = totals()
    t["balance"] = (round(float(balance), 2) if balance is not None else None)
    t["trading_pnl"] = trading_pnl(balance)
    return t
