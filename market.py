"""
market.py — Ingestion et indexation des ordres de marché ESI
Structure finale : ITEMS_PRICES[type_id][system_id] = {"buy": [...], "sell": [...]}
"""

from __future__ import annotations
import requests
from threading import Lock, Thread
from time import sleep, time

ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {
    "Accept": "application/json",
    "X-Compatibility-Date": "2025-12-16",
}

RANGE_JUMPS: dict[str, int] = {
    "station":     0,
    "solarsystem": 0,
    "1":  1, "2":  2, "3":  3, "4":  4,
    "5":  5, "10": 10, "20": 20,
    "region": -1,
}

REGIONS = [
    {"name": "Domain",         "id": 10000043},
    {"name": "Kador",          "id": 10000052},
    {"name": "Kor-Azor",       "id": 10000065},
    {"name": "Tash-Murkon",    "id": 10000020},
    {"name": "Aridia",         "id": 10000054},
    {"name": "Devoid",         "id": 10000036},
    {"name": "The Forge",      "id": 10000002},
    {"name": "Lonetrek",       "id": 10000016},
    {"name": "The Citadel",    "id": 10000033},
    {"name": "Black Rise",     "id": 10000069},
    {"name": "Sinq Laison",    "id": 10000032},
    {"name": "Essence",        "id": 10000064},
    {"name": "Verge Vendor",   "id": 10000068},
    {"name": "Placid",         "id": 10000048},
    {"name": "Heimatar",       "id": 10000030},
    {"name": "Metropolis",     "id": 10000042},
    {"name": "Molden Heath",   "id": 10000028},
    {"name": "The Bleak Lands","id": 10000038},
]


# ------------------------------------------------------------------ #
#  État global (partagé avec l'API et le moteur)                      #
# ------------------------------------------------------------------ #

# [type_id][system_id] = {"buy": [orders...], "sell": [orders...]}
ITEMS_PRICES: dict[int, dict[int, dict[str, list]]] = {}
_prices_lock = Lock()

_meta = {
    "last_updated":   None,   # timestamp UNIX
    "total_orders":   0,
    "total_items":    0,
    "loading":        False,
    "error":          None,
}


def get_meta() -> dict:
    return dict(_meta)


# ------------------------------------------------------------------ #
#  Fetch ESI                                                          #
# ------------------------------------------------------------------ #

def _fetch_region_orders(region_id: int, region_name: str) -> list[dict]:
    orders = []
    url = f"{ESI_BASE}/markets/{region_id}/orders/"
    params: dict = {"order_type": "all", "page": 1}

    resp = requests.get(url, headers=ESI_HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    orders.extend(resp.json())

    total_pages = int(resp.headers.get("X-Pages", 1))
    for page in range(2, total_pages + 1):
        params["page"] = page
        r = requests.get(url, headers=ESI_HEADERS, params=params, timeout=15)
        r.raise_for_status()
        orders.extend(r.json())

    print(f"[ESI] {region_name}: {len(orders)} ordres ({total_pages} pages)")
    return orders


# ------------------------------------------------------------------ #
#  Construction de l'index                                            #
# ------------------------------------------------------------------ #

def _build_index(all_orders: list[dict]) -> dict[int, dict[int, dict[str, list]]]:
    """
    Construit l'index [type_id][system_id] = {"buy": [...], "sell": [...]}.
    Trie les offres à la construction (sell ASC, buy DESC).
    """
    index: dict[int, dict[int, dict[str, list]]] = {}

    for order in all_orders:
        type_id   = order["type_id"]
        system_id = order["system_id"]
        side      = "buy" if order["is_buy_order"] else "sell"

        order["range_jumps"] = RANGE_JUMPS.get(str(order.get("range", "station")), 0)

        if type_id not in index:
            index[type_id] = {}
        if system_id not in index[type_id]:
            index[type_id][system_id] = {"buy": [], "sell": []}

        index[type_id][system_id][side].append(order)

    # Tri final unique à la construction
    for systems in index.values():
        for sides in systems.values():
            sides["sell"].sort(key=lambda o: o["price"])
            sides["buy"].sort(key=lambda o: o["price"], reverse=True)

    return index


# ------------------------------------------------------------------ #
#  Boucle de rechargement                                             #
# ------------------------------------------------------------------ #

def _reload_once(regions: list[dict]) -> None:
    _meta["loading"] = True
    _meta["error"]   = None
    all_orders: list[dict] = []

    for region in regions:
        try:
            orders = _fetch_region_orders(region["id"], region["name"])
            all_orders.extend(orders)
        except Exception as e:
            print(f"[ESI] Erreur région {region['name']}: {e}")
            _meta["error"] = str(e)

    if not all_orders:
        _meta["loading"] = False
        return

    new_index = _build_index(all_orders)

    # Swap atomique : jamais de dict vide visible depuis l'extérieur
    with _prices_lock:
        ITEMS_PRICES.clear()
        ITEMS_PRICES.update(new_index)

    _meta["last_updated"] = time()
    _meta["total_orders"] = len(all_orders)
    _meta["total_items"]  = len(ITEMS_PRICES)
    _meta["loading"]      = False

    print(f"[INDEX] {_meta['total_items']} items, {_meta['total_orders']} ordres")


def start_reload_loop(regions: list[dict] = REGIONS, interval: int = 300) -> None:
    """Lance la boucle de rechargement dans un thread daemon."""
    def loop():
        while True:
            try:
                _reload_once(regions)
            except Exception as e:
                print(f"[MARKET] Erreur reload: {e}")
                _meta["error"] = str(e)
            sleep(interval)

    t = Thread(target=loop, daemon=True, name="market-reload")
    t.start()
    print(f"[MARKET] Thread de rechargement démarré (intervalle: {interval}s)")
