"""
app.py — EVE Arbitrage Web Interface (Flask)
Interface locale avec formulaire + résultats détaillés
"""

from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'eve_arbitrage'))

import json
import threading
from time import time, strftime, localtime
from flask import Flask, render_template_string, request, jsonify

from market    import ITEMS_PRICES, get_meta, start_reload_loop, REGIONS
from graph     import SolarGraph
from arbitrage import ArbitrageEngine, ArbitrageConfig

# ------------------------------------------------------------------ #
#  Init                                                               #
# ------------------------------------------------------------------ #

app = Flask(__name__)

# Tous les caches dans le même dossier que app.py
_HERE       = os.path.dirname(os.path.abspath(__file__))
GRAPH_CACHE = os.path.join(_HERE, 'graph_cache.json')
NAMES_CACHE = os.path.join(_HERE, 'names_cache.json')

graph = SolarGraph()
engine_instance = ArbitrageEngine(graph, ArbitrageConfig())

_state = {
    "graph_loaded": False,
    "graph_systems": 0,
    "graph_building": False,
    "names": {},          # {id: name} pour items ET systèmes
    "names_loaded": False,
}

# ------------------------------------------------------------------ #
#  Résolution de noms (items + systèmes) via ESI                     #
# ------------------------------------------------------------------ #

import requests as _req

ESI = "https://esi.evetech.net/latest"
ESI_H = {"Accept": "application/json"}

def resolve_names(ids: list[int]) -> dict[int, str]:
    """Résout jusqu'à 1000 IDs en noms via ESI /universe/names/"""
    if not ids:
        return {}
    results = {}
    for i in range(0, len(ids), 999):
        chunk = ids[i:i+999]
        try:
            r = _req.post(f"{ESI}/universe/names/", json=chunk, headers=ESI_H, timeout=15)
            if r.ok:
                for item in r.json():
                    results[item["id"]] = item["name"]
        except Exception as e:
            print(f"[NAMES] Erreur: {e}")
    return results


def _load_names_cache():
    if os.path.exists(NAMES_CACHE):
        with open(NAMES_CACHE) as f:
            raw = json.load(f)
            _state["names"] = {int(k): v for k, v in raw.items()}
            _state["names_loaded"] = True
            print(f"[NAMES] Cache chargé : {len(_state['names'])} noms")


def _save_names_cache():
    with open(NAMES_CACHE, "w") as f:
        json.dump({str(k): v for k, v in _state["names"].items()}, f, separators=(",", ":"))


def get_name(id_: int) -> str:
    if id_ in _state["names"]:
        return _state["names"][id_]
    # Résolution à la volée
    new = resolve_names([id_])
    _state["names"].update(new)
    threading.Thread(target=_save_names_cache, daemon=True).start()
    return _state["names"].get(id_, str(id_))


def enrich_opportunity(opp_dict: dict) -> dict:
    """Ajoute les noms lisibles à une opportunité."""
    ids_to_resolve = []
    item_id = opp_dict["item_id"]
    buy_sys = opp_dict["buy_system"]
    sell_sys = opp_dict["sell_system"]
    path = opp_dict.get("route_path", [])

    all_ids = [item_id, buy_sys, sell_sys] + path
    missing = [i for i in set(all_ids) if i not in _state["names"]]
    if missing:
        new = resolve_names(missing)
        _state["names"].update(new)

    opp_dict["item_name"]       = _state["names"].get(item_id, f"Item #{item_id}")
    opp_dict["buy_system_name"]  = _state["names"].get(buy_sys, f"Sys #{buy_sys}")
    opp_dict["sell_system_name"] = _state["names"].get(sell_sys, f"Sys #{sell_sys}")
    opp_dict["route_names"]      = [_state["names"].get(s, str(s)) for s in path]
    return opp_dict


# ------------------------------------------------------------------ #
#  Chargement graphe                                                  #
# ------------------------------------------------------------------ #

def _load_graph_bg():
    global graph, engine_instance
    _state["graph_building"] = True
    try:
        # 1. Charge depuis cache si dispo, sinon fetch ESI
        if os.path.exists(GRAPH_CACHE):
            print(f"[GRAPH] Cache trouvé : {GRAPH_CACHE}")
            g = SolarGraph.load(GRAPH_CACHE)
        else:
            print(f"[GRAPH] Pas de cache, fetch ESI (plusieurs minutes)...")
            g = SolarGraph()
            g.load_from_esi([r["id"] for r in REGIONS])

        # 2. Le graphe est prêt — on l'active immédiatement
        graph = g
        engine_instance = ArbitrageEngine(graph, ArbitrageConfig())
        _state["graph_loaded"]  = True
        _state["graph_systems"] = graph.system_count()
        print(f"[GRAPH] Prêt : {_state['graph_systems']} systèmes")

        # 3. Sauvegarde du cache (non bloquant si ça plante)
        if not os.path.exists(GRAPH_CACHE):
            try:
                graph.save(GRAPH_CACHE)
            except Exception as e:
                print(f"[GRAPH] Impossible de sauvegarder le cache: {e}")

    except Exception as e:
        print(f"[GRAPH] Erreur chargement: {e}")
        import traceback; traceback.print_exc()
    finally:
        _state["graph_building"] = False


def startup():
    _load_names_cache()
    start_reload_loop()
    threading.Thread(target=_load_graph_bg, daemon=True, name="graph-load").start()


# ------------------------------------------------------------------ #
#  Template HTML                                                      #
# ------------------------------------------------------------------ #

HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EVE Arbitrage Scanner</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #050a0f;
    --bg2:       #080f17;
    --bg3:       #0c1624;
    --panel:     #0a1520;
    --border:    #1a3550;
    --border2:   #0f2035;
    --accent:    #00aaff;
    --accent2:   #0066cc;
    --accent3:   #003d7a;
    --gold:      #f0a500;
    --gold2:     #c07800;
    --green:     #00e676;
    --green2:    #00a152;
    --red:       #ff3d3d;
    --low:       #ff9800;
    --null:      #f44336;
    --high:      #4caf50;
    --text:      #c8dce8;
    --text2:     #7a9cb5;
    --text3:     #3d6070;
    --mono:      'Share Tech Mono', monospace;
    --head:      'Rajdhani', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--head);
    min-height: 100vh;
    background-image:
      radial-gradient(ellipse 80% 50% at 50% -10%, rgba(0,100,200,0.12) 0%, transparent 70%),
      repeating-linear-gradient(0deg, transparent, transparent 39px, rgba(0,100,200,0.03) 39px, rgba(0,100,200,0.03) 40px),
      repeating-linear-gradient(90deg, transparent, transparent 39px, rgba(0,100,200,0.03) 39px, rgba(0,100,200,0.03) 40px);
  }

  /* ── Layout ── */
  .wrap { max-width: 1400px; margin: 0 auto; padding: 0 24px 60px; }

  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 20px 0 16px;
    border-bottom: 1px solid var(--border2);
    margin-bottom: 28px;
  }
  .logo {
    font-size: 22px; font-weight: 700; letter-spacing: 3px; text-transform: uppercase;
    color: var(--accent);
    text-shadow: 0 0 20px rgba(0,170,255,0.4);
  }
  .logo span { color: var(--gold); }
  .status-bar {
    display: flex; gap: 20px; font-family: var(--mono); font-size: 11px; color: var(--text3);
  }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 6px; }
  .dot.ok  { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.err { background: var(--red);   box-shadow: 0 0 6px var(--red); }
  .dot.wait{ background: var(--low);   box-shadow: 0 0 6px var(--low); animation: pulse 1.5s infinite; }

  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

  .grid { display: grid; grid-template-columns: 340px 1fr; gap: 24px; align-items: start; }

  /* ── Panel ── */
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    position: relative; overflow: hidden;
  }
  .panel::before {
    content:''; position:absolute; top:0; left:0; right:0; height:2px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    opacity: .5;
  }
  .panel-title {
    font-size: 11px; font-weight: 700; letter-spacing: 3px;
    text-transform: uppercase; color: var(--text3);
    padding: 14px 18px 12px;
    border-bottom: 1px solid var(--border2);
    display: flex; align-items: center; gap: 8px;
  }
  .panel-title::before {
    content:''; display:block; width:3px; height:12px;
    background: var(--accent); opacity:.7;
  }
  .panel-body { padding: 18px; }

  /* ── Form ── */
  .form-group { margin-bottom: 14px; }
  label {
    display: block; font-size: 10px; font-weight: 600;
    letter-spacing: 2px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 6px;
  }
  input[type=number], input[type=text], select {
    width: 100%; background: var(--bg3);
    border: 1px solid var(--border); color: var(--text);
    font-family: var(--mono); font-size: 13px;
    padding: 8px 12px;
    outline: none; transition: border-color .2s;
  }
  input[type=number]:focus, select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px rgba(0,170,255,0.1);
  }
  select option { background: var(--bg3); }

  .toggle-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 8px 0; border-bottom: 1px solid var(--border2);
  }
  .toggle-row:last-child { border-bottom: none; }
  .toggle-label { font-size: 12px; color: var(--text2); }

  .switch { position: relative; display: inline-block; width: 36px; height: 20px; }
  .switch input { opacity:0; width:0; height:0; }
  .slider {
    position: absolute; cursor: pointer; inset: 0;
    background: var(--bg3); border: 1px solid var(--border);
    transition: .2s;
  }
  .slider:before {
    content:''; position:absolute; width:14px; height:14px;
    left:2px; top:2px; background: var(--text3); transition: .2s;
  }
  input:checked + .slider { background: var(--accent3); border-color: var(--accent); }
  input:checked + .slider:before { transform: translateX(16px); background: var(--accent); }

  .btn {
    width: 100%; padding: 12px;
    background: linear-gradient(135deg, var(--accent3), var(--accent2));
    border: 1px solid var(--accent); color: #fff;
    font-family: var(--head); font-size: 13px; font-weight: 700;
    letter-spacing: 3px; text-transform: uppercase;
    cursor: pointer; transition: all .2s; position: relative; overflow: hidden;
  }
  .btn:hover { background: linear-gradient(135deg, var(--accent2), var(--accent)); box-shadow: 0 0 20px rgba(0,170,255,0.3); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn.scanning::after {
    content:''; position:absolute; top:0; left:-100%; width:100%; height:100%;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,0.1), transparent);
    animation: shimmer 1.2s infinite;
  }
  @keyframes shimmer { to{ left:100% } }

  .hint { font-size: 10px; color: var(--text3); margin-top: 4px; font-family: var(--mono); }

  /* ── Results ── */
  #results-area { min-height: 300px; }

  .results-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 16px;
  }
  .results-count {
    font-size: 12px; color: var(--text3); font-family: var(--mono);
  }
  .results-count strong { color: var(--accent); font-size: 16px; }

  .sort-tabs { display: flex; gap: 4px; }
  .sort-tab {
    padding: 4px 12px; font-size: 10px; font-weight: 700;
    letter-spacing: 1.5px; text-transform: uppercase;
    background: var(--bg3); border: 1px solid var(--border);
    color: var(--text3); cursor: pointer; transition: all .15s;
  }
  .sort-tab.active, .sort-tab:hover { border-color: var(--accent); color: var(--accent); }

  /* ── Opportunity card ── */
  .opp-card {
    background: var(--bg2); border: 1px solid var(--border2);
    margin-bottom: 10px; transition: border-color .15s;
    animation: fadeIn .3s ease both;
  }
  .opp-card:hover { border-color: var(--border); }
  @keyframes fadeIn { from{opacity:0; transform:translateY(6px)} to{opacity:1; transform:translateY(0)} }

  .opp-header {
    display: grid; grid-template-columns: 1fr auto;
    align-items: center; padding: 12px 16px;
    border-bottom: 1px solid var(--border2); cursor: pointer;
    gap: 16px;
  }
  .opp-header:hover { background: rgba(0,100,200,0.04); }

  .item-name {
    font-size: 15px; font-weight: 700; color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .route-summary {
    font-size: 11px; color: var(--text3); margin-top: 3px;
    font-family: var(--mono);
  }
  .route-summary .sys { color: var(--text2); }
  .route-summary .arrow { color: var(--accent3); margin: 0 4px; }

  .scores {
    display: flex; gap: 20px; align-items: center; flex-shrink: 0;
  }
  .score-block { text-align: right; }
  .score-val {
    font-family: var(--mono); font-weight: 700; font-size: 14px;
  }
  .score-val.profit { color: var(--green); }
  .score-val.iskh   { color: var(--gold); }
  .score-val.roi    { color: var(--accent); }
  .score-lbl {
    font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase;
    color: var(--text3); margin-top: 1px;
  }

  /* ── Expanded details ── */
  .opp-detail {
    display: none; padding: 16px;
    background: rgba(5,10,15,0.6);
  }
  .opp-detail.open { display: block; }

  .detail-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin-bottom: 16px;
  }
  .detail-item { }
  .detail-lbl {
    font-size: 9px; letter-spacing: 2px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 4px;
  }
  .detail-val { font-family: var(--mono); font-size: 13px; color: var(--text); }

  /* ── Route path ── */
  .route-path {
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px;
    padding: 10px 14px; background: var(--bg3);
    border: 1px solid var(--border2); margin-top: 8px;
  }
  .hop {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 8px; font-family: var(--mono); font-size: 11px;
  }
  .hop.high { background: rgba(76,175,80,0.08);  border: 1px solid rgba(76,175,80,0.2);  color: var(--high); }
  .hop.low  { background: rgba(255,152,0,0.08);  border: 1px solid rgba(255,152,0,0.2);  color: var(--low); }
  .hop.null { background: rgba(244,67,54,0.08);  border: 1px solid rgba(244,67,54,0.2);  color: var(--null); }
  .hop-arrow { color: var(--text3); font-size: 9px; }

  .sec-badge {
    display: inline-block; padding: 2px 8px; font-size: 9px;
    font-family: var(--mono); font-weight: 700; letter-spacing: 1px;
    margin-left: 4px;
  }
  .sec-badge.high { background: rgba(76,175,80,0.15);  color: var(--high); }
  .sec-badge.low  { background: rgba(255,152,0,0.15);  color: var(--low); }
  .sec-badge.null { background: rgba(244,67,54,0.15);  color: var(--null); }

  .profit-bar {
    display: flex; gap: 0; height: 4px; margin-top: 12px; overflow: hidden;
  }
  .pbar-seg { height: 100%; transition: width .5s; }
  .pbar-gross { background: var(--accent3); }
  .pbar-fees  { background: var(--gold2); }
  .pbar-risk  { background: var(--null); opacity: .6; }
  .pbar-net   { background: var(--green2); }

  /* ── Empty/loading states ── */
  .empty {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    min-height: 260px; color: var(--text3);
    font-family: var(--mono); font-size: 12px; text-align: center; gap: 12px;
  }
  .empty-icon { font-size: 36px; opacity: .3; }

  .spinner {
    width: 32px; height: 32px; border: 2px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%;
    animation: spin 1s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 24px; right: 24px; z-index: 999;
    background: var(--panel); border: 1px solid var(--border);
    padding: 12px 20px; font-size: 12px; font-family: var(--mono);
    opacity: 0; transform: translateY(8px);
    transition: all .3s; pointer-events: none;
  }
  #toast.show { opacity: 1; transform: translateY(0); }

  /* ── Misc ── */
  .sep { border: none; border-top: 1px solid var(--border2); margin: 14px 0; }
  .tag {
    display: inline-block; padding: 2px 7px; font-size: 9px; font-weight: 700;
    letter-spacing: 1.5px; text-transform: uppercase; font-family: var(--mono);
  }
  .tag.safe   { background: rgba(76,175,80,0.12);  color: var(--high); border: 1px solid rgba(76,175,80,0.25); }
  .tag.unsafe { background: rgba(244,67,54,0.12);  color: var(--null); border: 1px solid rgba(244,67,54,0.25); }

  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .scores { gap: 12px; }
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="logo">EVE <span>ARBITRAGE</span> SCANNER</div>
    <div class="status-bar">
      <span id="mkt-status"><span class="dot wait"></span>MARKET —</span>
      <span id="gph-status"><span class="dot wait"></span>GRAPH —</span>
      <span id="clock" style="color:var(--text3)"></span>
    </div>
  </header>

  <div class="grid">

    <!-- ── Formulaire ── -->
    <aside>
      <div class="panel">
        <div class="panel-title">Paramètres</div>
        <div class="panel-body">

          <div class="form-group">
            <label>Profit minimum (ISK)</label>
            <input type="number" id="min_profit" value="2000000" step="500000" min="0">
            <div class="hint">Ex: 2000000 = 2M ISK</div>
          </div>

          <div class="form-group">
            <label>Jumps maximum</label>
            <input type="number" id="max_jumps" value="15" min="1" max="100">
          </div>

          <div class="form-group">
            <label>Top-K offres par item</label>
            <input type="number" id="top_k" value="5" min="1" max="20">
            <div class="hint">Plus élevé = plus lent mais plus complet</div>
          </div>

          <div class="form-group">
            <label>Coût logistique / jump (ISK)</label>
            <input type="number" id="cost_per_jump" value="0" step="100000" min="0">
            <div class="hint">Fuel, Jump Freighter, etc.</div>
          </div>

          <div class="form-group">
            <label>Frais broker (%)</label>
            <input type="number" id="broker_fee" value="3" step="0.1" min="0" max="100">
          </div>

          <div class="form-group">
            <label>Taxe de vente (%)</label>
            <input type="number" id="sales_tax" value="3.6" step="0.1" min="0" max="100">
          </div>

          <div class="form-group">
            <label>Volume minimum</label>
            <input type="number" id="min_volume" value="1" min="1">
          </div>

          <hr class="sep">

          <div class="form-group">
            <label>Sécurité des routes</label>
            <div class="toggle-row">
              <span class="toggle-label" style="color:var(--null)">Éviter Nullsec</span>
              <label class="switch"><input type="checkbox" id="avoid_null" checked><span class="slider"></span></label>
            </div>
            <div class="toggle-row">
              <span class="toggle-label" style="color:var(--low)">Éviter Lowsec</span>
              <label class="switch"><input type="checkbox" id="avoid_low"><span class="slider"></span></label>
            </div>
          </div>

          <hr class="sep">

          <div class="form-group">
            <label>Trier par</label>
            <select id="sort_by">
              <option value="isk_per_hour" selected>ISK / Heure</option>
              <option value="net_profit">Profit net</option>
              <option value="isk_per_jump">ISK / Jump</option>
              <option value="roi_pct">ROI %</option>
            </select>
          </div>

          <div class="form-group">
            <label>Limite de résultats</label>
            <input type="number" id="limit" value="30" min="1" max="200">
          </div>

          <button class="btn" id="scan-btn" onclick="doScan()">
            ⬡ Lancer le scan
          </button>

        </div>
      </div>
    </aside>

    <!-- ── Résultats ── -->
    <main>
      <div id="results-area">
        <div class="empty">
          <div class="empty-icon">◈</div>
          <div>Configurez les paramètres et lancez le scan</div>
          <div style="color:var(--text3);font-size:10px">Les données de marché se chargent en arrière-plan</div>
        </div>
      </div>
    </main>

  </div>
</div>

<div id="toast"></div>

<script>
// ── État local ──
let currentOpps = [];
let currentSort = 'isk_per_hour';

// ── Clock ──
setInterval(() => {
  const d = new Date();
  document.getElementById('clock').textContent =
    d.toUTCString().slice(17,25) + ' UTC';
}, 1000);

// ── Status polling ──
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();

    const mkt = document.getElementById('mkt-status');
    const gph = document.getElementById('gph-status');

    if (d.market_ready) {
      mkt.innerHTML = `<span class="dot ok"></span>MARKET — ${d.market_items.toLocaleString()} items`;
    } else {
      mkt.innerHTML = `<span class="dot wait"></span>MARKET — chargement...`;
    }
    if (d.graph_ready) {
      gph.innerHTML = `<span class="dot ok"></span>GRAPH — ${d.graph_systems.toLocaleString()} systèmes`;
    } else if (d.graph_building) {
      gph.innerHTML = `<span class="dot wait"></span>GRAPH — construction...`;
    } else {
      gph.innerHTML = `<span class="dot err"></span>GRAPH — non chargé`;
    }
  } catch(e) {}
}
pollStatus();
setInterval(pollStatus, 5000);

// ── Scan ──
async function doScan() {
  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  btn.classList.add('scanning');
  btn.textContent = '⬡ Scan en cours...';

  const params = {
    min_profit:    parseFloat(document.getElementById('min_profit').value),
    max_jumps:     parseInt(document.getElementById('max_jumps').value),
    top_k:         parseInt(document.getElementById('top_k').value),
    cost_per_jump: parseFloat(document.getElementById('cost_per_jump').value),
    broker_fee:    parseFloat(document.getElementById('broker_fee').value) / 100,
    sales_tax:     parseFloat(document.getElementById('sales_tax').value) / 100,
    min_volume:    parseInt(document.getElementById('min_volume').value),
    avoid_null:    document.getElementById('avoid_null').checked,
    avoid_low:     document.getElementById('avoid_low').checked,
    sort_by:       document.getElementById('sort_by').value,
    limit:         parseInt(document.getElementById('limit').value),
  };

  showLoading();

  try {
    const r = await fetch('/api/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(params)
    });
    const d = await r.json();

    if (!r.ok) {
      showError(d.error || 'Erreur serveur');
    } else {
      currentOpps = d.opportunities;
      currentSort = params.sort_by;
      renderResults(d);
    }
  } catch(e) {
    showError('Erreur réseau : ' + e.message);
  }

  btn.disabled = false;
  btn.classList.remove('scanning');
  btn.textContent = '⬡ Lancer le scan';
}

// ── Render ──
function renderResults(data) {
  const area = document.getElementById('results-area');

  if (!data.opportunities || data.opportunities.length === 0) {
    area.innerHTML = `<div class="empty">
      <div class="empty-icon">◈</div>
      <div>Aucune opportunité trouvée</div>
      <div style="font-size:10px;color:var(--text3)">Essayez d'abaisser le profit minimum ou d'augmenter les jumps max</div>
    </div>`;
    return;
  }

  let html = `<div class="results-header">
    <div class="results-count">
      <strong>${data.opportunities.length}</strong> opportunités &nbsp;·&nbsp;
      scan en ${data.elapsed_s}s &nbsp;·&nbsp;
      ${data.market_items?.toLocaleString() || '?'} items indexés
    </div>
  </div>`;

  data.opportunities.forEach((opp, i) => {
    html += renderCard(opp, i);
  });

  area.innerHTML = html;
}

function fmt(n) {
  if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'K';
  return n.toFixed(0);
}

function secClass(profile) {
  if ((profile.null || 0) > 0) return 'null';
  if ((profile.low  || 0) > 0) return 'low';
  return 'high';
}

function renderCard(opp, idx) {
  const sc = secClass(opp.sec_profile);
  const safe = opp.is_safe;

  // Barre de profit
  const gross = opp.gross_profit;
  const feesPct = gross > 0 ? Math.min(opp.fees / gross * 100, 50) : 0;
  const riskPct = gross > 0 ? Math.min(opp.route_risk_cost / gross * 100, 50) : 0;
  const netPct  = gross > 0 ? Math.max((opp.net_profit / gross * 100), 0) : 0;

  // Route hops
  let hopsHtml = '';
  const names = opp.route_names || [];
  const secs  = opp.route_sec_detail || [];
  names.forEach((name, i) => {
    const s = secs[i] || 'high';
    if (i > 0) hopsHtml += `<span class="hop-arrow">▶</span>`;
    hopsHtml += `<span class="hop ${s}">${name}</span>`;
  });

  return `
  <div class="opp-card" id="card-${idx}">
    <div class="opp-header" onclick="toggleDetail(${idx})">
      <div>
        <div class="item-name">${opp.item_name}</div>
        <div class="route-summary">
          <span class="sys">${opp.buy_system_name}</span>
          <span class="arrow">──${opp.jumps}j──▶</span>
          <span class="sys">${opp.sell_system_name}</span>
          &nbsp;
          <span class="sec-badge ${sc}">${sc.toUpperCase()}</span>
          ${safe ? '<span class="tag safe">SAFE</span>' : '<span class="tag unsafe">RISKY</span>'}
        </div>
      </div>
      <div class="scores">
        <div class="score-block">
          <div class="score-val iskh">${fmt(opp.isk_per_hour)}/h</div>
          <div class="score-lbl">ISK/heure</div>
        </div>
        <div class="score-block">
          <div class="score-val profit">${fmt(opp.net_profit)}</div>
          <div class="score-lbl">Net profit</div>
        </div>
        <div class="score-block">
          <div class="score-val roi">${opp.roi_pct.toFixed(1)}%</div>
          <div class="score-lbl">ROI</div>
        </div>
      </div>
    </div>

    <div class="opp-detail" id="detail-${idx}">
      <div class="detail-grid">
        <div class="detail-item">
          <div class="detail-lbl">Item (type_id)</div>
          <div class="detail-val">${opp.item_name} <span style="color:var(--text3);font-size:11px">#${opp.item_id}</span></div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Prix achat (sell order)</div>
          <div class="detail-val">${opp.buy_price.toLocaleString()} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Prix vente (buy order)</div>
          <div class="detail-val">${opp.sell_price.toLocaleString()} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Volume échangeable</div>
          <div class="detail-val">${opp.volume.toLocaleString()} unités</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Profit brut</div>
          <div class="detail-val" style="color:var(--text2)">${fmt(opp.gross_profit)} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Frais (broker + taxe)</div>
          <div class="detail-val" style="color:var(--low)">− ${fmt(opp.fees)} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Coût de route (risque)</div>
          <div class="detail-val" style="color:var(--null)">− ${fmt(opp.route_risk_cost)} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Profit net</div>
          <div class="detail-val" style="color:var(--green);font-size:15px">${fmt(opp.net_profit)} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">ISK / Jump</div>
          <div class="detail-val">${fmt(opp.isk_per_jump)}</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Investissement total</div>
          <div class="detail-val">${fmt(opp.total_investment)} ISK</div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Profil sécurité</div>
          <div class="detail-val">
            <span style="color:var(--high)">HS:${opp.sec_profile.high||0}</span>
            <span style="color:var(--low)"> LS:${opp.sec_profile.low||0}</span>
            <span style="color:var(--null)"> NS:${opp.sec_profile.null||0}</span>
          </div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Système départ</div>
          <div class="detail-val">${opp.buy_system_name} <span style="color:var(--text3);font-size:11px">#${opp.buy_system}</span></div>
        </div>
        <div class="detail-item">
          <div class="detail-lbl">Système arrivée</div>
          <div class="detail-val">${opp.sell_system_name} <span style="color:var(--text3);font-size:11px">#${opp.sell_system}</span></div>
        </div>
      </div>

      <div class="detail-lbl" style="margin-bottom:6px">Route complète (${opp.jumps} jumps)</div>
      <div class="route-path">${hopsHtml}</div>

      <div class="profit-bar" title="Gross → Fees → Risk → Net">
        <div class="pbar-seg pbar-gross" style="width:${100-feesPct-riskPct}%"></div>
        <div class="pbar-seg pbar-fees"  style="width:${feesPct}%"></div>
        <div class="pbar-seg pbar-risk"  style="width:${riskPct}%"></div>
      </div>
      <div style="font-size:9px;color:var(--text3);font-family:var(--mono);margin-top:4px">
        <span style="color:var(--accent3)">■</span> Brut &nbsp;
        <span style="color:var(--gold2)">■</span> Frais &nbsp;
        <span style="color:var(--null);opacity:.6">■</span> Risque route
      </div>
    </div>
  </div>`;
}

function toggleDetail(idx) {
  const d = document.getElementById(`detail-${idx}`);
  d.classList.toggle('open');
}

function showLoading() {
  document.getElementById('results-area').innerHTML = `
    <div class="empty">
      <div class="spinner"></div>
      <div>Scan en cours...</div>
      <div style="font-size:10px;color:var(--text3)">Calcul des routes et du profit net</div>
    </div>`;
}

function showError(msg) {
  document.getElementById('results-area').innerHTML = `
    <div class="empty">
      <div class="empty-icon" style="color:var(--red)">⚠</div>
      <div style="color:var(--red)">${msg}</div>
    </div>`;
}

function toast(msg, dur=3000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), dur);
}
</script>
</body>
</html>"""


# ------------------------------------------------------------------ #
#  Routes Flask                                                       #
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/status")
def api_status():
    meta = get_meta()
    return jsonify({
        "market_ready":   len(ITEMS_PRICES) > 0,
        "market_items":   len(ITEMS_PRICES),
        "market_orders":  meta.get("total_orders", 0),
        "market_updated": meta.get("last_updated"),
        "graph_ready":    _state["graph_loaded"],
        "graph_systems":  _state["graph_systems"],
        "graph_building": _state["graph_building"],
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json()

    if not ITEMS_PRICES:
        return jsonify({"error": "Index marché pas encore chargé. Patiente quelques secondes."}), 503
    if not _state["graph_loaded"]:
        return jsonify({"error": "Graphe de systèmes pas encore chargé."}), 503

    cfg = ArbitrageConfig(
        min_profit_isk = float(data.get("min_profit", 2_000_000)),
        max_jumps      = int(data.get("max_jumps", 15)),
        top_k_offers   = int(data.get("top_k", 5)),
        cost_per_jump  = float(data.get("cost_per_jump", 0)),
        broker_fee_pct = float(data.get("broker_fee", 0.03)),
        sales_tax_pct  = float(data.get("sales_tax", 0.036)),
        min_volume     = int(data.get("min_volume", 1)),
        avoid_null     = bool(data.get("avoid_null", False)),
        avoid_low      = bool(data.get("avoid_low", False)),
    )
    sort_by = data.get("sort_by", "isk_per_hour")
    limit   = int(data.get("limit", 50))

    t0 = time()
    eng = ArbitrageEngine(graph, cfg)
    opps = eng.scan(ITEMS_PRICES, sort_by=sort_by, limit=limit)
    elapsed = round(time() - t0, 3)

    # Enrichissement des noms + détail sécurité par hop
    enriched = []
    all_ids = []
    for o in opps:
        d = o.to_dict()
        all_ids += [d["item_id"], d["buy_system"], d["sell_system"]] + d["route_path"]

    missing = [i for i in set(all_ids) if i not in _state["names"]]
    if missing:
        new_names = resolve_names(missing)
        _state["names"].update(new_names)
        threading.Thread(target=_save_names_cache, daemon=True).start()

    for o in opps:
        d = o.to_dict()
        d["item_name"]        = _state["names"].get(d["item_id"],     f"#{d['item_id']}")
        d["buy_system_name"]  = _state["names"].get(d["buy_system"],  f"#{d['buy_system']}")
        d["sell_system_name"] = _state["names"].get(d["sell_system"], f"#{d['sell_system']}")
        d["route_names"]      = [_state["names"].get(s, str(s)) for s in d["route_path"]]
        d["route_risk_cost"]  = o.route.risk_cost

        # Profil de sécurité par hop pour colorisation
        sec_detail = []
        for sid in d["route_path"]:
            sys = graph.get_system(sid)
            sec_detail.append(sys.sec_bucket if sys else "high")
        d["route_sec_detail"] = sec_detail

        enriched.append(d)

    return jsonify({
        "count":         len(enriched),
        "elapsed_s":     elapsed,
        "market_items":  len(ITEMS_PRICES),
        "opportunities": enriched,
    })


# ------------------------------------------------------------------ #
#  Main                                                               #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    startup()
    print("\n[APP] Interface disponible sur http://localhost:5000\n")
    app.run(debug=False, host="0.0.0.0", port=5000, use_reloader=False)
