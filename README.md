# EVE Arbitrage Scanner

Outil local de détection d'opportunités d'arbitrage inter-systèmes pour EVE Online.  
Interface web Flask avec formulaire de paramètres et affichage détaillé des résultats.

---

## Structure

```
files/
├── app.py              # Application Flask + interface web (point d'entrée)
├── arbitrage.py        # Moteur de scan (branch & bound, scoring)
├── graph.py            # Graphe de systèmes solaires (Dijkstra, cache LRU)
├── market.py           # Ingestion des ordres de marché via ESI
├── requirements.txt    # Dépendances Python
├── graph_cache.json    # Généré au premier lancement (cache du graphe)
└── names_cache.json    # Généré automatiquement (cache des noms ESI)
```

---

## Installation

```bash
pip install -r requirements.txt
```

Dépendances : `flask`, `requests`.

---

## Lancement

```bash
python app.py
```

Puis ouvrir **http://localhost:5000** dans le navigateur.

---

## Ce qui se passe au démarrage

Le premier lancement prend environ **8–10 minutes** le temps de charger les données.  
Deux chargements se font en parallèle en arrière-plan :

| Tâche | Durée | Description |
|---|---|---|
| Marché ESI | ~3 min | ~1.3M ordres sur 18 régions, paginés |
| Graphe ESI | ~8 min | 1480 systèmes + stargates via ESI |

La barre de statut en haut de l'interface indique l'avancement en temps réel.  
Le scan devient disponible dès que les deux sont chargés.

**Dès le deuxième lancement**, le graphe se charge depuis `graph_cache.json` en moins d'une seconde. Le marché se recharge toujours depuis l'ESI (données live, cache de 5 min).

---

## Paramètres du formulaire

| Paramètre | Défaut | Description |
|---|---|---|
| Profit minimum | 2 000 000 ISK | Profit net minimum pour qu'une opportunité soit retenue |
| Jumps maximum | 15 | Nombre de sauts max entre le système d'achat et de vente |
| Top-K offres | 5 | Nombre d'offres considérées par item par côté (achat/vente) |
| Coût logistique/jump | 0 ISK | Frais fixes par saut (fuel, Jump Freighter, etc.) |
| Frais broker | 3% | Prélevés à l'achat sur les sell orders |
| Taxe de vente | 3.6% | Prélevée à la vente sur les buy orders |
| Volume minimum | 1 | Volume échangeable minimum |
| Éviter Nullsec | ✓ | Exclut les routes passant par des systèmes nullsec |
| Éviter Lowsec | ✗ | Exclut les routes passant par des systèmes lowsec |
| Trier par | ISK/heure | Critère de tri des résultats |
| Limite | 30 | Nombre maximum de résultats affichés |

---

## Modèle de profit

```
profit_brut  = (prix_buy_order_B − prix_sell_order_A) × volume

frais        = prix_achat × volume × broker_fee
             + prix_vente × volume × sales_tax

coût_route   = Σ SEC_RISK_COST[sécurité_système] pour chaque jump
             + jumps × cost_per_jump

profit_net   = profit_brut − frais − coût_route
```

Coûts de risque par sécurité de système (calibrables dans `graph.py`) :

| Sécurité | Coût par jump |
|---|---|
| Highsec (≥ 0.5) | 500 000 ISK |
| Lowsec (0.0 – 0.5) | 5 000 000 ISK |
| Nullsec (≤ 0.0) | 20 000 000 ISK |

Ces valeurs représentent le risque de perte de cargo et sont à ajuster selon ton ship.

---

## Scores affichés

| Score | Formule | Utilité |
|---|---|---|
| ISK/heure | `profit_net / (jumps × 60s) × 3600` | Meilleur critère global |
| ISK/jump | `profit_net / jumps` | Bon pour les trades récurrents |
| Profit net | ISK net après frais et risque | Valeur absolue du gain |
| ROI % | `profit_net / investissement × 100` | Efficacité du capital |

---

## Algorithme (résumé)

L'engine utilise du **branch & bound** pour éviter de comparer tous les couples de systèmes :

1. Construction d'un heap global des items triés par spread brut décroissant
2. Pour chaque item, extraction des top-K sell offers et top-K buy offers
3. Pruning horizontal : si `meilleur_buy × volume_max ≤ min_profit` → stop complet sur cet item
4. Pruning vertical : si `gross × volume ≤ 0` → break sur la boucle des buy orders
5. Filtre géographique O(1) via appartenance de région avant de lancer Dijkstra
6. Dijkstra pondéré par le risque de sécurité, avec cache LRU symétrique (20 000 routes)

Complexité effective : **O(I × K²)** avec I = items à spread positif, K = top_k_offers.  
En pratique K=5 → 25 paires max par item avant pruning.

---

## Données ESI utilisées

| Endpoint | Usage |
|---|---|
| `GET /markets/{region_id}/orders/` | Ordres de marché (paginés, toutes régions) |
| `GET /universe/systems/{system_id}/` | Infos système + stargates (chargement graphe) |
| `GET /universe/stargates/{gate_id}/` | Destination des stargates (construction du graphe) |
| `POST /universe/names/` | Résolution batch d'IDs en noms (items, systèmes) |

Intervalle de refresh marché : **300s** (= TTL cache ESI).

---

## Régions couvertes

Amarr Empire, Caldari State, Gallente Federation, Minmatar Republic + zones frontalières lowsec :

Domain · Kador · Kor-Azor · Tash-Murkon · Aridia · Devoid · The Forge · Lonetrek · The Citadel · Black Rise · Sinq Laison · Essence · Verge Vendor · Placid · Heimatar · Metropolis · Molden Heath · The Bleak Lands

Pour ajouter des régions nullsec, ajouter leurs IDs dans la liste `REGIONS` dans `market.py` (et désactiver "Éviter Nullsec" dans le formulaire).

---

## Limitations connues

- Le graphe ne couvre que les 18 régions listées — une route qui passe par une région non chargée sera inconnue de Dijkstra et l'opportunité ignorée.
- Les ordres avec `range: region` (achetables depuis n'importe quel système de la région) ne sont pas encore filtrés par portée réelle — ils sont traités comme des ordres station.
- `SEC_RISK_COST` dans `graph.py` est arbitraire, à calibrer selon la valeur de ton cargo et ton ship.
- Le scan peut prendre 10–30s sur 18 régions avec top_k=5 selon le nombre d'items à spread positif.
