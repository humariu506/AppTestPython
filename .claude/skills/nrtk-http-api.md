---
name: nrtk-http-api
description: Charge le contexte des deux serveurs HTTP optionnels du projet NRTK (`http_server.py` stdlib avec dashboard + `api_server.py` FastAPI REST/WebSocket). À invoquer dès qu'on touche à `files/http_server.py`, `files/api_server.py`, à leurs sections de config (`http:`, `api:`) ou qu'on diagnostique un problème d'accès au dashboard, à l'API REST ou aux WebSocket de l'application. Contient l'inventaire des endpoints, le pattern de cohabitation des deux serveurs, les pièges connus (dépendances optionnelles, diagnostic IDE faussement positif), et les points d'extension.
---

# Serveurs HTTP du projet NRTK — savoir-faire interne

Deux serveurs **optionnels** exposent l'état du moteur VRS à l'extérieur. Ils sont conçus pour cohabiter sans s'interférer, sur des ports distincts, en lecture seule sur le même état partagé (`vrs_engine.last_result` + `NrtkApp._collect_bases_status()`).

## Choix de conception

| Critère | `http_server.py` (Option A) | `api_server.py` (Option C) |
|---|---|---|
| Dépendances | **Aucune** (stdlib `http.server`) | `fastapi` + `uvicorn` (extra `api`) |
| Port par défaut | 8080 | 8081 |
| Cible | Humain (dashboard navigateur) | Programmatique (app Android) |
| Polling | JS dans la page (`setInterval`) | REST côté client OU WebSocket push |
| Documentation | aucune | OpenAPI auto sur `/docs` |
| CORS | `Access-Control-Allow-Origin: *` en dur | configurable via `api.cors` |

La règle implicite : tout *humain* passe par 8080, tout *client logiciel externe* (notamment Android) passe par 8081. Le dashboard 8080 doit rester self-contained — pas d'asset externe, pas de framework JS, juste du fetch + DOM.

## Activation

`config.yaml` :

```yaml
http:
  enabled: true
  host: "0.0.0.0"         # 127.0.0.1 pour ne pas exposer sur le réseau
  port: 8080
  refresh_ms: 500         # cadence du polling navigateur

api:
  enabled: true
  host: "0.0.0.0"
  port: 8081
  cors: true              # désactiver pour un déploiement strict
```

Les deux sont **désactivés par défaut** (`enabled: false`) pour ne pas surprendre l'utilisateur qui veut juste lancer l'UI tkinter.

## Installation des dépendances Option C

`fastapi` et `uvicorn` sont en `[project.optional-dependencies]` (groupe `api`) dans `pyproject.toml`. Installation :

```bash
uv sync --extra api          # ou : pip install fastapi uvicorn
```

Sans ces dépendances, **`api.enabled: true` n'empêche pas l'application de démarrer** : `_start_http_servers()` attrape l'`ImportError` levée par `NrtkApiServer.__init__`, logue un warning, et continue avec un serveur API à `None`. C'est délibéré : on évite de bloquer l'utilisateur si les extras n'ont pas été synchronisés.

Symptôme côté navigateur quand on oublie l'install : « 0 B transferred », icône rouge, `ECONNREFUSED` sur le port 8081 — alors que l'application tkinter elle-même tourne sans erreur. Vérifier les logs : `WARNING — API server non démarré : fastapi et uvicorn sont requis…`.

## Catalogue des endpoints

### `http_server.py` (port 8080)

| Méthode | Chemin | Réponse |
|---|---|---|
| GET | `/` | Dashboard HTML embarqué (auto-rafraîchi en JS) |
| GET | `/api/position` | `{position: PositionResult \| null, timestamp: float}` |
| GET | `/api/bases` | `{bases: list[BaseStatus], timestamp: float}` |
| GET | `/api/status` | `{position, bases, timestamp}` (combiné, utilisé par le dashboard) |

### `api_server.py` (port 8081)

| Méthode | Chemin | Réponse |
|---|---|---|
| GET | `/api/health` | `{ok: true, uptime: float}` — ping |
| GET | `/api/position` | identique à 8080 |
| GET | `/api/bases` | identique à 8080 |
| GET | `/api/status` | identique à 8080 |
| GET | `/docs` | Doc OpenAPI auto-générée par FastAPI |
| WS | `/ws/position` | Push JSON identique au payload `/api/status` à chaque résultat VRS publié |

Les payloads JSON utilisent la même fonction `_result_to_dict()` (définie dans chaque module). Le champ `vrs_rtcm: bytes` du `PositionResult` est **systématiquement retiré** avant sérialisation — inutile côté client et non-JSON-sérialisable.

## Mécanisme de push WebSocket

`api_server.NrtkApiServer.publish_result(result)` est appelée **depuis le thread sync du moteur VRS** dans `NrtkApp._on_position_result` ([main.py](files/main.py)). Elle :

1. Vérifie que l'event loop asyncio du serveur est prêt (`self._loop_ready` est un `threading.Event`).
2. Court-circuite si la liste `_ws_clients` est vide (évite l'allocation du payload pour rien).
3. Schedule `_broadcast(payload)` via `asyncio.run_coroutine_threadsafe` — c'est **la** primitive correcte pour passer du sync threadé à de l'asyncio.
4. `_broadcast` itère sur les clients, purge ceux qui lèvent une exception (déconnexion silencieuse).

`http_server.py` n'a pas de mécanisme de push : le dashboard fait du polling JS toutes les `refresh_ms` ms. C'est suffisant pour un humain qui regarde la page.

## Cohabitation et état partagé

Les deux serveurs n'ont **aucune connaissance l'un de l'autre**. Ils reçoivent en injection :

- `vrs_engine` (pour accéder à la propriété `last_result`)
- `bases_status_provider: Callable[[], list[dict]]` (callback qui retourne la liste à jour)

Le callback en question, `NrtkApp._collect_bases_status()` ([main.py](files/main.py)), unifie les deux modes :
- En mode mock, parcourt `self._mock_bases` (objets `MockNtripBase`).
- En mode réel, parcourt `self._ntrip_clients` (objets `NtripClient`).
- Retourne dans les deux cas la même structure de dict avec : `id`, `mountpoint`, `host`, `connected`, `msg_count`, `last_msg_age`, `last_error`.

Ajouter un champ implique de l'ajouter dans le dict ici, dans le rendu HTML côté `http_server.py`, et de mettre à jour la doc OpenAPI de `api_server.py` (FastAPI le fait quasi automatiquement si on type le retour).

## Intégration dans `main.py`

Trois points de couture :

1. `__init__` initialise `self._http_server = None` et `self._api_server = None`.
2. `start()` appelle `_start_http_servers()` après le démarrage du moteur VRS. Cette méthode lit `config.yaml`, instancie les serveurs et logue dans l'UI tkinter le chemin d'accès.
3. `_on_position_result()` appelle `self._api_server.publish_result(result)` si l'API est active (no-op silencieux sinon — la méthode gère elle-même le cas où le loop n'est pas prêt).
4. `stop()` ferme les deux serveurs (best-effort, swallow des exceptions pour ne pas bloquer le reste de l'arrêt).

## Pièges connus

### Faux positif IDE — `Cannot find module 'http_server'`

Les imports `from http_server import NrtkHttpServer` et `from api_server import NrtkApiServer` sont **dynamiques** (à l'intérieur de `_start_http_servers()`) et utilisent la convention flat du projet — tous les modules de `files/` sont importés à plat parce que `main.py` est lancé depuis `files/`. L'analyseur statique de l'IDE marque ces lignes en erreur, mais c'est cohérent avec les autres imports (`from mock_generator import ...`, etc.) et fonctionne à l'exécution. **Ne pas convertir en imports relatifs**, ça casserait le pattern existant.

### Le serveur API tourne dans son propre event loop

Ne pas tenter d'appeler du code FastAPI/asyncio depuis le thread principal — toujours passer par `publish_result()` ou par les endpoints standards. Si on veut ajouter une nouvelle source de broadcast (par exemple un signal de coupure NTRIP), wrapper l'appel avec `asyncio.run_coroutine_threadsafe(coro, self._loop)`.

### `lifespan="off"` dans uvicorn.Config

Volontaire. Évite que `uvicorn` cherche des handlers `startup`/`shutdown` qui pourraient mal cohabiter avec notre arrêt manuel via `should_exit = True`. Si un jour on veut un init asynchrone (cache Redis, pool DB…), il faudra passer à `lifespan="on"` et écrire un context manager FastAPI propre.

### `vrs_rtcm` retiré du JSON

Si à l'avenir on veut exposer le flux RTCM synthétisé au client (par exemple pour qu'un client Android le forward à un UM980 connecté via OTG), il faudrait l'encoder en hex ou base64 dans le dict — c'est strictement opt-in et il faudra réfléchir à la sécurité.

### CORS

`http_server.py` envoie `Access-Control-Allow-Origin: *` en dur sur les endpoints API (pas sur le HTML). C'est intentionnel pour qu'un client dev puisse tester depuis n'importe quelle origine, mais à durcir si l'application sort du périmètre interne. `api_server.py` permet de désactiver le middleware CORS via `config.yaml: api.cors: false`.

### Polling vs WebSocket pour Android

Le client Android peut très bien faire du polling sur `/api/status` toutes les 500 ms — c'est plus simple à coder côté Kotlin, et la charge est négligeable sur du localhost ou un LAN. La WebSocket `/ws/position` est l'option « propre » pour le temps réel : moins de latence, moins d'overhead HTTP, mais demande de gérer le cycle de vie (reconnect, ping/pong, veille terminal). Choisir selon la maturité de l'équipe Android.

## Points d'extension naturels

- **Authentification Basic ou Bearer** : `api_server.py` peut accueillir un `Depends(verify_token)` FastAPI sur chaque endpoint, en lisant un token depuis `config.yaml`. À envisager dès qu'on quitte le LAN.
- **POST de configuration** : un `POST /api/config/bases` permettrait à l'app Android d'envoyer la liste des balises à utiliser — ce qui rejoint la perspective d'amélioration n°2 du rapport (sélection automatique des balises proches).
- **Streaming RTCM brut** : exposer le `vrs_rtcm` du dernier résultat sur un endpoint `GET /api/vrs/rtcm` (binaire, content-type `application/octet-stream`) pour que des clients tiers puissent récupérer le flux corrigé.
- **Server-Sent Events** : si la WebSocket pose souci sur certains réseaux, un endpoint SSE sur `/api/stream` est trivial à ajouter — c'est juste un `text/event-stream` long-lived côté `api_server.py`.

## Quand intervenir avec prudence

- Modifier la structure du dict retourné par `_collect_bases_status()` : impact sur les deux serveurs, sur le rendu HTML du dashboard, et potentiellement sur l'app Android.
- Toucher au `_DASHBOARD_HTML` : c'est une chaîne triple-quote dans `http_server.py`. Ne pas y mettre de `{` ou `}` non échappés si on passe à du `str.format` un jour (actuellement c'est un simple `replace("__REFRESH__", ...)`).
- Activer `api.enabled: true` sans avoir fait `uv sync --extra api` : symptôme « 0 B transferred » dans la console réseau du navigateur, et un warning dans les logs. Toujours vérifier `files/logs/nrtk_log_*.log` au lancement.