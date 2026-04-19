"""
graph.py — Graphe de systèmes solaires EVE Online
Fournit :
  - chargement du graphe via ESI (systems + stargates)
  - Dijkstra avec coût pondéré par sécurité
  - cache LRU de routes symétrique
"""

from __future__ import annotations
import heapq
import requests
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock

ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {"Accept": "application/json", "X-Compatibility-Date": "2025-12-16"}

# Coût de traversée d'un jump selon la sécurité de destination
# Exprimé en ISK "équivalent risque" par jump — à calibrer
SEC_RISK_COST: dict[str, float] = {
    "high":  500_000,   # highsec  : sécurisé
    "low":  5_000_000,  # lowsec   : risque modéré
    "null": 20_000_000, # nullsec  : risque élevé
}

def _sec_bucket(security: float) -> str:
    if security >= 0.5:
        return "high"
    if security > 0.0:
        return "low"
    return "null"


@dataclass
class System:
    system_id: int
    name: str
    security: float
    region_id: int
    sec_bucket: str = field(init=False)

    def __post_init__(self):
        self.sec_bucket = _sec_bucket(self.security)


@dataclass
class Route:
    origin:      int
    destination: int
    path:        list[int]          # system_ids dans l'ordre
    jumps:       int
    risk_cost:   float              # coût ISK estimé du risque
    sec_profile: dict[str, int]     # {"high": n, "low": n, "null": n}

    def total_cost(self, cost_per_jump: float = 0.0) -> float:
        return self.risk_cost + self.jumps * cost_per_jump

    def is_safe(self) -> bool:
        return self.sec_profile.get("null", 0) == 0 and self.sec_profile.get("low", 0) == 0


class SolarGraph:
    """
    Graphe des systèmes solaires avec Dijkstra pondéré par risque.
    
    Usage :
        graph = SolarGraph()
        graph.load_from_esi(region_ids=[10000002, 10000043])
        route = graph.get_route(30000142, 30002187)
    """

    def __init__(self, route_cache_size: int = 20_000):
        self._systems:   dict[int, System]       = {}  # system_id → System
        self._adjacency: dict[int, list[int]]    = {}  # system_id → [voisins]
        self._region_of: dict[int, int]          = {}  # system_id → region_id

        self._cache:     OrderedDict[tuple, Route] = OrderedDict()
        self._cache_max  = route_cache_size
        self._cache_lock = Lock()

    # ------------------------------------------------------------------ #
    #  Chargement ESI                                                      #
    # ------------------------------------------------------------------ #

    def load_from_esi(self, region_ids: list[int]) -> None:
        """Charge tous les systèmes + stargates des régions données."""
        print(f"[GRAPH] Chargement de {len(region_ids)} régions...")

        all_system_ids: list[int] = []
        for rid in region_ids:
            try:
                r = requests.get(f"{ESI_BASE}/universe/regions/{rid}/",
                                 headers=ESI_HEADERS, timeout=10)
                r.raise_for_status()
                sids = r.json().get("constellations", [])
                # On passe par les constellations → systèmes
                for cid in sids:
                    rc = requests.get(f"{ESI_BASE}/universe/constellations/{cid}/",
                                      headers=ESI_HEADERS, timeout=10)
                    rc.raise_for_status()
                    for sid in rc.json().get("systems", []):
                        all_system_ids.append(sid)
                        self._region_of[sid] = rid
            except Exception as e:
                print(f"[GRAPH] Erreur région {rid}: {e}")

        print(f"[GRAPH] {len(all_system_ids)} systèmes à charger...")
        self._load_systems(all_system_ids)
        print(f"[GRAPH] Graphe prêt : {len(self._systems)} systèmes, "
              f"{sum(len(v) for v in self._adjacency.values())//2} connexions")

    def _load_systems(self, system_ids: list[int]) -> None:
        for sid in system_ids:
            try:
                r = requests.get(f"{ESI_BASE}/universe/systems/{sid}/",
                                 headers=ESI_HEADERS, timeout=10)
                r.raise_for_status()
                data = r.json()

                self._systems[sid] = System(
                    system_id=sid,
                    name=data.get("name", str(sid)),
                    security=data.get("security_status", 1.0),
                    region_id=self._region_of.get(sid, 0),
                )
                self._adjacency[sid] = []

                for gate_id in data.get("stargates", []):
                    rg = requests.get(f"{ESI_BASE}/universe/stargates/{gate_id}/",
                                      headers=ESI_HEADERS, timeout=10)
                    rg.raise_for_status()
                    dest = rg.json()["destination"]["system_id"]
                    if dest not in self._adjacency[sid]:
                        self._adjacency[sid].append(dest)

            except Exception as e:
                print(f"[GRAPH] Erreur système {sid}: {e}")

    def add_system(self, system: System, neighbors: list[int]) -> None:
        """Injection manuelle (tests / mock)."""
        self._systems[system.system_id] = system
        self._adjacency[system.system_id] = neighbors
        self._region_of[system.system_id] = system.region_id

    # ------------------------------------------------------------------ #
    #  Dijkstra                                                            #
    # ------------------------------------------------------------------ #

    def dijkstra(self, origin: int, destination: int,
                 avoid_null: bool = False,
                 avoid_low:  bool = False) -> Route | None:
        """
        Dijkstra pondéré par risque.
        
        Coût d'un arc = SEC_RISK_COST[sec_bucket(destination)]
        Permet d'éviter lowsec/nullsec via les flags.
        Retourne None si aucun chemin trouvé.
        """
        if origin not in self._systems or destination not in self._systems:
            return None
        if origin == destination:
            return Route(origin, destination, [origin], 0, 0.0, {"high":0,"low":0,"null":0})

        # (coût_cumulé, system_id, chemin)
        heap: list[tuple[float, int, list[int]]] = [(0.0, origin, [origin])]
        visited: set[int] = set()

        while heap:
            cost, current, path = heapq.heappop(heap)

            if current in visited:
                continue
            visited.add(current)

            if current == destination:
                return self._build_route(origin, destination, path, cost)

            for neighbor in self._adjacency.get(current, []):
                if neighbor in visited:
                    continue
                sys_n = self._systems.get(neighbor)
                if sys_n is None:
                    continue

                bucket = sys_n.sec_bucket
                if avoid_null and bucket == "null":
                    continue
                if avoid_low and bucket == "low":
                    continue

                jump_cost = SEC_RISK_COST[bucket]
                heapq.heappush(heap, (cost + jump_cost, neighbor, path + [neighbor]))

        return None  # pas de chemin

    def _build_route(self, origin: int, destination: int,
                     path: list[int], total_cost: float) -> Route:
        profile: dict[str, int] = {"high": 0, "low": 0, "null": 0}
        for sid in path[1:]:  # on ne compte pas l'origine
            sys = self._systems.get(sid)
            if sys:
                profile[sys.sec_bucket] += 1

        return Route(
            origin=origin,
            destination=destination,
            path=path,
            jumps=len(path) - 1,
            risk_cost=total_cost,
            sec_profile=profile,
        )

    # ------------------------------------------------------------------ #
    #  Cache de routes                                                     #
    # ------------------------------------------------------------------ #

    def get_route(self, origin: int, destination: int,
                  avoid_null: bool = False,
                  avoid_low:  bool = False) -> Route | None:
        """Route avec cache LRU symétrique."""
        key = (min(origin, destination), max(origin, destination), avoid_null, avoid_low)

        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

        route = self.dijkstra(origin, destination, avoid_null, avoid_low)

        with self._cache_lock:
            if len(self._cache) >= self._cache_max:
                self._cache.popitem(last=False)
            if route is not None:
                self._cache[key] = route

        return route

    def jumps_lower_bound(self, a: int, b: int) -> int:
        """
        Borne inférieure O(1) du nombre de jumps.
        Même région → au moins 1 jump.
        Régions différentes → au moins 2 jumps (pessimiste mais rapide).
        Utilisé pour pruning avant Dijkstra.
        """
        if a == b:
            return 0
        if self._region_of.get(a) == self._region_of.get(b):
            return 1
        return 2

    # ------------------------------------------------------------------ #
    #  Sérialisation / Désérialisation                                    #
    # ------------------------------------------------------------------ #

    def save(self, path: str) -> None:
        """
        Sauvegarde le graphe en JSON.
        Format compact : évite de refaire ~1h d'appels ESI à chaque redémarrage.
        """
        import json
        data = {
            "systems": {
                str(sid): {
                    "name":      s.name,
                    "security":  s.security,
                    "region_id": s.region_id,
                }
                for sid, s in self._systems.items()
            },
            "adjacency": {
                str(sid): neighbors
                for sid, neighbors in self._adjacency.items()
            },
        }
        with open(path, "w") as f:
            json.dump(data, f, separators=(",", ":"))
        size_mb = __import__("os").path.getsize(path) / 1024 / 1024
        print(f"[GRAPH] Sauvegardé dans {path} ({size_mb:.1f} MB, "
              f"{len(self._systems)} systèmes)")

    @classmethod
    def load(cls, path: str, route_cache_size: int = 20_000) -> "SolarGraph":
        """
        Charge le graphe depuis un fichier JSON pré-calculé.
        Typiquement < 1s au lieu de ~1h via ESI.
        """
        import json
        graph = cls(route_cache_size=route_cache_size)
        with open(path) as f:
            data = json.load(f)

        for sid_str, s in data["systems"].items():
            sid = int(sid_str)
            graph._systems[sid] = System(
                system_id=sid,
                name=s["name"],
                security=s["security"],
                region_id=s["region_id"],
            )
            graph._region_of[sid] = s["region_id"]

        for sid_str, neighbors in data["adjacency"].items():
            graph._adjacency[int(sid_str)] = neighbors

        print(f"[GRAPH] Chargé depuis {path} : "
              f"{len(graph._systems)} systèmes, "
              f"{sum(len(v) for v in graph._adjacency.values())//2} connexions")
        return graph

    @classmethod
    def load_or_fetch(cls, path: str, region_ids: list[int],
                      route_cache_size: int = 20_000) -> "SolarGraph":
        """
        Charge depuis le fichier si existant, sinon fetch ESI + sauvegarde.
        C'est le point d'entrée recommandé en production.

        Exemple :
            graph = SolarGraph.load_or_fetch(
                path="graph_cache.json",
                region_ids=[r["id"] for r in REGIONS]
            )
        """
        import os
        if os.path.exists(path):
            print(f"[GRAPH] Cache trouvé : {path}")
            return cls.load(path, route_cache_size)

        print(f"[GRAPH] Pas de cache, fetch ESI (peut prendre plusieurs minutes)...")
        graph = cls(route_cache_size=route_cache_size)
        graph.load_from_esi(region_ids)
        graph.save(path)
        return graph

    # ------------------------------------------------------------------ #
    #  Accesseurs                                                          #
    # ------------------------------------------------------------------ #

    def get_system(self, system_id: int) -> System | None:
        return self._systems.get(system_id)

    def has_system(self, system_id: int) -> bool:
        return system_id in self._systems

    def system_count(self) -> int:
        return len(self._systems)

    def cache_stats(self) -> dict:
        with self._cache_lock:
            return {"size": len(self._cache), "max": self._cache_max}
