"""
dashboard_roundtablebase.py -- RoundTableBase A.I. (AlbionBase Desk)
================================================================================
Command centre for the AlbionBase promotion desk. Port 5036.

  * Reads the 3 AlbionBase systems -- OilBase 5035, GoldBase 5033,
    USBase 5034 (shows only the ones that are up; offline systems render gracefully).
  * Reads the live AI traders on ports 5002-5005 purely for the P&L comparison
    (AlbionBase mechanical vs full-AI Albion, apples-to-apples per instrument).
  * Each system's WITH/AGAINST direction switch is shown and controllable here
    (proxied live to that system -- no restart).

All times UTC. Reads other systems only; owns no trading state.
"""
import json
import logging
import os
import signal
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request
from trading_mode import read_mode, write_mode, reset_to_demo, PNL_START_DATE
import ledger

BASE_DIR = Path(__file__).resolve().parent
_VER = BASE_DIR / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"
PORT = 5036
FETCH_TIMEOUT = 2.5

# ── Monitoring separation (Part 2, .env-driven; each PC shows only its OWN systems) ──
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass
ENV_LABEL       = os.getenv("ENV_LABEL", "TEST").strip().upper()         # TEST (Dell, amber) / LIVE (K1, green)
ALBIONBASE_HOST = os.getenv("ALBIONBASE_HOST", "localhost")              # this PC's own AlbionBase systems
# The main-desk comparison (5002-5005) + Gaius (:5012) live on the DELL. Empty -> local (this IS the Dell)
# so localhost is correct; set to the Dell Tailscale IP on the K1. Unreachable -> renders "N/A" gracefully.
ALBIONBASE_DELL_HOST = (os.getenv("ALBIONBASE_DELL_HOST", "").strip() or "localhost")

if ENV_LABEL == "LIVE":
    _ENV_BADGE = ('<span style="background:#12331b;color:#3fb950;border:1px solid #2ea043;border-radius:5px;'
                  'padding:3px 11px;font-size:12px;font-weight:700;letter-spacing:1px;">LIVE &mdash; K1</span>')
else:
    _ENV_BADGE = ('<span style="background:#3a2f00;color:#e0b020;border:1px solid #6b5600;border-radius:5px;'
                  'padding:3px 11px;font-size:12px;font-weight:700;letter-spacing:1px;">TEST &mdash; Dell</span>')

PUSHOVER_USER_KEY  = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "").strip()
LIVE_NOTIFICATIONS = os.getenv("LIVE_NOTIFICATIONS", "False").strip().lower() in ("1", "true", "yes", "on")

logging.basicConfig(level=logging.WARNING)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("RoundTableBase")
app = Flask(__name__)

# Part 3f: the desk ALWAYS starts in DEMO -- never auto-resume LIVE after a restart. Nick manually flips
# back to LIVE once he has confirmed everything is running correctly. RoundTableBase owns the switch file.
try:
    reset_to_demo()
    log.warning("Startup: trading mode forced to DEMO (Part 3f).")
except Exception as _e:
    log.warning("Startup DEMO reset failed: %s", _e)

# Part 3b: seed the investment ledger on first run (no-op if it already exists). Default = the go-live
# deposit placeholder; Nick edits the amount/date to his real first deposit (or, on demo, his funded amount).
try:
    if ledger.seed_if_missing(1000, notes="Initial go-live capital (seed -- edit to your real deposit)"):
        log.warning("Seeded investment_ledger.csv (edit the opening row to your real net invested).")
except Exception as _e:
    log.warning("Ledger seed failed: %s", _e)


def _pushover_send(title, message, priority=1):
    """Percival alert via Pushover. Gated by LIVE_NOTIFICATIONS + trading_mode==LIVE (K1 live box only)."""
    if not LIVE_NOTIFICATIONS:
        return
    try:                                  # STANDING RULE: Pushover ONLY in LIVE mode; DEMO/unknown = silent
        if read_mode() != "LIVE":
            return
    except Exception:
        return
    if not (PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN):
        log.warning("Pushover not configured -- skipping alert: %s", message)
        return
    try:
        import urllib.parse
        data = urllib.parse.urlencode({"token": PUSHOVER_API_TOKEN, "user": PUSHOVER_USER_KEY,
                                       "title": title, "message": message, "priority": priority}).encode()
        urllib.request.urlopen("https://api.pushover.net/1/messages.json", data=data, timeout=6)
    except Exception as exc:
        log.warning("Pushover send failed: %s", exc)


def _percival_mode_alert(mode):
    """Fire a High-priority Pushover on every DEMO/LIVE change (always High -- Part 4d)."""
    if mode == "LIVE":
        _pushover_send("AlbionBase switched to LIVE 🔴",
                       "Real money active on Capital.com live account\nAll 3 systems now trading live", priority=1)
    else:
        _pushover_send("AlbionBase switched to DEMO 🟡",
                       "Demo mode active\nAll 3 systems now trading demo account", priority=1)

# AlbionBase systems (standalone desk -- no cross-desk comparison, Part 4a). 3 instruments: Gold/Oil/US500.
SYSTEMS = [
    {"key": "oil",  "name": "OilBase",  "market": "Brent Crude", "port": 5035, "start": 1000.0, "colour": "#FF6600"},
    {"key": "gold", "name": "GoldBase", "market": "GOLD (XAU)",  "port": 5033, "start": 1000.0, "colour": "#FFD700"},
    {"key": "us",   "name": "USBase",   "market": "S&P 500",     "port": 5034, "start": 1000.0, "colour": "#FFFFFF"},
    # FTSEBase REMOVED 11 Aug 2026 -- £2/pt on UK100 (~£21.7k notional, ~£1,080 margin) exceeds the
    # £3,000 pot at 2% risk. AlbionBase runs 3 instruments: Gold, Oil, US500. FTSEBaseAI repo archived.
]
SYS_BY_KEY = {s["key"]: s for s in SYSTEMS}

# SHUTDOWN ALL targets: every base system (5022-5026 + 5028). NikkeiBase
# is a base too, so it is included. RoundTableBase (5030) then shuts itself.
# (CryptoBase 5021 was mothballed 29 Jul 2026.)
SHUTDOWN_PORTS = [s["port"] for s in SYSTEMS]
LOG_DIR = BASE_DIR / "logs"
SHUTDOWN_FLAG = LOG_DIR / "shutdown.flag"


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_json(url, timeout=FETCH_TIMEOUT, method="GET", data=None):
    try:
        headers = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _fetch_one(cfg):
    """Return a uniform row for one base system + its original counterpart."""
    row = {"key": cfg["key"], "name": cfg["name"], "market": cfg["market"],
           "port": cfg["port"], "colour": cfg["colour"], "start": cfg["start"],
           "online": False, "mode": None, "price": None, "position": None,
           "floating_gbp": 0.0, "locked": None, "today_pnl": None, "balance": None,
           "cum_pnl": None, "lancelot": "--", "session": "24/7" if cfg["key"] == "crypto" else "--",
           "market": None, "acct_bal": None, "acct_type": None, "data_only": False}

    st = _get_json("http://%s:%d/api/state" % (ALBIONBASE_HOST, cfg["port"]))
    if st and (st.get("portfolio") or st.get("balance") is not None):
        row["online"] = True
        port = st.get("portfolio") or {}
        bal = _fnum(port.get("balance"))
        if bal is None:
            bal = _fnum(st.get("balance"))
        today = _fnum(port.get("today_pnl"))
        if today is None:
            today = _fnum(st.get("today_pnl"))
        row["balance"] = bal
        row["today_pnl"] = today if today is not None else 0.0
        row["floating_gbp"] = _fnum(port.get("floating_gbp")) or 0.0
        # Locked profit (Profit Protection Ladder floor). Crypto sums its two legs.
        if cfg["key"] == "crypto":
            lk = 0.0
            for leg in (st.get("btc"), st.get("eth")):
                if isinstance(leg, dict):
                    lk += _fnum(leg.get("locked_gbp")) or 0.0
            row["locked"] = round(lk, 2) if lk else None
        else:
            row["locked"] = _fnum(st.get("locked_gbp"))
        row["mode"] = st.get("mode")
        row["data_only"] = bool(st.get("data_only"))          # DEMOTED -> amber "data only" badge (no real orders)
        row["acct_bal"] = _fnum(st.get("account_balance"))    # Part 3: shared Capital.com pot (read-only)
        row["acct_type"] = st.get("account_type")
        # Risk/trade is RISK_PCT of the FIXED NOTIONAL_CAPITAL (£60 on £3,000) as the trader reports it --
        # NOT 2% of the real Capital.com balance (that showed £611 against the ~£30k demo pot).
        row["risk_per_trade"] = _fnum(st.get("risk_per_trade"))
        row["session"] = st.get("session") or row["session"]
        row["market"] = st.get("market")   # Part 4d: {in_session, tradeable, hours} for the status dot
        row["live_configured"] = st.get("live_configured")   # DEMO/LIVE: are this system's live creds set?
        # Part 4c: cum P&L = real Capital.com orders only. Each system now reports cum_pnl computed from
        # its own trade CSV since the Stage-B epoch (Stanley paper-era excluded). Fall back to
        # balance-minus-start for any system that predates the field.
        cum = _fnum(st.get("cum_pnl"))
        if cum is not None:
            row["cum_pnl"] = round(cum, 2)
        elif bal is not None:
            row["cum_pnl"] = round(bal - cfg["start"], 2)
        # A compact position/lancelot summary (crypto has two instruments)
        legs = [st.get("btc"), st.get("eth")] if cfg["key"] == "crypto" else [st]
        pos_bits, lanc_bits = [], []
        _short = cfg["name"].replace("Base", "")   # GoldBase -> Gold (Part 4b: label was blank on non-crypto)
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            if leg.get("in_trade") and leg.get("position"):
                pos_bits.append("%s %s" % (leg.get("label") or _short, leg["position"].get("direction", "")))
            lanc_bits.append(str(leg.get("lancelot", "")))
        row["position"] = ", ".join(b for b in pos_bits if b.strip()) or "FLAT"
        row["lancelot"] = " / ".join(b for b in lanc_bits if b) or "--"
        # Part 4a: entry/stop/target + distance-to-stop / distance-to-TP for the open position (single-instrument).
        row["pos_detail"] = None
        if st.get("in_trade") and isinstance(st.get("position"), dict):
            _p = st["position"]; _price = _fnum(st.get("price"))
            def _dist(a, b):
                try: return round(abs(float(a) - float(b)), 1)
                except (TypeError, ValueError): return None
            row["pos_detail"] = {"entry": _p.get("entry"), "stop": _p.get("stop"), "target": _p.get("target"),
                                 "to_stop": _dist(_price, _p.get("stop")), "to_tp": _dist(_price, _p.get("target"))}
        row["price"] = (st.get("btc") or {}).get("price") if cfg["key"] == "crypto" else _fnum(st.get("price"))

    # Part 2: PAPER/LIVE trading mode is a desk-wide master (trading_mode.json), not per-system.
    return row


def _gather():
    with ThreadPoolExecutor(max_workers=len(SYSTEMS)) as ex:
        rows = list(ex.map(_fetch_one, SYSTEMS))
    online = [r for r in rows if r["online"]]
    # AlbionBase is a standalone desk -- its only measure is its own pot growth (Part 4a: no cross-desk
    # comparison). cum P&L sums each online system's real-orders-only figure (Part 4c).
    bench_val = sum((r["balance"] or 0.0) for r in online)
    bench_today = sum((r["today_pnl"] or 0.0) for r in online)
    bench_cum = sum((r["cum_pnl"] or 0.0) for r in online)
    # Part 3: TOTAL POT = the real (shared) Capital.com balance -- every system reports the same figure,
    # so take the first non-null. Read-only.
    pots = [r["acct_bal"] for r in online if r.get("acct_bal") is not None]
    total_pot = pots[0] if pots else None
    atypes = [r["acct_type"] for r in online if r.get("acct_type")]
    account_type = (atypes[0] if atypes else "DEMO")
    # Risk/trade = each trader's reported figure (RISK_PCT of the FIXED £3,000 notional = £60), NOT 2% of
    # the real balance -- that showed £611 against the ~£30k demo pot; sizing never uses the real balance.
    risks = [r.get("risk_per_trade") for r in online if r.get("risk_per_trade") is not None]
    risk_pt = risks[0] if risks else None
    return {
        "systems": rows,
        "online_count": len(online),
        "total_count": len(SYSTEMS),
        "pot": {"total": total_pot, "account_type": account_type, "risk": risk_pt},
        "base": {"value": round(bench_val, 2), "today_pnl": round(bench_today, 2),
                      "cum_pnl": round(bench_cum, 2)},
        "pnl_start": PNL_START_DATE,
        "version": APP_VERSION,
        "updated_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


def _g(v):
    """Format a GBP amount with sign, or '--' when missing."""
    if v is None:
        return "--"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    return ("+" if v >= 0 else "-") + "GBP %.2f" % abs(v)


def build_base_brief(g):
    """Plain-text Archie brief for the Base RoundTable (matches original RoundTable format)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bar = "=" * 60
    b = g["base"]
    lines = []
    lines.append(bar)
    lines.append("ARCHIE BRIEF -- BASE ROUNDTABLE")
    lines.append("Generated: %s UTC" % ts)
    lines.append(bar)
    lines.append("")
    lines.append("PORTFOLIO")
    _pot = g.get("pot") or {}
    if _pot.get("total") is not None:
        lines.append("  TOTAL POT       : GBP %.2f (%s, read-only) | risk/trade GBP %.2f"
                     % (_pot["total"], _pot.get("account_type", "DEMO"), _pot.get("risk") or 0.0))
    lines.append("  AlbionBase Total: GBP %.2f" % b["value"])
    lines.append("  Cum P&L         : %s (real orders since %s)" % (_g(b["cum_pnl"]), g.get("pnl_start", "go-live")))
    lines.append("  Today           : %s" % _g(b["today_pnl"]))
    lines.append("  Systems online  : %d / %d" % (g["online_count"], g["total_count"]))
    lines.append("")
    lines.append(bar)
    lines.append("SYSTEMS")
    lines.append(bar)
    open_positions = []
    for s in g["systems"]:
        if not s["online"]:
            lines.append("")
            lines.append("%s [:%d]  OFFLINE -- awaiting build / launch" % (s["name"], s["port"]))
            continue
        price = ("%.2f" % s["price"]) if s.get("price") is not None else "--"
        pos = s.get("position") or "FLAT"
        lines.append("")
        lines.append("%s [:%d]  ONLINE  price %s  %s" % (s["name"], s["port"], price, pos))
        lines.append("    locked %s | today %s | cum %s | bal %s | lancelot %s | floating %s"
                     % (_g(s.get("locked")), _g(s.get("today_pnl")), _g(s.get("cum_pnl")),
                        ("GBP %.2f" % s["balance"]) if s.get("balance") is not None else "--",
                        s.get("lancelot") or "--", _g(s.get("floating_gbp"))))
        if pos not in ("FLAT", "--", ""):
            open_positions.append("  %s: %s (floating %s)" % (s["name"], pos, _g(s.get("floating_gbp"))))
    lines.append("")
    lines.append(bar)
    lines.append("OPEN POSITIONS")
    lines.append(bar)
    if open_positions:
        lines.extend(open_positions)
    else:
        lines.append("  No open positions")
    lines.append("")
    lines.append(bar)
    lines.append("End of Base RoundTable Archie Brief")
    lines.append(bar)
    return "\n".join(lines)


@app.route("/api/archie-brief")
def api_archie_brief():
    return build_base_brief(_gather()), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/systems")
def api_systems():
    return Response(json.dumps(_gather(), default=str), mimetype="application/json")


def _switch_state():
    """(mode, live_configured, position_open). live_configured = every online trader has live creds;
    position_open = any online system currently holds a position."""
    g = _gather()
    online = [s for s in g["systems"] if s.get("online")]
    pos_open = any((s.get("position") or "FLAT") not in ("FLAT", "--", "") for s in online)
    flags = [s.get("live_configured") for s in online if s.get("live_configured") is not None]
    live_configured = bool(flags) and all(flags)
    return read_mode(), live_configured, pos_open


@app.route("/api/trading-mode", methods=["GET", "POST"])
def api_trading_mode():
    """DEMO/LIVE account switch. GET -> {mode, live_configured, position_open}. POST {mode:'DEMO'|'LIVE'}
    moves ALL systems together. Guards (Nick-only, behind a UI confirmation):
      * Part 3d -- REFUSED (409) while any position is open (never one account opening, another closing).
      * Part 4  -- LIVE REFUSED (403) when the live credentials are blank ('not configured').
      * Part 3e -- a Percival Pushover alert fires on every successful change."""
    mode, live_configured, pos_open = _switch_state()
    if request.method == "GET":
        return jsonify({"mode": mode, "live_configured": live_configured, "position_open": pos_open})
    body = request.get_json(force=True, silent=True) or {}
    want = str(body.get("mode", "")).strip().upper()
    if want not in ("DEMO", "LIVE"):
        return jsonify({"error": "mode must be DEMO or LIVE"}), 400
    if want == mode:
        return jsonify({"mode": mode, "live_configured": live_configured, "position_open": pos_open, "ok": True})
    if pos_open:
        log.warning("DEMO/LIVE switch to %s REFUSED -- a position is open.", want)
        return jsonify({"error": "A position is open. Wait for it to close before switching modes.",
                        "mode": mode, "live_configured": live_configured, "position_open": True}), 409
    if want == "LIVE" and not live_configured:
        log.warning("DEMO/LIVE switch to LIVE REFUSED -- live credentials not configured.")
        return jsonify({"error": "Live account not configured. Please add live credentials to .env first.",
                        "mode": mode, "live_configured": False, "position_open": False}), 403
    ok = write_mode(want)
    log.warning("DEMO/LIVE switch -> %s (by Nick) | written=%s", want, ok)
    if ok:
        _percival_mode_alert(want)
    return jsonify({"mode": read_mode(), "live_configured": live_configured, "position_open": pos_open, "ok": ok})


def _capital_balance():
    """The real (shared) Capital.com balance = TOTAL POT, from any online system's report. None if all offline."""
    g = _gather()
    pots = [s.get("acct_bal") for s in g["systems"] if s.get("online") and s.get("acct_bal") is not None]
    return pots[0] if pots else None


@app.route("/api/ledger", methods=["GET"])
def api_ledger():
    """Investment-ledger summary + the TRUE Trading P&L (= Capital.com balance - net invested)."""
    return jsonify(ledger.summary(_capital_balance()))


@app.route("/api/ledger/deposit", methods=["POST"])
def api_ledger_deposit():
    body = request.get_json(force=True, silent=True) or {}
    row = ledger.append("DEPOSIT", body.get("amount"), body.get("notes", ""))
    if not row:
        return jsonify({"error": "Invalid amount -- enter a positive number."}), 400
    log.warning("LEDGER deposit +£%s (%s)", row["amount_gbp"], row.get("notes", ""))
    return jsonify({"ok": True, "row": row, "summary": ledger.summary(_capital_balance())})


@app.route("/api/ledger/withdraw", methods=["POST"])
def api_ledger_withdraw():
    body = request.get_json(force=True, silent=True) or {}
    row = ledger.append("WITHDRAWAL", body.get("amount"), body.get("notes", ""))
    if not row:
        return jsonify({"error": "Invalid amount -- enter a positive number."}), 400
    log.warning("LEDGER withdrawal -£%s (%s)", row["amount_gbp"], row.get("notes", ""))
    return jsonify({"ok": True, "row": row, "summary": ledger.summary(_capital_balance())})


@app.route("/api/shutdown-all", methods=["POST"])
def api_shutdown_all():
    """SHUTDOWN ALL -- mirror the original RoundTable mechanism: POST
    /api/shutdown to each child port (all base systems 5022-5026 + 5028), then
    shut RoundTableBase (5030) itself down. A child that is offline or lacks the
    endpoint is reported but does not block the rest."""
    results = {}
    for port in SHUTDOWN_PORTS:
        url = "http://%s:%d/api/shutdown" % (ALBIONBASE_HOST, port)
        try:
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT):
                pass
            results[str(port)] = "shutdown sent"
        except Exception as exc:
            results[str(port)] = "error: %s" % exc
    log.warning("SHUTDOWN ALL activated -- shutdown sent to base ports %s; self-shutdown follows.",
                ", ".join(str(p) for p in SHUTDOWN_PORTS))

    # Self-shutdown 5030: leave a shutdown flag (for any supervisor) then SIGTERM self.
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SHUTDOWN_FLAG.write_text("shutdown requested %s\n" % datetime.now(timezone.utc).isoformat(),
                                 encoding="utf-8")
    except Exception as exc:
        log.warning("Could not write shutdown flag: %s", exc)

    def _kill():
        time.sleep(1.0)   # allow the HTTP response to flush first
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_kill, daemon=True).start()

    return jsonify({"status": "shutting_down", "targets": SHUTDOWN_PORTS, "results": results})


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>RoundTableBase</title>
<style>
:root{--bg:#0d0d0f;--bg2:#16161a;--bd:#2a2a30;--tx:#e6edf3;--mut:#8b949e;
--gold:#c8a24a;--green:#3fb950;--red:#f85149;--teal:#00b4d8;}
*{box-sizing:border-box;} body{margin:0;background:var(--bg);color:var(--tx);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;}
header{background:var(--bg2);border-bottom:2px solid var(--gold);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;}
.brand{font-size:19px;font-weight:800;letter-spacing:1px;} .brand .cap{color:var(--gold);}
.brand small{color:var(--mut);font-size:11px;font-weight:400;letter-spacing:0;margin-left:8px;}
.clock{font-family:monospace;color:var(--gold);font-weight:700;}
.wrap{max-width:1180px;margin:0 auto;padding:18px;}
.cmp{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:18px;}
.cmp .box{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;text-align:center;}
.cmp .box .lbl{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;}
.cmp .box .big{font-size:24px;font-weight:800;margin-top:4px;}
.cmp .box .sub{font-size:11px;color:var(--mut);margin-top:3px;}
.cmp .verdict{grid-column:1/-1;text-align:center;font-size:15px;font-weight:700;padding:10px;border-radius:8px;background:var(--bg2);border:1px solid var(--bd);}
table{width:100%;border-collapse:collapse;font-size:12px;}
th{text-align:left;color:var(--mut);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--bd);white-space:nowrap;}
td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,0.05);white-space:nowrap;}
tr.offline td{color:#555;}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;}
.dot.on{background:var(--green);} .dot.off{background:#555;}
.sw{display:inline-flex;gap:4px;}
.sw button{font-size:10px;font-weight:700;padding:3px 9px;border-radius:5px;cursor:pointer;background:#1e1e1e;color:#888;border:1px solid #444;}
.sw button.on-WITH{background:rgba(63,185,80,0.20);color:var(--green);border-color:var(--green);}
.sw button.on-AGAINST{background:rgba(248,81,73,0.22);color:var(--red);border-color:var(--red);}
.bull{color:var(--green);} .bear{color:var(--red);} .mut{color:var(--mut);}
.note{color:var(--mut);font-size:10px;margin-top:14px;text-align:center;line-height:1.5;}
</style></head><body>
<header>
  <div class="brand">&#127942; <span class="cap">Base RoundTable</span>
    <small>__VER__ &middot; port 5036 &middot; pure Lancelot + SSL, no AI overlay</small></div>
  <div style="display:flex;align-items:center;gap:12px;">
    __ENV__
    <span id="modeLabel" style="padding:3px 10px;border-radius:5px;font-weight:700;font-size:12px;letter-spacing:0.5px;background:#3a2f00;color:#e0b020;border:1px solid #6b5600;">DEMO</span>
    <span id="modeSw" title="Click to switch account" style="padding:5px 13px;border-radius:5px;font-weight:800;font-size:12px;letter-spacing:0.5px;background:#3a2f00;color:#e0b020;border:1px solid #6b5600;cursor:pointer;">DEMO &#8594; click for LIVE</span>
    <button onclick="shutdownAll()" title="Shut down all base systems + this RoundTable"
      style="font-size:12px;font-weight:700;color:#e74c3c;background:rgba(231,76,60,0.15);border:1px solid rgba(231,76,60,0.6);padding:4px 11px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#9211; SHUTDOWN ALL</button>
    <div class="clock" id="clock">--:--:-- UTC</div>
  </div>
</header>
<div class="wrap">
  <div id="potbar" style="display:flex;gap:26px;align-items:center;flex-wrap:wrap;background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:12px 18px;margin-bottom:14px;"></div>
  <div id="ledgerCard" style="background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px 18px;margin-bottom:14px;"></div>
  <div class="cmp" id="cmp"></div>
  <table><thead><tr>
    <th>System</th><th>Status</th><th>Price</th><th>Position</th><th>Floating</th>
    <th>Locked</th><th>Today</th><th>Contribution</th><th>Lancelot</th><th>Session</th>
  </tr></thead><tbody id="rows"></tbody></table>
  <div class="note">Trading P&amp;L (above) = Capital.com balance &minus; net invested &mdash; the true figure, matching the broker exactly.
    Per-system <b>Contribution</b> is a price-based estimate (points &times; stake) kept for Gaius, not the account truth. DEMO/LIVE is the master switch above.</div>
</div>
<script>
function clk(){var t=new Date();document.getElementById('clock').textContent=
  String(t.getUTCHours()).padStart(2,'0')+':'+String(t.getUTCMinutes()).padStart(2,'0')+':'+String(t.getUTCSeconds()).padStart(2,'0')+' UTC';}
setInterval(clk,1000);clk();
function money(v){if(v===null||v===undefined)return '--';var n=Number(v);return (n<0?'-£':'+£')+Math.abs(n).toFixed(2);}
function bal(v){return v===null||v===undefined?'--':'£'+Number(v).toFixed(2);}
function cls(v){return Number(v)>=0?'bull':'bear';}
function renderMode(d){
  var m=(d&&d.mode)||'DEMO', live=(m==='LIVE'), posOpen=!!(d&&d.position_open);
  var lbl=document.getElementById('modeLabel'), sw=document.getElementById('modeSw');
  var redCss='background:#3d0f0f;color:#ff5555;border:1px solid #b02020;';
  var amberCss='background:#3a2f00;color:#e0b020;border:1px solid #6b5600;';
  if(lbl){ lbl.innerHTML=live?'LIVE &mdash; REAL MONEY':'DEMO';
    lbl.style.cssText='padding:3px 10px;border-radius:5px;font-weight:700;font-size:12px;letter-spacing:0.5px;'+(live?redCss:amberCss); }
  if(sw){
    var base='padding:5px 13px;border-radius:5px;font-weight:800;font-size:12px;letter-spacing:0.5px;';
    if(posOpen){
      sw.innerHTML=(live?'LIVE':'DEMO')+' &#128274;'; sw.title='A position is open -- wait for it to close before switching modes';
      sw.style.cssText=base+'cursor:not-allowed;opacity:0.6;'+(live?redCss:amberCss); sw.onclick=null;
    } else {
      sw.innerHTML=live?'LIVE &mdash; REAL MONEY &#8594; click for DEMO':'DEMO &#8594; click for LIVE';
      sw.title=live?'Click to switch back to DEMO':'Click to switch to LIVE (real money)';
      sw.style.cssText=base+'cursor:pointer;'+(live?redCss:amberCss); sw.onclick=flipMode;
    }
  }
}
function posDetail(s){
  // Part 4a: entry, stop, and how far price is from the stop / take-profit (so Nick can see protection).
  var d=s.pos_detail; if(!d)return '';
  var bits=[];
  if(d.entry!=null) bits.push('@'+d.entry);
  if(d.stop!=null) bits.push('stop '+d.stop+(d.to_stop!=null?' ('+d.to_stop+'&rarr;stop)':''));
  if(d.to_tp!=null) bits.push(d.to_tp+'&rarr;TP');
  return bits.length ? '<div class="mut" style="font-size:10px;line-height:1.4;">'+bits.join(' &middot; ')+'</div>' : '';
}
function sessionCell(s){
  // Part 4d: coloured dot -- green=in session+tradeable, amber=in session but market temporarily closed,
  // red=out of session, grey=offline/unknown. Reads the engine's live market-status pre-check.
  var m=s.market||{}; var hours=m.hours||'--'; var color, title;
  if(!s.online){ color='#6e7681'; title='offline'; }
  else if(m.in_session===false){ color='#f85149'; title='out of session -- no trading'; }
  else if(m.tradeable===false){ color='#d29922'; title='in session, market temporarily closed'; }
  else if(m.in_session===true){ color='#3fb950'; title='in session, tradeable'; }
  else { color='#6e7681'; title='market status unknown'; }
  return '<span title="'+title+'" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:'+color+';margin-right:6px;vertical-align:middle;"></span>'+
         '<span class="mut">'+hours+'</span>';
}
function pollMode(){ fetch('/api/trading-mode').then(function(r){return r.json();}).then(renderMode).catch(function(){}); }
function flipMode(){
  fetch('/api/trading-mode').then(function(r){return r.json();}).then(function(d){
    if(d.position_open){ alert('A position is open. Wait for it to close before switching modes.'); return; }
    var target=(d.mode==='LIVE')?'DEMO':'LIVE';
    if(target==='LIVE'){
      if(!d.live_configured){ alert('Live account not configured. Please add live credentials to .env first.'); return; }
      if(!confirm('Switch to LIVE? This uses REAL MONEY on your live account. Are you sure?')) return;
    } else {
      if(!confirm('Switch back to DEMO?')) return;
    }
    fetch('/api/trading-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:target})})
      .then(function(r){return r.json().then(function(j){return {status:r.status,j:j};});})
      .then(function(res){ if(res.status>=400 && res.j && res.j.error){ alert(res.j.error); } renderMode(res.j); })
      .catch(function(){});
  });
}
function poll(){
  fetch('/api/systems').then(function(r){return r.json();}).then(function(d){
    var b=d.base;
    // AlbionBase is a standalone desk -- it measures itself by its own pot growth, not vs other desks.
    document.getElementById('cmp').innerHTML=
      '<div class="box"><div class="lbl">Systems Contribution</div><div class="big '+cls(b.cum_pnl)+'">'+money(b.cum_pnl)+'</div><div class="sub">price estimate (Gaius) &mdash; not the account truth</div></div>'+
      '<div class="box"><div class="lbl">Today (est.)</div><div class="big '+cls(b.today_pnl)+'">'+money(b.today_pnl)+'</div><div class="sub">all 3 systems</div></div>'+
      '<div class="box"><div class="lbl">Systems Online</div><div class="big">'+d.online_count+' / '+d.total_count+'</div><div class="sub">updated '+(d.updated_utc||'--')+' UTC</div></div>';
    var p=d.pot||{};
    var live=(p.account_type==='LIVE');
    var tBg=live?'#12331b':'#26262b',tFg=live?'#3fb950':'#8b949e',tBd=live?'#2ea043':'#444c56';
    document.getElementById('potbar').innerHTML=
      '<div><div style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">Total Pot</div>'+
      '<div style="font-size:22px;font-weight:800;">'+(p.total!=null?bal(p.total):'--')+'</div></div>'+
      '<div><div style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">Account</div>'+
      '<div style="margin-top:4px;"><span style="background:'+tBg+';color:'+tFg+';border:1px solid '+tBd+';border-radius:4px;padding:2px 9px;font-weight:700;letter-spacing:1px;">'+(p.account_type||'DEMO')+'</span></div></div>'+
      '<div><div style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">Risk / Trade (2%)</div>'+
      '<div style="font-size:22px;font-weight:800;">'+(p.risk!=null?bal(p.risk):'--')+'</div></div>'+
      '<div class="mut" style="font-size:11px;max-width:300px;line-height:1.5;">Read live from the Capital.com account (read-only). Per-system paper balances retired &mdash; per-system P&amp;L is in the table.</div>';
    var h='';
    for(var i=0;i<d.systems.length;i++){var s=d.systems[i];
      if(!s.online){
        h+='<tr class="offline"><td>'+s.name+' <span class="mut">:'+s.port+'</span></td>'+
           '<td><span class="dot off"></span>not online</td><td colspan="8" class="mut">awaiting build / launch</td></tr>';
        continue;
      }
      var demoted=s.data_only?' <span style="background:#3a2f00;color:#e0b020;border:1px solid #6b5600;border-radius:4px;padding:1px 5px;font-size:10px;font-weight:700;white-space:nowrap;" title="Signal has no demonstrated edge -- Lancelot still runs and logs, but NO real orders are placed">DEMOTED &mdash; data only</span>':'';
      h+='<tr><td><span style="color:'+s.colour+'">&#9632;</span> '+s.name+demoted+' <span class="mut">:'+s.port+'</span></td>'+
        '<td><span class="dot on"></span>online</td>'+
        '<td>'+(s.price!=null?'£'+Number(s.price).toLocaleString('en-GB',{maximumFractionDigits:2}):'--')+'</td>'+
        '<td>'+(s.position||'--')+posDetail(s)+'</td>'+
        '<td class="'+cls(s.floating_gbp)+'">'+money(s.floating_gbp)+'</td>'+
        '<td class="'+(s.locked!=null&&Number(s.locked)>0?'green':'mut')+'">'+(s.locked!=null&&Number(s.locked)>0?'&#128274; +£'+Number(s.locked).toFixed(2):'--')+'</td>'+
        '<td class="'+cls(s.today_pnl)+'">'+money(s.today_pnl)+'</td>'+
        '<td class="'+cls(s.cum_pnl)+'">'+money(s.cum_pnl)+'</td>'+
        '<td class="mut" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;">'+(s.lancelot||'--')+'</td>'+
        '<td>'+sessionCell(s)+'</td></tr>';
    }
    document.getElementById('rows').innerHTML=h;
  }).catch(function(e){});
}
poll();setInterval(poll,7000);
pollMode();setInterval(pollMode,5000);
/* ── Investment ledger (Part 3): true Trading P&L = Capital.com balance - net invested ── */
function _lkv(label,val){return '<div><div style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">'+label+'</div><div style="font-size:18px;font-weight:800;">'+val+'</div></div>';}
function renderLedger(d){
  d=d||{}; var el=document.getElementById('ledgerCard'); if(!el)return; var tp=d.trading_pnl;
  var btn='font-weight:700;font-size:12px;border-radius:6px;padding:6px 12px;cursor:pointer;margin-left:8px;';
  el.innerHTML=
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">'+
      '<div style="font-size:12px;font-weight:800;letter-spacing:1px;color:var(--mut);text-transform:uppercase;">Investment Ledger</div>'+
      '<div><button onclick="addDeposit()" style="'+btn+'background:#12331b;color:#3fb950;border:1px solid #2ea043;">+ Add deposit</button>'+
      '<button onclick="recordWithdrawal()" style="'+btn+'background:#3a2f00;color:#e0b020;border:1px solid #6b5600;">&minus; Record withdrawal</button></div>'+
    '</div>'+
    '<div style="display:flex;gap:26px;flex-wrap:wrap;margin-top:12px;align-items:flex-end;">'+
      _lkv('Total deposited', d.deposited!=null?bal(d.deposited):'--')+
      _lkv('Total withdrawn', d.withdrawn!=null?bal(d.withdrawn):'--')+
      _lkv('Net invested', d.net_invested!=null?bal(d.net_invested):'--')+
      _lkv('Capital.com balance', d.balance!=null?bal(d.balance):'--')+
      '<div><div style="color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">Trading P&amp;L</div>'+
        '<div class="'+cls(tp)+'" style="font-size:24px;font-weight:800;">'+(tp!=null?money(tp):'--')+'</div>'+
        '<div class="mut" style="font-size:10px;">balance &minus; net invested (matches the broker)</div></div>'+
    '</div>';
}
function pollLedger(){ fetch('/api/ledger').then(function(r){return r.json();}).then(renderLedger).catch(function(){}); }
function _ledgerTxn(url,verb){
  var a=prompt(verb+' amount (£):'); if(a===null)return;
  var amt=parseFloat(a); if(!(amt>0)){alert('Enter a positive number.');return;}
  var notes=prompt('Notes (optional):'); if(notes===null)notes='';
  fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount:amt,notes:notes})})
    .then(function(r){return r.json();}).then(function(res){ if(res&&res.error){alert(res.error);} if(res&&res.summary){renderLedger(res.summary);} pollLedger(); })
    .catch(function(){ alert('Ledger update failed.'); });
}
function addDeposit(){ _ledgerTxn('/api/ledger/deposit','Deposit'); }
function recordWithdrawal(){ if(!confirm('Record a withdrawal from the account?'))return; _ledgerTxn('/api/ledger/withdraw','Withdrawal'); }
pollLedger();setInterval(pollLedger,7000);
/* Gaius collection light (28 Jul brief): green < 24h, red = stale (click to collect). */
function renderGaiusBar(g){
  var el=document.getElementById('gaius-bar'); if(!el) return; g=g||{};
  if(window._gaiusCollecting){ el.innerHTML='<span style="color:#f39c12;font-weight:700">&#9680; GAIUS COLLECTING...</span>'; return; }
  if(g.collector_fresh){ el.innerHTML='<span style="color:#2ecc71;font-weight:700" title="Last Gaius collection under 24h ago">&#128994; GAIUS OK</span> <span style="color:#888">'+(g.collector_last||'')+'</span>'; }
  else { el.innerHTML='<span onclick="collectGaius()" title="Over 24h old -- click to run a Gaius collection" style="color:#e74c3c;font-weight:700;cursor:pointer">&#128308; GAIUS &mdash; click to collect</span>'; }
}
function pollGaius(){ fetch('/api/gaius-status').then(function(r){return r.json();}).then(renderGaiusBar).catch(function(e){}); }
function collectGaius(){
  if(window._gaiusCollecting) return; window._gaiusCollecting=true;
  var el=document.getElementById('gaius-bar'); if(el){ el.innerHTML='<span style="color:#f39c12;font-weight:700">&#9680; GAIUS COLLECTING...</span>'; }
  fetch('/api/gaius-collect',{method:'POST'}).then(function(r){return r.json();}).then(function(res){
    if(res&&res.status==='error'){ window._gaiusCollecting=false; alert(res.error||'Gaius collect failed.'); }
    else { setTimeout(function(){ window._gaiusCollecting=false; },90000); }
  }).catch(function(){ window._gaiusCollecting=false; alert('Gaius collect request failed.'); });
}
pollGaius();setInterval(pollGaius,15000);
function shutdownAll(){
  if(!confirm("SHUTDOWN ALL base systems (5022-5026 + 5028) and this RoundTable (5030)?")) return;
  fetch('/api/shutdown-all',{method:'POST'})
    .then(function(r){return r.json();})
    .then(function(){
      document.body.innerHTML='<div style="text-align:center;margin-top:16vh;font-family:sans-serif;">'+
        '<h1 style="color:#e74c3c;">&#9211; SHUTDOWN ALL SENT</h1>'+
        '<p style="font-size:14px;color:#888;max-width:620px;margin:8px auto;">'+
        'A shutdown signal was sent to every base system and to this RoundTable. '+
        'Restart from the AlbionBase Desk launcher when ready.</p></div>';
    })
    .catch(function(e){ alert("SHUTDOWN ALL failed: "+e); });
}
</script>
<!-- ARCHIE BRIEF -->
<script>
(function(){
  var ARCHIE_LABEL = '&#9993; ARCHIE BRIEF';
  var BASE_CSS = 'margin-left:10px;font-size:12px;font-weight:700;color:#3498db;background:rgba(52,152,219,0.15);border:1px solid rgba(52,152,219,0.6);padding:4px 11px;border-radius:4px;cursor:pointer;vertical-align:middle;';
  function fallback(txt, done){
    var ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-2000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta); done();
  }
  function copyText(txt, btn){
    function done(){
      btn.style.color='#2ecc71'; btn.style.borderColor='rgba(46,204,113,0.7)';
      btn.textContent='COPIED!';
      setTimeout(function(){ btn.style.cssText=BASE_CSS; btn.innerHTML=ARCHIE_LABEL; },2000);
    }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done, function(){ fallback(txt, done); });
    } else { fallback(txt, done); }
  }
  window.archieBrief=function(btn){
    btn.textContent='...';
    fetch('/api/archie-brief').then(function(r){return r.text();}).then(function(txt){
      copyText(txt, btn);
    }).catch(function(){ btn.textContent='ERROR'; setTimeout(function(){ btn.innerHTML=ARCHIE_LABEL; },2000); });
  };
  function inject(){
    if(document.getElementById('archieBtn')) return;
    var btn=document.createElement('button');
    btn.id='archieBtn'; btn.type='button'; btn.innerHTML=ARCHIE_LABEL;
    btn.setAttribute('onclick','archieBrief(this)');
    btn.style.cssText=BASE_CSS;
    var brand=document.querySelector('.brand');
    if(brand){ brand.appendChild(btn); }
    else { var h=document.querySelector('header'); if(h){ h.appendChild(btn); } }
  }
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', inject); }
  else { inject(); }
})();
</script>
<div id="gaius-bar" style="position:fixed;bottom:0;left:0;right:0;background:var(--bg2);border-top:1px solid var(--bd);padding:5px 20px;font-size:12px;text-align:center;z-index:50;">&#128300; Gaius &mdash; loading...</div>
</body></html>"""


@app.route("/")
def index():
    return HTML.replace("__VER__", "v" + APP_VERSION).replace("__ENV__", _ENV_BADGE)


@app.route("/api/gaius-status")
def api_gaius_status():
    """Gaius collector freshness (green < 24h). Reads the sibling GaiusAI snapshot file
    (Chronicle+Gaius are shared, owned by the Original RoundTable). All times UTC."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "GaiusAI", "logs")
    last, age = None, None
    try:
        with open(os.path.join(base, "gaius_daily_snapshots.json"), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, list) and d:
            last = d[-1].get("generated_utc")
            t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = round((datetime.now(timezone.utc) - t).total_seconds() / 3600.0, 2)
    except Exception:
        pass
    return jsonify({"collector_last": last, "collector_age_h": age,
                    "collector_fresh": (age is not None and age < 24)})


@app.route("/api/gaius-collect", methods=["POST"])
def api_gaius_collect():
    """Proxy a manual Gaius collection to the collector API (:5012 /api/collect-now)."""
    try:
        req = urllib.request.Request("http://%s:5012/api/collect-now" % ALBIONBASE_DELL_HOST,
                                     data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return Response(r.read().decode("utf-8", "replace"), mimetype="application/json")
    except Exception as exc:                               # noqa: BLE001
        return Response(json.dumps({"status": "error",
                                    "error": "Gaius API offline on :5012 -- restart the Gaius collector (%s)" % exc}),
                        status=502, mimetype="application/json")


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "system": "RoundTableBase",
                    "time": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    print("RoundTableBase -> http://localhost:%d" % PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
