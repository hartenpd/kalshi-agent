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


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

    # ── Summary stats ────────────────────────────────────────────────
    total_picks = len(picks)
    traded_picks = [p for p in picks if p["bet_placed"]]
    total_traded = len(traded_picks)

    settled = [p for p in picks if p["outcome"] in ("win", "loss")]
    wins = sum(1 for p in settled if p["outcome"] == "win")
    losses = sum(1 for p in settled if p["outcome"] == "loss")
    win_rate = wins / len(settled) * 100 if settled else 0

    bets_with_pnl = [p for p in picks if p["bet_placed"] and p["pnl"] is not None]
    total_pnl = sum(p["pnl"] for p in bets_with_pnl)
    total_wagered = sum(p["bet_amount"] for p in bets_with_pnl if p["bet_amount"])
    roi = (total_pnl / total_wagered * 100) if total_wagered > 0 else 0

    # Best and worst single trade
    best_trade = max(bets_with_pnl, key=lambda p: p["pnl"]) if bets_with_pnl else None
    worst_trade = min(bets_with_pnl, key=lambda p: p["pnl"]) if bets_with_pnl else None

    # ── Calibration data ─────────────────────────────────────────────
    # By confidence level
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
            "stars": stars,
            "n": n,
            "wins": bucket_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_model": round(avg_model, 1),
            "diff": round(diff, 1),
        })

    # By sport
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
            "sport": sport,
            "n": n,
            "wins": sport_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_model": round(avg_model, 1),
            "pnl": round(sport_pnl, 2),
        })

    # ── Edge analysis ────────────────────────────────────────────────
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
            "label": label,
            "n": n,
            "wins": bucket_wins,
            "actual_wr": round(actual_wr, 1),
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": round(total_bucket_pnl, 2),
            "bets": len(bucket_bets),
        })

    # Also include negative edge for comparison
    neg_edge = [p for p in edge_picks if p["edge"] < 0.02]
    if neg_edge:
        n = len(neg_edge)
        neg_wins = sum(1 for p in neg_edge if p["outcome"] == "win")
        neg_bets = [p for p in neg_edge if p["bet_placed"] and p["pnl"] is not None]
        neg_pnl = sum(p["pnl"] for p in neg_bets) / len(neg_bets) if neg_bets else 0
        edge_analysis.insert(0, {
            "label": "<2%",
            "n": n,
            "wins": neg_wins,
            "actual_wr": round(neg_wins / n * 100, 1),
            "avg_pnl": round(neg_pnl, 2),
            "total_pnl": round(sum(p["pnl"] for p in neg_bets), 2) if neg_bets else 0,
            "bets": len(neg_bets),
        })

    # ── Pick log for the table ───────────────────────────────────────
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
        })

    # ── Generate timestamp ───────────────────────────────────────────
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Build the HTML ───────────────────────────────────────────────
    html = _build_html(
        total_picks=total_picks,
        total_traded=total_traded,
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 1),
        total_pnl=round(total_pnl, 2),
        total_wagered=round(total_wagered, 2),
        roi=round(roi, 1),
        best_trade=best_trade,
        worst_trade=worst_trade,
        cal_by_conf=cal_by_conf,
        cal_by_sport=cal_by_sport,
        edge_analysis=edge_analysis,
        pick_rows=pick_rows,
        generated_at=generated_at,
    )

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)

    print(f"Dashboard generated: {OUTPUT_PATH}")
    print(f"  {total_picks} picks, {total_traded} traded, ${total_pnl:+.2f} P&L")


def _build_html(**data):
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

    # Serialize data for JS
    cal_conf_json = json.dumps(data["cal_by_conf"])
    cal_sport_json = json.dumps(data["cal_by_sport"])
    edge_json = json.dumps(data["edge_analysis"])
    picks_json = json.dumps(data["pick_rows"])

    pnl_color = "#4ade80" if data["total_pnl"] >= 0 else "#f87171"
    pnl_sign = "+" if data["total_pnl"] >= 0 else ""
    roi_color = "#4ade80" if data["roi"] >= 0 else "#f87171"
    roi_sign = "+" if data["roi"] >= 0 else ""

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

/* Stars */
.stars {{ color: #f59e0b; letter-spacing: 1px; }}

/* Edge bucket highlight */
.edge-positive {{ color: #4ade80; }}
.edge-negative {{ color: #f87171; }}

/* Responsive */
@media (max-width: 640px) {{
  body {{ padding: 12px; }}
  .cards {{ grid-template-columns: 1fr 1fr; }}
  table {{ font-size: 0.78rem; }}
  td, th {{ padding: 6px 8px; }}
}}
</style>
</head>
<body>

<h1>Kalshi Agent Dashboard</h1>
<p class="subtitle">Generated {data["generated_at"]} &middot; {data["total_picks"]} picks tracked</p>

<!-- ═══ Summary Cards ═══ -->
<div class="cards">
  <div class="card">
    <div class="card-label">Picks</div>
    <div class="card-value">{data["total_picks"]}</div>
    <div class="card-detail">{data["total_traded"]} traded &middot; {data["total_picks"] - data["total_traded"]} passed</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value">{data["win_rate"]}%</div>
    <div class="card-detail">{data["wins"]}W / {data["losses"]}L</div>
  </div>
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-value" style="color: {pnl_color}">{pnl_sign}${abs(data["total_pnl"]):.2f}</div>
    <div class="card-detail">${data["total_wagered"]:.2f} wagered &middot; {roi_sign}{data["roi"]}% ROI</div>
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

<!-- ═══ Calibration by Confidence ═══ -->
<h2>Calibration by Confidence</h2>
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
<tbody id="cal-conf-body"></tbody>
</table>

<!-- ═══ Calibration by Sport ═══ -->
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

<!-- ═══ Edge Analysis ═══ -->
<h2>Edge Analysis</h2>
<p class="subtitle" style="margin-bottom: 12px">Are higher-edge picks actually more profitable?</p>
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
<tbody id="edge-body"></tbody>
</table>

<!-- ═══ Pick Log ═══ -->
<h2>Pick Log</h2>
<table>
<thead>
  <tr>
    <th>#</th>
    <th>Date</th>
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

<p class="subtitle" style="margin-top: 24px; text-align: center;">
  Kalshi Sports Agent &middot; Built with Claude Code
</p>

<script>
const calConf = {cal_conf_json};
const calSport = {cal_sport_json};
const edgeData = {edge_json};
const picksData = {picks_json};

// ── Calibration by Confidence ──
const confBody = document.getElementById('cal-conf-body');
calConf.forEach(r => {{
  const stars = '★'.repeat(r.stars) + '☆'.repeat(5 - r.stars);
  const diff = r.diff;
  let badge;
  if (Math.abs(diff) < 5) badge = '<span class="badge badge-good">well calibrated</span>';
  else if (diff > 0) badge = `<span class="badge badge-warn">underconfident ${{Math.abs(diff).toFixed(0)}}%</span>`;
  else badge = `<span class="badge badge-bad">overconfident ${{Math.abs(diff).toFixed(0)}}%</span>`;

  confBody.innerHTML += `<tr>
    <td><span class="stars">${{stars}}</span></td>
    <td class="num">${{r.n}}</td>
    <td class="num">${{r.avg_model.toFixed(0)}}%</td>
    <td class="num">${{r.actual_wr.toFixed(0)}}%</td>
    <td>${{badge}}</td>
  </tr>`;
}});

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

// ── Edge Analysis ──
const edgeBody = document.getElementById('edge-body');
edgeData.forEach(r => {{
  const wrClass = r.actual_wr >= 50 ? 'edge-positive' : 'edge-negative';
  const pnlClass = r.total_pnl >= 0 ? 'win' : 'loss';
  const pnlSign = r.total_pnl >= 0 ? '+' : '';
  const avgSign = r.avg_pnl >= 0 ? '+' : '';
  edgeBody.innerHTML += `<tr>
    <td>${{r.label}}</td>
    <td class="num">${{r.n}}</td>
    <td class="num ${{wrClass}}">${{r.actual_wr.toFixed(0)}}%</td>
    <td class="num">${{r.bets}}</td>
    <td class="num">${{avgSign}}$${{Math.abs(r.avg_pnl).toFixed(2)}}</td>
    <td class="num ${{pnlClass}}">${{pnlSign}}$${{Math.abs(r.total_pnl).toFixed(2)}}</td>
  </tr>`;
}});

// ── Pick Log ──
const picksBody = document.getElementById('picks-body');
picksData.forEach(r => {{
  const outcomeClass = r.outcome === 'win' ? 'win' : r.outcome === 'loss' ? 'loss' : r.outcome === 'push' ? 'push' : 'pending';
  const outcomeText = r.outcome.toUpperCase();
  const stars = '★'.repeat(r.confidence);
  const marketStr = r.market_price !== null ? r.market_price + '¢' : '—';
  const edgeStr = r.edge !== null ? (r.edge > 0 ? '+' : '') + r.edge.toFixed(1) + '%' : '—';
  const edgeClass = r.edge !== null ? (r.edge > 5 ? 'edge-positive' : r.edge < 0 ? 'edge-negative' : '') : '';
  const betStr = r.bet_amount !== null ? '$' + r.bet_amount.toFixed(2) : '—';
  const pnlStr = r.pnl !== null ? (r.pnl >= 0 ? '+' : '') + '$' + Math.abs(r.pnl).toFixed(2) : '—';
  const pnlClass = r.pnl !== null ? (r.pnl >= 0 ? 'win' : 'loss') : 'pending';

  picksBody.innerHTML += `<tr>
    <td class="num">${{r.id}}</td>
    <td>${{r.date}}</td>
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
</script>
</body>
</html>"""


if __name__ == "__main__":
    build_dashboard()
