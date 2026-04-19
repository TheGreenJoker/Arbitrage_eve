"""
api.py — API FastAPI pour le moteur d'arbitrage EVE Online
Endpoints :
  GET  /health                       état du service
  GET  /market/status                état de l'index marché
  GET  /graph/status                 état du graphe de systèmes
  POST /graph/build                  (re)construit le graphe via ESI
  GET  /arbitrage/scan               scan complet (avec filtres)
  GET  /arbitrage/item/{type_id}     scan ciblé sur un item
  GET  /arbitrage/config             config actuelle
  POST /arbitrage/config             met à jour la config du moteur
"""

from __future__ import annotations
import os
from contextlib import asynccontextmanager
from time import time

from fastapi import FastAPI, Query, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from market    import ITEMS_PRICES, get_meta, start_reload_loop, REGIONS
from graph     import SolarGraph
from arbitrage import ArbitrageEngine, ArbitrageConfig

# ------------------------------------------------------------------ #
#  State global                                                       #
# ------------------------------------------------------------------ #

GRAPH_CACHE_PATH = os.environ.get("GRAPH_CACHE", "graph_cache.json")
REGION_IDS       = [r["id"] for r in REGIONS]

graph  = SolarGraph()
config = ArbitrageConfig()
engine = ArbitrageEngine(graph, config)

_graph_meta = {
    "loaded":       False,
    "system_count": 0,
    "building":     False,
    "error":        None,
    "source":       None,   # "cache" | "esi"
}


def _load_graph():
    global graph, engine
    _graph_meta["building"] = True
    _graph_meta["error"]    = None
    try:
        graph  = SolarGraph.load_or_fetch(GRAPH_CACHE_PATH, REGION_IDS)
        engine = ArbitrageEngine(graph, config)
        _graph_meta["loaded"]       = True
        _graph_meta["system_count"] = graph.system_count()
        _graph_meta["source"]       = "cache" if os.path.exists(GRAPH_CACHE_PATH) else "esi"
    except Exception as e:
        _graph_meta["error"] = str(e)
        print(f"[GRAPH] Erreur chargement: {e}")
    finally:
        _graph_meta["building"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[API] Démarrage...")
    start_reload_loop()
    _load_graph()
    yield
    print("[API] Arrêt.")


app = FastAPI(
    title="EVE Arbitrage Engine",
    description="Détection d'opportunités d'arbitrage inter-systèmes EVE Online",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------ #
#  Schémas Pydantic                                                   #
# ------------------------------------------------------------------ #

class ConfigUpdate(BaseModel):
    min_profit_isk: float = Field(default=2_000_000, ge=0)
    max_jumps:      int   = Field(default=15,         ge=1, le=100)
    top_k_offers:   int   = Field(default=5,          ge=1, le=20)
    cost_per_jump:  float = Field(default=0.0,        ge=0)
    avoid_null:     bool  = False
    avoid_low:      bool  = False
    min_volume:     int   = Field(default=1,          ge=1)
    broker_fee_pct: float = Field(default=0.03,       ge=0, le=1)
    sales_tax_pct:  float = Field(default=0.036,      ge=0, le=1)


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

def _assert_ready():
    if not ITEMS_PRICES:
        raise HTTPException(503, "Index marché non encore chargé.")
    if not _graph_meta["loaded"]:
        raise HTTPException(503, "Graphe de systèmes non encore chargé.")


# ------------------------------------------------------------------ #
#  Endpoints système                                                  #
# ------------------------------------------------------------------ #

@app.get("/health", tags=["système"])
def health():
    return {
        "status":       "ok",
        "timestamp":    time(),
        "market_ready": len(ITEMS_PRICES) > 0,
        "graph_ready":  _graph_meta["loaded"],
    }


@app.get("/market/status", tags=["marché"])
def market_status():
    meta = get_meta()
    return {**meta, "items_in_index": len(ITEMS_PRICES), "ready": len(ITEMS_PRICES) > 0}


@app.get("/graph/status", tags=["graphe"])
def graph_status():
    return {
        **_graph_meta,
        "cache_file":   GRAPH_CACHE_PATH,
        "cache_exists": os.path.exists(GRAPH_CACHE_PATH),
        "route_cache":  graph.cache_stats(),
    }


@app.post("/graph/build", tags=["graphe"])
def graph_build(background_tasks: BackgroundTasks,
                force: bool = Query(default=False)):
    """
    (Re)construit le graphe depuis l'ESI en arrière-plan.
    force=true : supprime le cache local et refetch tout (~1h).
    """
    if _graph_meta["building"]:
        raise HTTPException(409, "Construction déjà en cours.")
    if force and os.path.exists(GRAPH_CACHE_PATH):
        os.remove(GRAPH_CACHE_PATH)
    background_tasks.add_task(_load_graph)
    return {"status": "started", "message": "Construction lancée en arrière-plan."}


# ------------------------------------------------------------------ #
#  Endpoints arbitrage                                                #
# ------------------------------------------------------------------ #

@app.get("/arbitrage/scan", tags=["arbitrage"])
def scan(
    sort_by:    str   = Query(default="isk_per_hour",
                              description="net_profit | isk_per_jump | isk_per_hour | roi_pct"),
    limit:      int   = Query(default=50,  ge=1, le=500),
    min_profit: float = Query(default=None, description="Override min_profit_isk (ISK)"),
    max_jumps:  int   = Query(default=None, description="Override max_jumps"),
    safe_only:  bool  = Query(default=False, description="Highsec uniquement"),
):
    _assert_ready()

    if sort_by not in {"net_profit", "isk_per_jump", "isk_per_hour", "roi_pct"}:
        raise HTTPException(400, "sort_by invalide.")

    local_cfg = ArbitrageConfig(
        min_profit_isk = min_profit if min_profit is not None else config.min_profit_isk,
        max_jumps      = max_jumps  if max_jumps  is not None else config.max_jumps,
        top_k_offers   = config.top_k_offers,
        cost_per_jump  = config.cost_per_jump,
        avoid_null     = True if safe_only else config.avoid_null,
        avoid_low      = True if safe_only else config.avoid_low,
        min_volume     = config.min_volume,
        broker_fee_pct = config.broker_fee_pct,
        sales_tax_pct  = config.sales_tax_pct,
    )
    t0 = time()
    results = ArbitrageEngine(graph, local_cfg).scan(ITEMS_PRICES, sort_by=sort_by, limit=limit)
    return {
        "count":         len(results),
        "elapsed_s":     round(time() - t0, 3),
        "sort_by":       sort_by,
        "opportunities": [o.to_dict() for o in results],
    }


@app.get("/arbitrage/item/{type_id}", tags=["arbitrage"])
def scan_item(type_id: int, sort_by: str = Query(default="isk_per_hour")):
    _assert_ready()
    if type_id not in ITEMS_PRICES:
        raise HTTPException(404, f"Item {type_id} introuvable.")
    t0 = time()
    results = engine.scan_item(type_id, ITEMS_PRICES, sort_by=sort_by)
    return {
        "type_id":       type_id,
        "count":         len(results),
        "elapsed_s":     round(time() - t0, 3),
        "opportunities": [o.to_dict() for o in results],
    }


@app.get("/arbitrage/config", tags=["configuration"])
def get_config():
    return config.__dict__


@app.post("/arbitrage/config", tags=["configuration"])
def update_config(body: ConfigUpdate):
    global config, engine
    config = ArbitrageConfig(**body.model_dump())
    engine = ArbitrageEngine(graph, config)
    return {"status": "updated", "config": body.model_dump()}
