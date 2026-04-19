"""
arbitrage.py — Moteur de détection d'opportunités d'arbitrage inter-systèmes
Implémente :
  - scan par item avec branch & bound
  - scoring ISK/jump et ISK/heure
  - filtres configurables (sécurité, jumps max, profit min)
"""

from __future__ import annotations
import heapq
from dataclasses import dataclass, field
from typing import Iterator

from graph import SolarGraph, Route

# ------------------------------------------------------------------ #
#  Configuration                                                       #
# ------------------------------------------------------------------ #

@dataclass
class ArbitrageConfig:
    min_profit_isk:   float = 2_000_000   # profit net minimum en ISK
    max_jumps:        int   = 15          # filtre géographique grossier
    top_k_offers:     int   = 5           # top-K offres par side par item
    cost_per_jump:    float = 0.0         # coût logistique fixe par jump (fuel, etc.)
    avoid_null:       bool  = False       # évite les systèmes nullsec
    avoid_low:        bool  = False       # évite les systèmes lowsec
    min_volume:       int   = 1           # volume minimum échangeable
    sec_per_jump:     float = 60.0        # secondes par jump (pour ISK/heure)
    broker_fee_pct:   float = 0.03        # 3% frais de courtier à l'achat
    sales_tax_pct:    float = 0.036       # 3.6% taxe de vente


# ------------------------------------------------------------------ #
#  Structures de données                                               #
# ------------------------------------------------------------------ #

@dataclass
class Offer:
    order_id:   int
    system_id:  int
    price:      float
    volume:     int
    range_jumps: int    # portée de l'ordre (0=station, -1=région)
    location_id: int


@dataclass
class Opportunity:
    item_id:      int
    buy_system:   int   # système où on ACHÈTE (sell order)
    sell_system:  int   # système où on VEND   (buy order)
    buy_price:    float
    sell_price:   float
    volume:       int   # unités échangeables
    route:        Route

    gross_profit:  float = field(init=False)
    fees:          float = field(init=False)
    net_profit:    float = field(init=False)
    isk_per_jump:  float = field(init=False)
    isk_per_hour:  float = field(init=False)
    total_investment: float = field(init=False)
    roi_pct:       float = field(init=False)

    def __post_init__(self):
        self._compute_scores()

    def _compute_scores(self):
        self.gross_profit     = (self.sell_price - self.buy_price) * self.volume
        broker_fee            = self.buy_price * self.volume * 0.03
        sales_tax             = self.sell_price * self.volume * 0.036
        self.fees             = broker_fee + sales_tax
        self.net_profit       = self.gross_profit - self.fees - self.route.risk_cost
        self.total_investment = self.buy_price * self.volume
        self.roi_pct          = (self.net_profit / self.total_investment * 100
                                 if self.total_investment > 0 else 0.0)
        jumps = max(self.route.jumps, 1)
        self.isk_per_jump     = self.net_profit / jumps
        transit_hours         = (self.route.jumps * 60.0) / 3600.0
        self.isk_per_hour     = (self.net_profit / transit_hours
                                 if transit_hours > 0 else self.net_profit)

    def is_safe_route(self) -> bool:
        return self.route.is_safe()

    @property
    def jumps(self) -> int:
        return self.route.jumps

    def to_dict(self) -> dict:
        return {
            "item_id":          self.item_id,
            "buy_system":       self.buy_system,
            "sell_system":      self.sell_system,
            "buy_price":        round(self.buy_price, 2),
            "sell_price":       round(self.sell_price, 2),
            "volume":           self.volume,
            "jumps":            self.route.jumps,
            "gross_profit":     round(self.gross_profit, 2),
            "fees":             round(self.fees, 2),
            "net_profit":       round(self.net_profit, 2),
            "isk_per_jump":     round(self.isk_per_jump, 2),
            "isk_per_hour":     round(self.isk_per_hour, 2),
            "roi_pct":          round(self.roi_pct, 2),
            "total_investment": round(self.total_investment, 2),
            "route_path":       self.route.path,
            "sec_profile":      self.route.sec_profile,
            "is_safe":          self.is_safe_route(),
        }


# ------------------------------------------------------------------ #
#  Moteur d'arbitrage                                                  #
# ------------------------------------------------------------------ #

class ArbitrageEngine:
    """
    Détecte les opportunités d'arbitrage dans ITEMS_PRICES.

    Algorithme :
      1. Pour chaque item, extrait top-K sell offers et top-K buy offers
      2. Branch & bound sur les paires (sell_system, buy_system)
      3. Filtre géographique grossier O(1) avant Dijkstra
      4. Calcul du profit net avec frais + risque route
    """

    def __init__(self, graph: SolarGraph, config: ArbitrageConfig | None = None):
        self.graph  = graph
        self.config = config or ArbitrageConfig()

    # ------------------------------------------------------------------ #
    #  Point d'entrée principal                                           #
    # ------------------------------------------------------------------ #

    def scan(
        self,
        market: dict,                    # ITEMS_PRICES indexé [type_id][system_id]
        sort_by: str = "isk_per_hour",   # "net_profit" | "isk_per_jump" | "isk_per_hour" | "roi_pct"
        limit: int = 50,
    ) -> list[Opportunity]:
        """
        Scan complet du marché.
        Retourne les `limit` meilleures opportunités triées par `sort_by`.
        """
        results: list[Opportunity] = []

        # Heap global par gross_spread pour prioriser les items prometteurs
        # Structure : (-gross_spread, type_id)
        item_heap: list[tuple[float, int]] = []

        for type_id, systems in market.items():
            best_sell, best_buy = self._item_spread(systems)
            if best_sell is None or best_buy is None:
                continue
            gross = best_buy.price - best_sell.price
            if gross > 0:
                heapq.heappush(item_heap, (-gross, type_id))

        # Traite les items du plus prometteur au moins prometteur
        while item_heap:
            neg_spread, type_id = heapq.heappop(item_heap)

            # Borne supérieure : gross_spread est en ISK/unité.
            # On ne peut pas pruner ici sans connaître le volume — on laisse
            # _scan_item gérer le pruning fin. On filtre seulement le spread nul.
            if -neg_spread <= 0:
                break

            systems = market[type_id]
            for opp in self._scan_item(type_id, systems):
                results.append(opp)

        results.sort(key=lambda o: getattr(o, sort_by), reverse=True)
        return results[:limit]

    def scan_item(
        self,
        type_id: int,
        market:  dict,
        sort_by: str = "isk_per_hour",
    ) -> list[Opportunity]:
        """Scan ciblé sur un seul item."""
        systems = market.get(type_id, {})
        results = list(self._scan_item(type_id, systems))
        results.sort(key=lambda o: getattr(o, sort_by), reverse=True)
        return results

    # ------------------------------------------------------------------ #
    #  Scan d'un item (branch & bound)                                   #
    # ------------------------------------------------------------------ #

    def _scan_item(self, type_id: int, systems: dict) -> Iterator[Opportunity]:
        """
        Pour un item donné, trouve toutes les paires (A achat, B vente) rentables.

        Complexité : O(K² × Dijkstra) avec K = top_k_offers
        En pratique K=5, donc 25 paires max par item avant pruning.
        """
        cfg = self.config

        # Extrait top-K sell offers (prix croissant) et buy offers (prix décroissant)
        sell_offers = self._top_k_sells(systems, cfg.top_k_offers)
        buy_offers  = self._top_k_buys(systems, cfg.top_k_offers)

        if not sell_offers or not buy_offers:
            return

        for sell in sell_offers:
            # Pruning horizontal :
            # Borne sup du profit brut = meilleur_buy_unit * volume_echangeable_max
            # Si même ça < min_profit → ce sell et tous les suivants inutiles
            best_gross_unit = buy_offers[0].price - sell.price
            max_volume = min(sell.volume, buy_offers[0].volume)
            if best_gross_unit * max_volume <= cfg.min_profit_isk:
                return

            for buy in buy_offers:
                # Pruning vertical :
                # gross * volume ≤ 0 → tous les buy suivants (moins chers) aussi → break
                gross = (buy.price - sell.price) * min(sell.volume, buy.volume)
                if gross <= 0:
                    break

                # Même système → pas d'arbitrage inter-systèmes
                if sell.system_id == buy.system_id:
                    continue

                # Filtre géographique grossier O(1)
                lb = self.graph.jumps_lower_bound(sell.system_id, buy.system_id)
                if lb > cfg.max_jumps:
                    continue

                # Estimation rapide du profit net avec borne inférieure de coût
                optimistic_net = gross - (lb * cfg.cost_per_jump)
                if optimistic_net < cfg.min_profit_isk:
                    continue

                # Calcul de route précis (avec cache LRU)
                route = self.graph.get_route(
                    sell.system_id, buy.system_id,
                    avoid_null=cfg.avoid_null,
                    avoid_low=cfg.avoid_low,
                )
                if route is None or route.jumps > cfg.max_jumps:
                    continue

                # Volume échangeable réel
                volume = min(sell.volume, buy.volume)
                if volume < cfg.min_volume:
                    continue

                opp = Opportunity(
                    item_id=type_id,
                    buy_system=sell.system_id,
                    sell_system=buy.system_id,
                    buy_price=sell.price,
                    sell_price=buy.price,
                    volume=volume,
                    route=route,
                )

                if opp.net_profit >= cfg.min_profit_isk:
                    yield opp

    # ------------------------------------------------------------------ #
    #  Extraction des offres                                              #
    # ------------------------------------------------------------------ #

    def _top_k_sells(self, systems: dict, k: int) -> list[Offer]:
        """
        Top-K meilleures offres de vente (prix le plus bas) tous systèmes confondus.
        Les offres sont déjà triées par prix croissant à l'ingestion.
        """
        all_sells: list[Offer] = []
        for system_id, sides in systems.items():
            for order in sides.get("sell", []):
                all_sells.append(Offer(
                    order_id=order["order_id"],
                    system_id=system_id,
                    price=order["price"],
                    volume=order["volume_remain"],
                    range_jumps=order.get("range_jumps", 0),
                    location_id=order["location_id"],
                ))
        all_sells.sort(key=lambda o: o.price)
        return all_sells[:k]

    def _top_k_buys(self, systems: dict, k: int) -> list[Offer]:
        """
        Top-K meilleures offres d'achat (prix le plus haut) tous systèmes confondus.
        """
        all_buys: list[Offer] = []
        for system_id, sides in systems.items():
            for order in sides.get("buy", []):
                all_buys.append(Offer(
                    order_id=order["order_id"],
                    system_id=system_id,
                    price=order["price"],
                    volume=order["volume_remain"],
                    range_jumps=order.get("range_jumps", 0),
                    location_id=order["location_id"],
                ))
        all_buys.sort(key=lambda o: o.price, reverse=True)
        return all_buys[:k]

    def _item_spread(self, systems: dict) -> tuple[Offer | None, Offer | None]:
        """Retourne la meilleure sell offer et la meilleure buy offer globales."""
        sells = self._top_k_sells(systems, 1)
        buys  = self._top_k_buys(systems, 1)
        return (sells[0] if sells else None, buys[0] if buys else None)
