# RoundTableBase A.I.

Command centre for the **Albion Base Desk** — the parallel scientific baseline
that trades pure Lancelot + SSL with no AI overlay, so its P&L can be measured
against the original Albion Trading Desk.

- **Port:** 5030 · reads other systems only, owns no trading state.

## What it shows

- The **6 base systems** on ports **5021–5026** — status, price, position, floating P&L, today's P&L, balance, cumulative P&L, Lancelot, and each system's **WITH/AGAINST switch** (controllable from here, live, no restart). Systems not yet built simply render as *not online* and appear automatically once launched.
- The **original desk** on ports **5001–5006**, read purely for the comparison.
- **Headline metric:** Base total cumulative P&L **vs** Original Desk cumulative P&L — compared **apples-to-apples** over the base systems currently online. A positive delta means the base is *ahead* of the AI desk (a strategic signal that the overlay may not be adding value).

## Routes
- `GET /` — the dashboard.
- `GET /api/systems` — gathered base + original comparison data.
- `POST /api/direction/<key>` — proxy a WITH/AGAINST switch change to that base system (live).

## Running
```
python dashboard_roundtablebase.py     # http://localhost:5030
```

All times UTC. Paper trading only.
