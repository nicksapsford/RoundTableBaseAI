"""
dashboard_roundtablebase.py -- RoundTableBase A.I. (AlbionBase Desk)
================================================================================
Command centre for the AlbionBase promotion desk. Port 5036.

  * Reads the 4 AlbionBase systems -- OilBase 5035, GoldBase 5033, FTSEBase 5032,
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

BASE_DIR = Path(__file__).resolve().parent
_VER = BASE_DIR / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"
PORT = 5036
FETCH_TIMEOUT = 2.5

logging.basicConfig(level=logging.WARNING)
logging.Formatter.converter = time.gmtime
log = logging.getLogger("RoundTableBase")
app = Flask(__name__)

# Base systems + their original-desk counterpart (for the comparison).
SYSTEMS = [
    {"key": "oil",  "name": "OilBase",  "market": "Brent Crude", "port": 5035, "start": 1000.0, "orig_port": 5005, "orig_kind": "oil",  "colour": "#FF6600"},
    {"key": "gold", "name": "GoldBase", "market": "GOLD (XAU)",  "port": 5033, "start": 1000.0, "orig_port": 5003, "orig_kind": "gold", "colour": "#FFD700"},
    {"key": "ftse", "name": "FTSEBase", "market": "FTSE 100",    "port": 5032, "start": 1000.0, "orig_port": 5002, "orig_kind": "ftse", "colour": "#4169E1"},
    {"key": "us",   "name": "USBase",   "market": "S&P 500",     "port": 5034, "start": 1000.0, "orig_port": 5004, "orig_kind": "us",   "colour": "#FFFFFF"},
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


def _orig_balance(state, kind, start):
    """Balance from an ORIGINAL system's /api/state (mirrors RoundTable extraction)."""
    if kind == "crypto":
        b = _fnum(state.get("combined_capital"))
        if b is None:
            a = _fnum((state.get("account") or {}).get("capital")) or 0.0
            e = _fnum((state.get("account_eth") or {}).get("capital")) or 0.0
            b = a + e
        return b
    b = _fnum(state.get("capital"))
    if b is None:
        b = _fnum((state.get("account") or {}).get("capital"))
    return b


def _orig_daily(state, kind):
    if kind == "crypto":
        a = _fnum((state.get("account") or {}).get("daily_pnl")) or 0.0
        e = _fnum((state.get("account_eth") or {}).get("daily_pnl")) or 0.0
        return a + e
    v = _fnum((state.get("account") or {}).get("daily_pnl"))
    if v is None:
        v = _fnum(state.get("daily_pnl"))
    return v if v is not None else 0.0


def _fetch_one(cfg):
    """Return a uniform row for one base system + its original counterpart."""
    row = {"key": cfg["key"], "name": cfg["name"], "market": cfg["market"],
           "port": cfg["port"], "colour": cfg["colour"], "start": cfg["start"],
           "online": False, "mode": None, "price": None, "position": None,
           "floating_gbp": 0.0, "locked": None, "today_pnl": None, "balance": None,
           "cum_pnl": None, "lancelot": "--", "session": "24/7" if cfg["key"] == "crypto" else "--",
           "orig_balance": None, "orig_cum_pnl": None}

    st = _get_json("http://localhost:%d/api/state" % cfg["port"])
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
        row["session"] = st.get("session") or row["session"]
        if bal is not None:
            row["cum_pnl"] = round(bal - cfg["start"], 2)
        # A compact position/lancelot summary (crypto has two instruments)
        legs = [st.get("btc"), st.get("eth")] if cfg["key"] == "crypto" else [st]
        pos_bits, lanc_bits = [], []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            if leg.get("in_trade") and leg.get("position"):
                pos_bits.append("%s %s" % (leg.get("label", ""), leg["position"].get("direction", "")))
            lanc_bits.append(str(leg.get("lancelot", "")))
        row["position"] = ", ".join(b for b in pos_bits if b.strip()) or "FLAT"
        row["lancelot"] = " / ".join(b for b in lanc_bits if b) or "--"
        row["price"] = (st.get("btc") or {}).get("price") if cfg["key"] == "crypto" else _fnum(st.get("price"))

    # Direction mode authoritative from the switch endpoint (if online)
    if row["online"] and not row["mode"]:
        d = _get_json("http://localhost:%d/api/direction" % cfg["port"])
        if d:
            row["mode"] = d.get("mode")

    # Original-desk counterpart P&L (comparison side)
    ost = _get_json("http://localhost:%d/api/state" % cfg["orig_port"])
    if ost:
        ob = _orig_balance(ost, cfg["orig_kind"], cfg["start"])
        if ob is not None:
            row["orig_balance"] = round(ob, 2)
            row["orig_cum_pnl"] = round(ob - cfg["start"], 2)
    return row


def _gather():
    with ThreadPoolExecutor(max_workers=len(SYSTEMS)) as ex:
        rows = list(ex.map(_fetch_one, SYSTEMS))
    online = [r for r in rows if r["online"]]
    # apples-to-apples: compare only systems whose BASE side is online
    bench_val = sum((r["balance"] or 0.0) for r in online)
    bench_today = sum((r["today_pnl"] or 0.0) for r in online)
    bench_cum = sum((r["cum_pnl"] or 0.0) for r in online)
    orig_val = sum((r["orig_balance"] or 0.0) for r in online if r["orig_balance"] is not None)
    orig_cum = sum((r["orig_cum_pnl"] or 0.0) for r in online if r["orig_cum_pnl"] is not None)
    return {
        "systems": rows,
        "online_count": len(online),
        "total_count": len(SYSTEMS),
        "base": {"value": round(bench_val, 2), "today_pnl": round(bench_today, 2),
                      "cum_pnl": round(bench_cum, 2)},
        "original": {"cum_pnl": round(orig_cum, 2), "value": round(orig_val, 2)},
        "delta": round(bench_cum - orig_cum, 2),   # >0 = base ahead of AI desk
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
    o = g["original"]
    delta = g["delta"]
    delta_word = "AHEAD" if delta >= 0 else "BEHIND"
    lines = []
    lines.append(bar)
    lines.append("ARCHIE BRIEF -- BASE ROUNDTABLE")
    lines.append("Generated: %s UTC" % ts)
    lines.append(bar)
    lines.append("")
    lines.append("PORTFOLIO")
    lines.append("  Base Total : GBP %.2f" % b["value"])
    lines.append("  Original Desk   : GBP %.2f" % o["value"])
    lines.append("  Delta           : %s (%s of the AI desk)" % (_g(delta), delta_word))
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
        lines.append("    locked %s | today %s | bal %s | switch %s | lancelot %s | floating %s"
                     % (_g(s.get("locked")), _g(s.get("today_pnl")),
                        ("GBP %.2f" % s["balance"]) if s.get("balance") is not None else "--",
                        s.get("mode") or "--", s.get("lancelot") or "--", _g(s.get("floating_gbp"))))
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
    lines.append("COMPARISON (cumulative P&L, matched online systems)")
    lines.append(bar)
    lines.append("  Base Cum P&L   : %s" % _g(b["cum_pnl"]))
    lines.append("  Original Desk Cum   : %s" % _g(o["cum_pnl"]))
    lines.append("  Delta               : %s (base %s)" % (_g(delta), delta_word))
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


@app.route("/api/direction/<key>", methods=["POST"])
def api_direction(key):
    """Proxy a WITH/AGAINST switch change to the base system (live, no restart)."""
    cfg = SYS_BY_KEY.get(key)
    if not cfg:
        return jsonify({"error": "unknown system"}), 404
    try:
        body = request.get_json(force=True, silent=True) or {}
        payload = json.dumps({"mode": body.get("mode"), "by": body.get("by") or "Nick"}).encode("utf-8")
        res = _get_json("http://localhost:%d/api/direction" % cfg["port"], method="POST", data=payload)
        if res is None:
            return jsonify({"error": "system offline"}), 502
        return jsonify(res)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/shutdown-all", methods=["POST"])
def api_shutdown_all():
    """SHUTDOWN ALL -- mirror the original RoundTable mechanism: POST
    /api/shutdown to each child port (all base systems 5022-5026 + 5028), then
    shut RoundTableBase (5030) itself down. A child that is offline or lacks the
    endpoint is reported but does not block the rest."""
    results = {}
    for port in SHUTDOWN_PORTS:
        url = "http://localhost:%d/api/shutdown" % port
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
    <small>__VER__ &middot; port 5030 &middot; pure Lancelot + SSL, no AI overlay</small></div>
  <div style="display:flex;align-items:center;gap:12px;">
    <button onclick="shutdownAll()" title="Shut down all base systems + this RoundTable"
      style="font-size:12px;font-weight:700;color:#e74c3c;background:rgba(231,76,60,0.15);border:1px solid rgba(231,76,60,0.6);padding:4px 11px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#9211; SHUTDOWN ALL</button>
    <div class="clock" id="clock">--:--:-- UTC</div>
  </div>
</header>
<div class="wrap">
  <div class="cmp" id="cmp"></div>
  <table><thead><tr>
    <th>System</th><th>Status</th><th>Price</th><th>Position</th><th>Floating</th>
    <th>Locked</th><th>Today</th><th>Balance</th><th>Cum P&amp;L</th><th>Lancelot</th><th>Switch</th>
  </tr></thead><tbody id="rows"></tbody></table>
  <div class="note">Base P&amp;L vs Original Desk P&amp;L is compared only over base systems currently online
    (apples-to-apples). Systems 5022-5026 appear here automatically as they are built. Paper trading only.</div>
</div>
<script>
function clk(){var t=new Date();document.getElementById('clock').textContent=
  String(t.getUTCHours()).padStart(2,'0')+':'+String(t.getUTCMinutes()).padStart(2,'0')+':'+String(t.getUTCSeconds()).padStart(2,'0')+' UTC';}
setInterval(clk,1000);clk();
function money(v){if(v===null||v===undefined)return '--';var n=Number(v);return (n<0?'-£':'+£')+Math.abs(n).toFixed(2);}
function bal(v){return v===null||v===undefined?'--':'£'+Number(v).toFixed(2);}
function cls(v){return Number(v)>=0?'bull':'bear';}
function setDir(key,mode){
  fetch('/api/direction/'+key,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode,by:'Nick'})})
    .then(function(r){return r.json();}).then(function(){poll();}).catch(function(e){console.error(e);});
}
function swBtns(key,mode,online){
  if(!online){return '<span class="mut">--</span>';}
  var w=(mode==='WITH')?' class="on-WITH"':'';var a=(mode==='AGAINST')?' class="on-AGAINST"':'';
  return '<span class="sw"><button'+w+' onclick="setDir(\\''+key+'\\',\\'WITH\\')">WITH</button>'+
         '<button'+a+' onclick="setDir(\\''+key+'\\',\\'AGAINST\\')">AGAINST</button></span>';
}
function poll(){
  fetch('/api/systems').then(function(r){return r.json();}).then(function(d){
    var b=d.base,o=d.original,delta=d.delta;
    var vClass=delta>=0?'bull':'bear';
    var vTxt=delta>=0?('Base AHEAD of original desk by '+money(delta).replace('+','')):('Base BEHIND original desk by £'+Math.abs(delta).toFixed(2));
    document.getElementById('cmp').innerHTML=
      '<div class="box"><div class="lbl">Base Cum P&amp;L</div><div class="big '+cls(b.cum_pnl)+'">'+money(b.cum_pnl)+'</div><div class="sub">value £'+Number(b.value).toFixed(2)+' &middot; today '+money(b.today_pnl)+'</div></div>'+
      '<div class="box"><div class="lbl">Original Desk Cum P&amp;L</div><div class="big '+cls(o.cum_pnl)+'">'+money(o.cum_pnl)+'</div><div class="sub">matched systems &middot; value £'+Number(o.value).toFixed(2)+'</div></div>'+
      '<div class="box"><div class="lbl">Systems Online</div><div class="big">'+d.online_count+' / '+d.total_count+'</div><div class="sub">updated '+(d.updated_utc||'--')+' UTC</div></div>'+
      '<div class="verdict '+vClass+'">'+vTxt+'</div>';
    var h='';
    for(var i=0;i<d.systems.length;i++){var s=d.systems[i];
      if(!s.online){
        h+='<tr class="offline"><td>'+s.name+' <span class="mut">:'+s.port+'</span></td>'+
           '<td><span class="dot off"></span>not online</td><td colspan="8" class="mut">awaiting build / launch</td></tr>';
        continue;
      }
      h+='<tr><td><span style="color:'+s.colour+'">&#9632;</span> '+s.name+' <span class="mut">:'+s.port+'</span></td>'+
        '<td><span class="dot on"></span>online</td>'+
        '<td>'+(s.price!=null?'£'+Number(s.price).toLocaleString('en-GB',{maximumFractionDigits:2}):'--')+'</td>'+
        '<td>'+(s.position||'--')+'</td>'+
        '<td class="'+cls(s.floating_gbp)+'">'+money(s.floating_gbp)+'</td>'+
        '<td class="'+(s.locked!=null&&Number(s.locked)>0?'green':'mut')+'">'+(s.locked!=null&&Number(s.locked)>0?'&#128274; +£'+Number(s.locked).toFixed(2):'--')+'</td>'+
        '<td class="'+cls(s.today_pnl)+'">'+money(s.today_pnl)+'</td>'+
        '<td>'+bal(s.balance)+'</td>'+
        '<td class="'+cls(s.cum_pnl)+'">'+money(s.cum_pnl)+'</td>'+
        '<td class="mut" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;">'+(s.lancelot||'--')+'</td>'+
        '<td>'+swBtns(s.key,s.mode,s.online)+'</td></tr>';
    }
    document.getElementById('rows').innerHTML=h;
  }).catch(function(e){});
}
poll();setInterval(poll,7000);
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
    return HTML.replace("__VER__", "v" + APP_VERSION)


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
        req = urllib.request.Request("http://localhost:5012/api/collect-now",
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
