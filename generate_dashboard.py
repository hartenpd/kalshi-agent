"""
Dashboard Generator — reads kalshi_agent.db and produces dashboard.html

Run:  uv run generate_dashboard.py
Then open dashboard.html in your browser.
"""

import os
import sqlite3
import json
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.path.join(os.path.dirname(__file__), "kalshi_agent.db")
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")

# Methodology display config: label, CSS class suffix, active flag
METHODOLOGIES = {
    "flat_v1": {"label": "Flat", "tag_class": "meth-flat", "active": False},
    "market_aware_v1": {"label": "Market-Aware", "tag_class": "meth-market", "active": True},
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _compute_stats(pick_list):
    """Compute summary stats for a list of picks (any methodology slice)."""
    total = len(pick_list)
    traded = [p for p in pick_list if p["bet_placed"]]
    settled = [p for p in pick_list if p["outcome"] in ("win", "loss")]
    wins = sum(1 for p in settled if p["outcome"] == "win")
    losses = sum(1 for p in settled if p["outcome"] == "loss")
    win_rate = wins / len(settled) * 100 if settled else 0

    bets_with_pnl = [p for p in pick_list if p["bet_placed"] and p["pnl"] is not None]
    total_pnl = sum(p["pnl"] for p in bets_with_pnl)
    total_wagered = sum(p["bet_amount"] for p in bets_with_pnl if p["bet_amount"])
    roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0
    avg_bet = total_wagered / len(bets_with_pnl) if bets_with_pnl else 0

    return {
        "total": total,
        "traded": len(traded),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "total_wagered": round(total_wagered, 2),
        "roi": round(roi, 1),
        "avg_bet": round(avg_bet, 2),
    }


def _compute_calibration(pick_list):
    """Compute calibration-by-confidence and edge analysis for a pick slice."""
    settled = [p for p in pick_list if p["outcome"] in ("win", "loss")]

    # Calibration by confidence
    cal_by_conf = []
    for stars in (5, 4, 3, 2, 1):
        bucket = [p for p in settled if p["confidence"] == stars]
        if not bucket:
            continue
        n = len(bucket)
        bucket_wins = sum(1 for p in bucket if p["outcome"] == "win")
        actual_wr = bucket_wins / n * 100
        avg_model = sum(p["model_probability"] for p in bucket) / n * 100
        diff = actual_wr - avg_model
        cal_by_conf.append({
            "stars": stars, "n": n, "wins": bucket_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_model": round(avg_model, 1),
            "diff": round(diff, 1),
        })

    # Edge analysis
    edge_buckets = [
        ("2-5%", 0.02, 0.05),
        ("5-10%", 0.05, 0.10),
        ("10-15%", 0.10, 0.15),
        ("15%+", 0.15, 999),
    ]
    edge_analysis = []
    edge_picks = [p for p in settled if p["edge"] is not None]

    for label, lo, hi in edge_buckets:
        bucket = [p for p in edge_picks if lo <= p["edge"] < hi]
        if not bucket:
            continue
        n = len(bucket)
        bucket_wins = sum(1 for p in bucket if p["outcome"] == "win")
        actual_wr = bucket_wins / n * 100
        bucket_bets = [p for p in bucket if p["bet_placed"] and p["pnl"] is not None]
        avg_pnl = sum(p["pnl"] for p in bucket_bets) / len(bucket_bets) if bucket_bets else 0
        total_bucket_pnl = sum(p["pnl"] for p in bucket_bets)
        edge_analysis.append({
            "label": label, "n": n, "wins": bucket_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_bucket_pnl, 2),
            "bets": len(bucket_bets),
        })

    # Negative/low edge bucket
    neg_edge = [p for p in edge_picks if p["edge"] < 0.02]
    if neg_edge:
        n = len(neg_edge)
        neg_wins = sum(1 for p in neg_edge if p["outcome"] == "win")
        neg_bets = [p for p in neg_edge if p["bet_placed"] and p["pnl"] is not None]
        neg_pnl = sum(p["pnl"] for p in neg_bets) / len(neg_bets) if neg_bets else 0
        edge_analysis.insert(0, {
            "label": "<2%", "n": n, "wins": neg_wins,
            "actual_wr": round(neg_wins / n * 100, 1),
            "avg_pnl": round(neg_pnl, 2),
            "total_pnl": round(sum(p["pnl"] for p in neg_bets), 2) if neg_bets else 0,
            "bets": len(neg_bets),
        })

    # Avg edge accuracy: how close model prob was to actual outcome
    avg_edge_accuracy = None
    if edge_picks:
        # For each pick, edge accuracy = how close edge predicted the outcome
        # Use average absolute edge as a proxy
        avg_edge_accuracy = round(
            sum(abs(p["edge"]) for p in edge_picks) / len(edge_picks) * 100, 1
        )

    return cal_by_conf, edge_analysis, avg_edge_accuracy


def build_dashboard():
    conn = get_db()

    # ── Fetch all data ───────────────────────────────────────────────
    picks = conn.execute(
        "SELECT * FROM analyst_picks ORDER BY game_date DESC, id DESC"
    ).fetchall()

    trades = conn.execute(
        "SELECT * FROM trades WHERE status IN ('submitted', 'filled') "
        "ORDER BY timestamp DESC"
    ).fetchall()

    conn.close()

    # ── Overall summary stats ─────────────────────────────────────────
    overall = _compute_stats(picks)

    # Best and worst single trade (overall)
    bets_with_pnl = [p for p in picks if p["bet_placed"] and p["pnl"] is not None]
    best_trade = max(bets_with_pnl, key=lambda p: p["pnl"]) if bets_with_pnl else None
    worst_trade = min(bets_with_pnl, key=lambda p: p["pnl"]) if bets_with_pnl else None

    # ── Per-methodology stats ─────────────────────────────────────────
    meth_groups = defaultdict(list)
    for p in picks:
        meth_groups[p["methodology"]].append(p)

    meth_stats = {}
    meth_calibrations = {}
    for meth_key in METHODOLOGIES:
        meth_picks = meth_groups.get(meth_key, [])
        meth_stats[meth_key] = _compute_stats(meth_picks)
        cal_conf, edge_anal, avg_edge = _compute_calibration(meth_picks)
        meth_calibrations[meth_key] = {
            "cal_by_conf": cal_conf,
            "edge_analysis": edge_anal,
            "avg_edge_accuracy": avg_edge,
        }

    # ── Overall calibration (combined) ────────────────────────────────
    overall_cal_conf, overall_edge, _ = _compute_calibration(picks)

    # By sport (overall)
    settled = [p for p in picks if p["outcome"] in ("win", "loss")]
    sports_data = defaultdict(list)
    for p in settled:
        sports_data[p["sport"]].append(p)

    cal_by_sport = []
    for sport in sorted(sports_data.keys()):
        rows = sports_data[sport]
        n = len(rows)
        sport_wins = sum(1 for p in rows if p["outcome"] == "win")
        actual_wr = sport_wins / n * 100
        avg_model = sum(p["model_probability"] for p in rows) / n * 100
        sport_bets = [p for p in rows if p["bet_placed"] and p["pnl"] is not None]
        sport_pnl = sum(p["pnl"] for p in sport_bets)
        cal_by_sport.append({
            "sport": sport, "n": n, "wins": sport_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_model": round(avg_model, 1),
            "pnl": round(sport_pnl, 2),
        })

    # ── Pick log with methodology ─────────────────────────────────────
    pick_rows = []
    for p in picks:
        pick_rows.append({
            "id": p["id"],
            "date": p["game_date"],
            "sport": p["sport"],
            "game": p["game"],
            "pick": p["pick"],
            "confidence": p["confidence"],
            "model_prob": round(p["model_probability"] * 100, 1),
            "market_price": p["market_price"],
            "edge": round(p["edge"] * 100, 1) if p["edge"] is not None else None,
            "bet_amount": round(p["bet_amount"], 2) if p["bet_amount"] else None,
            "outcome": p["outcome"],
            "pnl": round(p["pnl"], 2) if p["pnl"] is not None else None,
            "methodology": p["methodology"],
        })

    # ── Generate timestamp ───────────────────────────────────────────
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Build the HTML ───────────────────────────────────────────────
    html = _build_html(
        overall=overall,
        best_trade=best_trade,
        worst_trade=worst_trade,
        meth_stats=meth_stats,
        meth_calibrations=meth_calibrations,
        overall_cal_conf=overall_cal_conf,
        overall_edge=overall_edge,
        cal_by_sport=cal_by_sport,
        pick_rows=pick_rows,
        generated_at=generated_at,
    )

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_PATH}")
    print(f"  {overall['total']} picks, {overall['traded']} traded, ${overall['total_pnl']:+.2f} P&L")


def _build_html(**data):
    overall = data["overall"]
    best = data["best_trade"]
    worst = data["worst_trade"]
    best_str = (
        f'+${best["pnl"]:.2f} — {best["pick"]} ({best["game"]})'
        if best else "—"
    )
    worst_str = (
        f'-${abs(worst["pnl"]):.2f} — {worst["pick"]} ({worst["game"]})'
        if worst else "—"
    )

    pnl_color = "#4ade80" if overall["total_pnl"] >= 0 else "#f87171"
    pnl_sign = "+" if overall["total_pnl"] >= 0 else ""
    roi_color = "#4ade80" if overall["roi"] >= 0 else "#f87171"
    roi_sign = "+" if overall["roi"] >= 0 else ""

    # ── Methodology summary cards HTML ────────────────────────────────
    meth_cards_html = ""
    for meth_key, meta in METHODOLOGIES.items():
        s = data["meth_stats"].get(meth_key, _compute_stats([]))
        is_active = meta["active"]
        # Active methodology gets a blue left border; retired gets dimmed
        border_style = "border-left: 3px solid #3b82f6;" if is_active else "border-left: 3px solid #475569; opacity: 0.7;"
        status_label = "ACTIVE" if is_active else "RETIRED"
        status_color = "#3b82f6" if is_active else "#64748b"
        tag_class = meta["tag_class"]

        m_pnl_color = "#4ade80" if s["total_pnl"] >= 0 else "#f87171"
        m_pnl_sign = "+" if s["total_pnl"] >= 0 else ""
        m_roi_sign = "+" if s["roi"] >= 0 else ""

        meth_cards_html += f"""
  <div class="meth-row" style="{border_style}">
    <div class="meth-header">
      <span class="badge {tag_class}">{meta["label"]}</span>
      <span style="color: {status_color}; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.05em;">{status_label}</span>
    </div>
    <div class="meth-stats">
      <div class="meth-stat">
        <span class="meth-stat-label">Picks</span>
        <span class="meth-stat-value">{s["total"]}</span>
      </div>
      <div class="meth-stat">
        <span class="meth-stat-label">Win Rate</span>
        <span class="meth-stat-value">{s["win_rate"]}%</span>
        <span class="meth-stat-detail">{s["wins"]}W / {s["losses"]}L ({s["settled"]} settled)</span>
      </div>
      <div class="meth-stat">
        <span class="meth-stat-label">P&L</span>
        <span class="meth-stat-value" style="color: {m_pnl_color}">{m_pnl_sign}${abs(s["total_pnl"]):.2f}</span>
      </div>
      <div class="meth-stat">
        <span class="meth-stat-label">ROI</span>
        <span class="meth-stat-value">{m_roi_sign}{s["roi"]}%</span>
      </div>
    </div>
  </div>"""

    # ── Per-methodology calibration sections ──────────────────────────
    meth_cal_sections = ""
    for meth_key, meta in METHODOLOGIES.items():
        cal_data = data["meth_calibrations"].get(meth_key, {})
        cal_conf = cal_data.get("cal_by_conf", [])
        edge_anal = cal_data.get("edge_analysis", [])
        is_active = meta["active"]
        status_text = "(active)" if is_active else "(retired)"
        section_opacity = "" if is_active else "opacity: 0.75;"

        cal_conf_id = f"cal-conf-{meth_key.replace('_', '-')}"
        edge_id = f"edge-{meth_key.replace('_', '-')}"

        meth_cal_sections += f"""
<div style="{section_opacity}">
<h2><span class="badge {meta['tag_class']}">{meta["label"]}</span> Methodology {status_text}</h2>

<h3 style="font-size: 0.95rem; color: #94a3b8; margin: 16px 0 8px;">Calibration by Confidence</h3>
<table>
<thead>
  <tr>
    <th>Stars</th>
    <th class="num">Picks</th>
    <th class="num">Model Prob</th>
    <th class="num">Actual Win%</th>
    <th>Verdict</th>
  </tr>
</thead>
<tbody id="{cal_conf_id}"></tbody>
</table>

<h3 style="font-size: 0.95rem; color: #94a3b8; margin: 16px 0 8px;">Edge Analysis</h3>
<table>
<thead>
  <tr>
    <th>Edge Bucket</th>
    <th class="num">Picks</th>
    <th class="num">Win%</th>
    <th class="num">Bets Placed</th>
    <th class="num">Avg P&L</th>
    <th class="num">Total P&L</th>
  </tr>
</thead>
<tbody id="{edge_id}"></tbody>
</table>
</div>
"""

    # Serialize data for JS
    cal_sport_json = json.dumps(data["cal_by_sport"])
    picks_json = json.dumps(data["pick_rows"])

    # Per-methodology calibration data for JS
    meth_cal_js_blocks = ""
    for meth_key in METHODOLOGIES:
        cal_data = data["meth_calibrations"].get(meth_key, {})
        conf_id = f"cal-conf-{meth_key.replace('_', '-')}"
        edge_id = f"edge-{meth_key.replace('_', '-')}"
        conf_json = json.dumps(cal_data.get("cal_by_conf", []))
        edge_json = json.dumps(cal_data.get("edge_analysis", []))
        meth_cal_js_blocks += f"""
renderCalConf(document.getElementById('{conf_id}'), {conf_json});
renderEdge(document.getElementById('{edge_id}'), {edge_json});
"""

    # ── Methodology comparison data ───────────────────────────────────
    comparison_rows = []
    min_picks_threshold = 10
    for meth_key, meta in METHODOLOGIES.items():
        s = data["meth_stats"].get(meth_key, _compute_stats([]))
        cal_data = data["meth_calibrations"].get(meth_key, {})
        comparison_rows.append({
            "key": meth_key,
            "label": meta["label"],
            "tag_class": meta["tag_class"],
            "active": meta["active"],
            "settled": s["settled"],
            "win_rate": s["win_rate"],
            "roi": s["roi"],
            "avg_bet": s["avg_bet"],
            "avg_edge": cal_data.get("avg_edge_accuracy"),
            "total_pnl": s["total_pnl"],
            "sufficient": s["settled"] >= min_picks_threshold,
        })
    comparison_json = json.dumps(comparison_rows)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Agent Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: #0f1117;
  color: #e2e8f0;
  line-height: 1.6;
  padding: 24px;
  max-width: 1200px;
  margin: 0 auto;
}}
h1 {{
  font-size: 1.8rem;
  font-weight: 700;
  margin-bottom: 4px;
  color: #f8fafc;
}}
.subtitle {{
  color: #64748b;
  font-size: 0.85rem;
  margin-bottom: 24px;
}}
h2 {{
  font-size: 1.15rem;
  font-weight: 600;
  margin: 32px 0 12px;
  color: #f1f5f9;
  border-bottom: 1px solid #1e293b;
  padding-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 8px;
}}
h3 {{
  font-size: 0.95rem;
  color: #94a3b8;
  margin: 16px 0 8px;
}}

/* Summary cards */
.cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}}
.card {{
  background: #1a1d2e;
  border: 1px solid #2d3348;
  border-radius: 10px;
  padding: 16px 20px;
}}
.card-label {{
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #64748b;
  margin-bottom: 4px;
}}
.card-value {{
  font-size: 1.5rem;
  font-weight: 700;
  color: #f8fafc;
}}
.card-detail {{
  font-size: 0.8rem;
  color: #94a3b8;
  margin-top: 2px;
}}

/* Methodology rows */
.meth-section {{
  margin: 16px 0 24px;
}}
.meth-row {{
  background: #1a1d2e;
  border: 1px solid #2d3348;
  border-radius: 10px;
  padding: 14px 20px;
  margin-bottom: 10px;
}}
.meth-header {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}}
.meth-stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
}}
.meth-stat {{
  display: flex;
  flex-direction: column;
}}
.meth-stat-label {{
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: #64748b;
}}
.meth-stat-value {{
  font-size: 1.15rem;
  font-weight: 700;
  color: #f8fafc;
}}
.meth-stat-detail {{
  font-size: 0.72rem;
  color: #64748b;
}}

/* Tables */
table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
  margin-bottom: 8px;
}}
th {{
  text-align: left;
  padding: 10px 12px;
  background: #1a1d2e;
  color: #94a3b8;
  font-weight: 600;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid #2d3348;
  position: sticky;
  top: 0;
}}
td {{
  padding: 8px 12px;
  border-bottom: 1px solid #1e293b;
  color: #cbd5e1;
}}
tr:hover td {{
  background: #1a1d2e;
}}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}

/* Outcome colors */
.win {{ color: #4ade80; font-weight: 600; }}
.loss {{ color: #f87171; font-weight: 600; }}
.push {{ color: #facc15; font-weight: 600; }}
.pending {{ color: #64748b; }}

/* Calibration bars */
.cal-bar-wrap {{
  display: flex;
  align-items: center;
  gap: 8px;
}}
.cal-bar {{
  height: 8px;
  border-radius: 4px;
  min-width: 4px;
}}
.bar-model {{ background: #3b82f6; opacity: 0.4; }}
.bar-actual {{ background: #3b82f6; }}

/* Verdict badges */
.badge {{
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 0.75rem;
  font-weight: 600;
}}
.badge-good {{ background: #166534; color: #4ade80; }}
.badge-warn {{ background: #713f12; color: #facc15; }}
.badge-bad {{ background: #7f1d1d; color: #f87171; }}

/* Methodology badges */
.meth-flat {{ background: #374151; color: #9ca3af; }}
.meth-market {{ background: #1e3a5f; color: #60a5fa; }}

/* Stars */
.stars {{ color: #f59e0b; letter-spacing: 1px; }}

/* Edge bucket highlight */
.edge-positive {{ color: #4ade80; }}
.edge-negative {{ color: #f87171; }}

/* Comparison section */
.compare-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin: 16px 0;
}}
.compare-card {{
  background: #1a1d2e;
  border: 1px solid #2d3348;
  border-radius: 10px;
  padding: 16px 20px;
}}
.compare-card h3 {{
  margin: 0 0 12px;
  font-size: 0.9rem;
}}
.compare-metric {{
  display: flex;
  justify-content: space-between;
  padding: 6px 0;
  border-bottom: 1px solid #1e293b;
  font-size: 0.85rem;
}}
.compare-metric:last-child {{
  border-bottom: none;
}}
.compare-metric-label {{
  color: #94a3b8;
}}
.compare-metric-value {{
  font-weight: 600;
  color: #f8fafc;
  font-variant-numeric: tabular-nums;
}}
.insufficient {{
  color: #64748b;
  font-style: italic;
  text-align: center;
  padding: 16px;
  font-size: 0.85rem;
}}

/* Responsive */
@media (max-width: 640px) {{
  body {{ padding: 12px; }}
  .cards {{ grid-template-columns: 1fr 1fr; }}
  .compare-grid {{ grid-template-columns: 1fr; }}
  table {{ font-size: 0.78rem; }}
  td, th {{ padding: 6px 8px; }}
}}
</style>
</head>
<body>

<h1>Kalshi Agent Dashboard</h1>
<p class="subtitle">Generated {data["generated_at"]} &middot; {overall["total"]} picks tracked</p>

<!-- ═══ Overall Summary Cards ═══ -->
<div class="cards">
  <div class="card">
    <div class="card-label">Total Picks</div>
    <div class="card-value">{overall["total"]}</div>
    <div class="card-detail">{overall["traded"]} traded &middot; {overall["total"] - overall["traded"]} passed</div>
  </div>
  <div class="card">
    <div class="card-label">Overall Win Rate</div>
    <div class="card-value">{overall["win_rate"]}%</div>
    <div class="card-detail">{overall["wins"]}W / {overall["losses"]}L</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-value" style="color: {pnl_color}">{pnl_sign}${abs(overall["total_pnl"]):.2f}</div>
    <div class="card-detail">${overall["total_wagered"]:.2f} wagered &middot; {roi_sign}{overall["roi"]}% ROI</div>
  </div>
  <div class="card">
    <div class="card-label">Best Trade</div>
    <div class="card-value" style="color: #4ade80; font-size: 1rem">{best_str}</div>
  </div>
  <div class="card">
    <div class="card-label">Worst Trade</div>
    <div class="card-value" style="color: #f87171; font-size: 1rem">{worst_str}</div>
  </div>
</div>

<!-- ═══ Per-Methodology Summary ═══ -->
<h2>Performance by Methodology</h2>
<div class="meth-section">
{meth_cards_html}
</div>

<!-- ═══ Calibration by Sport (overall) ═══ -->
<h2>Calibration by Sport</h2>
<table>
<thead>
  <tr>
    <th>Sport</th>
    <th class="num">Picks</th>
    <th class="num">Model Prob</th>
    <th class="num">Actual Win%</th>
    <th class="num">P&L</th>
  </tr>
</thead>
<tbody id="cal-sport-body"></tbody>
</table>

<!-- ═══ Per-Methodology Calibration & Edge ═══ -->
{meth_cal_sections}

<!-- ═══ Pick Log ═══ -->
<h2>Pick Log</h2>
<table>
<thead>
  <tr>
    <th>#</th>
    <th>Date</th>
    <th>Method</th>
    <th>Sport</th>
    <th>Game</th>
    <th>Pick</th>
    <th class="num">Conf</th>
    <th class="num">Model</th>
    <th class="num">Market</th>
    <th class="num">Edge</th>
    <th class="num">Bet</th>
    <th>Result</th>
    <th class="num">P&L</th>
  </tr>
</thead>
<tbody id="picks-body"></tbody>
</table>

<!-- ═══ Methodology Comparison ═══ -->
<h2>Methodology Comparison</h2>
<div id="comparison-section"></div>

<p class="subtitle" style="margin-top: 24px; text-align: center;">
  Kalshi Sports Agent &middot; Built with Claude Code
</p>

<script>
const calSport = {cal_sport_json};
const picksData = {picks_json};
const comparisonData = {comparison_json};

// ── Methodology badge helper ──
const METH_CONFIG = {{
  'flat_v1': {{ label: 'FLAT', cls: 'meth-flat' }},
  'market_aware_v1': {{ label: 'MARKET-AWARE', cls: 'meth-market' }},
}};
function methBadge(key) {{
  const cfg = METH_CONFIG[key] || {{ label: key, cls: 'meth-flat' }};
  return `<span class="badge ${{cfg.cls}}">${{cfg.label}}</span>`;
}}

// ── Reusable renderers ──
function renderCalConf(tbody, data) {{
  if (!data.length) {{
    tbody.innerHTML = '<tr><td colspan="5" class="insufficient">No settled picks yet</td></tr>';
    return;
  }}
  data.forEach(r => {{
    const stars = '\u2605'.repeat(r.stars) + '\u2606'.repeat(5 - r.stars);
    const diff = r.diff;
    let badge;
    if (Math.abs(diff) < 5) badge = '<span class="badge badge-good">well calibrated</span>';
    else if (diff > 0) badge = `<span class="badge badge-warn">underconfident ${{Math.abs(diff).toFixed(0)}}%</span>`;
    else badge = `<span class="badge badge-bad">overconfident ${{Math.abs(diff).toFixed(0)}}%</span>`;

    tbody.innerHTML += `<tr>
      <td><span class="stars">${{stars}}</span></td>
      <td class="num">${{r.n}}</td>
      <td class="num">${{r.avg_model.toFixed(0)}}%</td>
      <td class="num">${{r.actual_wr.toFixed(0)}}%</td>
      <td>${{badge}}</td>
    </tr>`;
  }});
}}

function renderEdge(tbody, data) {{
  if (!data.length) {{
    tbody.innerHTML = '<tr><td colspan="6" class="insufficient">No settled picks with edge data yet</td></tr>';
    return;
  }}
  data.forEach(r => {{
    const wrClass = r.actual_wr >= 50 ? 'edge-positive' : 'edge-negative';
    const pnlClass = r.total_pnl >= 0 ? 'win' : 'loss';
    const pnlSign = r.total_pnl >= 0 ? '+' : '';
    const avgSign = r.avg_pnl >= 0 ? '+' : '';
    tbody.innerHTML += `<tr>
      <td>${{r.label}}</td>
      <td class="num">${{r.n}}</td>
      <td class="num ${{wrClass}}">${{r.actual_wr.toFixed(0)}}%</td>
      <td class="num">${{r.bets}}</td>
      <td class="num">${{avgSign}}$${{Math.abs(r.avg_pnl).toFixed(2)}}</td>
      <td class="num ${{pnlClass}}">${{pnlSign}}$${{Math.abs(r.total_pnl).toFixed(2)}}</td>
    </tr>`;
  }});
}}

// ── Render per-methodology calibration tables ──
{meth_cal_js_blocks}

// ── Calibration by Sport ──
const sportBody = document.getElementById('cal-sport-body');
calSport.forEach(r => {{
  const pnlClass = r.pnl >= 0 ? 'win' : 'loss';
  const pnlSign = r.pnl >= 0 ? '+' : '';
  sportBody.innerHTML += `<tr>
    <td>${{r.sport}}</td>
    <td class="num">${{r.n}}</td>
    <td class="num">${{r.avg_model.toFixed(0)}}%</td>
    <td class="num">${{r.actual_wr.toFixed(0)}}%</td>
    <td class="num ${{pnlClass}}">${{pnlSign}}$${{Math.abs(r.pnl).toFixed(2)}}</td>
  </tr>`;
}});

// ── Pick Log (with methodology column) ──
const picksBody = document.getElementById('picks-body');
picksData.forEach(r => {{
  const outcomeClass = r.outcome === 'win' ? 'win' : r.outcome === 'loss' ? 'loss' : r.outcome === 'push' ? 'push' : 'pending';
  const outcomeText = r.outcome.toUpperCase();
  const stars = '\u2605'.repeat(r.confidence);
  const marketStr = r.market_price !== null ? r.market_price + '\u00a2' : '\u2014';
  const edgeStr = r.edge !== null ? (r.edge > 0 ? '+' : '') + r.edge.toFixed(1) + '%' : '\u2014';
  const edgeClass = r.edge !== null ? (r.edge > 5 ? 'edge-positive' : r.edge < 0 ? 'edge-negative' : '') : '';
  const betStr = r.bet_amount !== null ? '$' + r.bet_amount.toFixed(2) : '\u2014';
  const pnlStr = r.pnl !== null ? (r.pnl >= 0 ? '+' : '') + '$' + Math.abs(r.pnl).toFixed(2) : '\u2014';
  const pnlClass = r.pnl !== null ? (r.pnl >= 0 ? 'win' : 'loss') : 'pending';

  picksBody.innerHTML += `<tr>
    <td class="num">${{r.id}}</td>
    <td>${{r.date}}</td>
    <td>${{methBadge(r.methodology)}}</td>
    <td>${{r.sport}}</td>
    <td>${{r.game}}</td>
    <td>${{r.pick}}</td>
    <td class="num"><span class="stars">${{stars}}</span></td>
    <td class="num">${{r.model_prob.toFixed(0)}}%</td>
    <td class="num">${{marketStr}}</td>
    <td class="num ${{edgeClass}}">${{edgeStr}}</td>
    <td class="num">${{betStr}}</td>
    <td class="${{outcomeClass}}">${{outcomeText}}</td>
    <td class="num ${{pnlClass}}">${{pnlStr}}</td>
  </tr>`;
}});

// ── Methodology Comparison ──
const compSection = document.getElementById('comparison-section');
const allSufficient = comparisonData.every(m => m.sufficient);

if (!allSufficient) {{
  // Show which methodologies lack data
  let msg = comparisonData
    .filter(m => !m.sufficient)
    .map(m => `${{m.label}}: ${{m.settled}} settled picks`)
    .join(', ');
  compSection.innerHTML = `
    <div class="compare-grid">
      ${{comparisonData.map(m => `
        <div class="compare-card" style="border-left: 3px solid ${{m.active ? '#3b82f6' : '#475569'}};">
          <h3><span class="badge ${{m.tag_class}}">${{m.label}}</span> ${{m.active ? '(active)' : '(retired)'}}</h3>
          ${{m.sufficient ? `
            <div class="compare-metric"><span class="compare-metric-label">Win Rate</span><span class="compare-metric-value">${{m.win_rate}}%</span></div>
            <div class="compare-metric"><span class="compare-metric-label">ROI</span><span class="compare-metric-value">${{m.roi >= 0 ? '+' : ''}}${{m.roi}}%</span></div>
            <div class="compare-metric"><span class="compare-metric-label">Avg Bet Size</span><span class="compare-metric-value">$${{m.avg_bet.toFixed(2)}}</span></div>
            <div class="compare-metric"><span class="compare-metric-label">Avg Edge</span><span class="compare-metric-value">${{m.avg_edge !== null ? m.avg_edge + '%' : '\u2014'}}</span></div>
            <div class="compare-metric"><span class="compare-metric-label">Total P&L</span><span class="compare-metric-value ${{m.total_pnl >= 0 ? 'win' : 'loss'}}">${{m.total_pnl >= 0 ? '+' : ''}}$${{Math.abs(m.total_pnl).toFixed(2)}}</span></div>
          ` : `
            <p class="insufficient">Insufficient data \u2014 ${{m.settled}} of 10 settled picks needed</p>
          `}}
        </div>
      `).join('')}}
    </div>`;
}} else {{
  compSection.innerHTML = `
    <div class="compare-grid">
      ${{comparisonData.map(m => `
        <div class="compare-card" style="border-left: 3px solid ${{m.active ? '#3b82f6' : '#475569'}};">
          <h3><span class="badge ${{m.tag_class}}">${{m.label}}</span> ${{m.active ? '(active)' : '(retired)'}}</h3>
          <div class="compare-metric"><span class="compare-metric-label">Win Rate</span><span class="compare-metric-value">${{m.win_rate}}%</span></div>
          <div class="compare-metric"><span class="compare-metric-label">ROI</span><span class="compare-metric-value">${{m.roi >= 0 ? '+' : ''}}${{m.roi}}%</span></div>
          <div class="compare-metric"><span class="compare-metric-label">Avg Bet Size</span><span class="compare-metric-value">$${{m.avg_bet.toFixed(2)}}</span></div>
          <div class="compare-metric"><span class="compare-metric-label">Avg Edge</span><span class="compare-metric-value">${{m.avg_edge !== null ? m.avg_edge + '%' : '\u2014'}}</span></div>
          <div class="compare-metric"><span class="compare-metric-label">Total P&L</span><span class="compare-metric-value ${{m.total_pnl >= 0 ? 'win' : 'loss'}}">${{m.total_pnl >= 0 ? '+' : ''}}$${{Math.abs(m.total_pnl).toFixed(2)}}</span></div>
        </div>
      `).join('')}}
    </div>`;
}}
</script>
</body>
</html>"""


if __name__ == "__main__":
    build_dashboard()
