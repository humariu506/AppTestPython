# AppTestPython — Programme de test NRTK / réseau Centipede

Application Python de test pour le positionnement **NRTK** (Network RTK) à
précision centimétrique. Elle se connecte simultanément à **cinq balises**
du réseau [Centipede](https://centipede.fr/) via NTRIP, calcule une **station
de référence virtuelle (VRS)** à la position du rover par interpolation des
corrections, puis pousse le flux RTCM3 résultant vers un récepteur GNSS
(typiquement un **Unicore UM980**) — ou simule l'ensemble de la chaîne en
mode *mock* pour développer sans matériel.

## Sommaire

- [Aperçu fonctionnel](#aperçu-fonctionnel)
- [Architecture](#architecture)
- [Structure du dépôt](#structure-du-dépôt)
- [Installation](#installation)
- [Configuration](#configuration)
- [Utilisation](#utilisation)
- [Modes de fonctionnement](#modes-de-fonctionnement)
- [Modes VRS](#modes-vrs)
- [Dashboard HTTP et API mobile](#dashboard-http-et-api-mobile)
- [Altitude et géoïde](#altitude-et-géoïde)
- [Logs](#logs)

## Aperçu fonctionnel

Le programme implémente la méthode **NRTK / VRS** :

1. Cinq clients NTRIP reçoivent en parallèle les flux RTCM3 de cinq balises
   Centipede proches du rover.
2. Le décodeur RTCM extrait, pour chaque base, sa position ECEF (messages
   1005/1006) et les observables L1/L2 par satellite (1001–1004 / 1009–1012).
3. Le moteur **VRS** interpole les corrections différentielles à la position
   approximative du rover (fournie par les trames NMEA GGA du récepteur)
   via une pondération inverse au carré de la distance (**IDW**).
4. Le résultat est ré-encapsulé en RTCM3 (messages 1004 + 1005) comme s'il
   provenait d'une *base virtuelle placée exactement au rover* — la
   *baseline* devient théoriquement nulle, ce qui permet une résolution
   d'ambiguïté de phase quasi-instantanée.
5. Ce flux est transmis au récepteur GNSS via le port série (ou passé à
   **RTKLIB** pour résolution offline). Une altitude orthométrique correcte
   est obtenue en appliquant l'ondulation du géoïde **RAF20** (`H = h − N`).

Interface graphique **tkinter** temps réel : position, statut de fix
(NONE / SINGLE / FLOAT / FIX), précision σH / σV, statut des cinq bases,
journal d'événements. Un mode `--no-ui` (headless) est également disponible.

## Architecture

```
                ┌──────────────┐      ┌─────────────────────┐
                │   UM980      │◀────▶│   SerialManager     │  (read/write thread-safe)
                │  (réel) ou   │      │   serial_manager.py │
                │  MockSensor  │      └──────────┬──────────┘
                └──────────────┘                 │ NMEA GGA
                                                 ▼
   ┌─────────────┐    RTCM3      ┌──────────────────────────┐
   │ 5 × Caster  │──────────────▶│   NtripClient × 5        │
   │  Centipede  │   (TCP)       │   ntrip_client.py        │
   │  (NTRIP v2) │◀──────────────│   (envoi GGA périodique) │
   └─────────────┘    GGA        └─────────────┬────────────┘
                                               │ trames RTCM3
                                               ▼
                                   ┌──────────────────────┐
                                   │  RtcmDecoder         │
                                   │  rtcm_decoder.py     │──▶ ObservationStore
                                   └──────────────────────┘    (thread-safe)
                                                                       │
                                                                       ▼
                                                       ┌────────────────────────┐
                                                       │   VrsEngine (1 Hz)     │
                                                       │   vrs_engine.py        │
                                                       │  ┌──────────────────┐  │
                                                       │  │  Interpolation   │  │
                                                       │  │  IDW             │  │
                                                       │  ├──────────────────┤  │
                                                       │  │  Synthèse RTCM   │  │
                                                       │  │  1004 + 1005     │  │
                                                       │  ├──────────────────┤  │
                                                       │  │  RTKLIB ou WLS   │  │
                                                       │  └──────────────────┘  │
                                                       └────────────┬───────────┘
                                                                    │ PositionResult + RTCM VRS
                                                                    ▼
                                                       ┌────────────────────────┐
                                                       │   UI tkinter (ui.py)   │
                                                       │   + SerialManager.write│
                                                       └────────────────────────┘
```

Le découpage est strictement **multithread** :

| Thread                  | Rôle                                              |
|-------------------------|---------------------------------------------------|
| `serial-read`           | Lit le flux NMEA du récepteur                     |
| `serial-write`          | Pousse les corrections RTCM3 (file d'attente)     |
| `ntrip-<base_id>` × 5   | Reçoit le RTCM3 de chaque base Centipede          |
| `vrs-engine`            | Recalcule la VRS à 1 Hz                           |
| Thread principal        | Boucle `mainloop()` tkinter (ou idle headless)    |

Aucun thread ne bloque les autres : la transmission RTCM continue même
si le calcul VRS s'attarde.

## Structure du dépôt

```
AppTestPython/
├── files/
│   ├── main.py             ← point d'entrée, orchestration
│   ├── config.yaml         ← configuration (bases, NTRIP, capteur, VRS…)
│   ├── serial_manager.py   ← accès thread-safe au port série du UM980
│   ├── ntrip_client.py     ← client NTRIP v1/v2 (1 instance par base)
│   ├── rtcm_decoder.py     ← parsing RTCM3 + ObservationStore
│   ├── vrs_engine.py       ← interpolation IDW, synthèse RTCM, solveur
│   ├── mock_generator.py   ← capteur + bases NTRIP simulés
│   ├── geoid.py            ← chargeur RAF20 (.gtx) et interpolation N
│   ├── ui.py               ← interface tkinter
│   ├── http_server.py      ← dashboard web stdlib + endpoints JSON (port 8080)
│   ├── api_server.py       ← API REST + WebSocket FastAPI (port 8081, opt.)
│   ├── RAF20.gtx           ← grille géoïde IGN (France métropolitaine)
│   └── logs/               ← journaux horodatés générés à l'exécution
├── RTKLIB-master/          ← solveur RTK externe (optionnel, cm-level)
├── pyproject.toml          ← dépendances (numpy, pyserial, pyyaml ; pyrtcm déconseillé)
└── Feuille de route.txt    ← étapes du stage
```

## Installation

Le projet utilise [uv](https://github.com/astral-sh/uv) pour la gestion
de l'environnement (un `uv.lock` est versionné) et cible **Python ≥ 3.14**.

```bash
# Cloner et entrer dans le dossier
uv sync                       # crée .venv/ et installe les dépendances de base
uv sync --extra api           # ajoute fastapi + uvicorn pour le serveur REST/WS
```

La deuxième commande n'est nécessaire que si vous comptez activer le
serveur API mobile (`api.enabled: true` dans `config.yaml`). Le dashboard
HTTP stdlib (`http.enabled: true`) n'a besoin d'aucune dépendance
supplémentaire.

Sans `uv`, un classique `pip install -r` à partir de `pyproject.toml`
fonctionne aussi :

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install numpy pyserial pyyaml
pip install fastapi uvicorn   # uniquement si on active le serveur API
```

> /!\ **N'installez pas `pyrtcm` pour l'instant.** Lorsque le module est
> présent, le décodeur l'utilise en priorité et l'application n'arrive plus
> à établir une position fixe RTK. Tant que le problème n'est pas identifié,
> laissez `pyrtcm` désinstallé : le décodeur RTCM interne prend alors le relais
> et le logiciel fonctionne correctement.

**Dépendances**

| Paquet     | Rôle                                          | Obligatoire ? |
|------------|-----------------------------------------------|---------------|
| `pyyaml`   | Lecture de `config.yaml`                      | Oui           |
| `numpy`    | Calcul VRS (algèbre linéaire, WLS)            | Oui           |
| `pyserial` | Accès au port série du récepteur réel         | Mode réel     |
| `fastapi`  | Serveur REST + WebSocket pour intégration Android | Extra `api` |
| `uvicorn`  | Serveur ASGI utilisé par FastAPI              | Extra `api`   |
| `pyrtcm`   | /!\ À NE PAS installer pour l'instant (voir ci-dessus) | Déconseillé   |

**RTKLIB** (optionnel) : pour la précision centimétrique avec résolution
d'ambiguïté entière, compiler `rtkpost`/`rtkrcv` depuis `RTKLIB-master/`
et renseigner le chemin dans `config.yaml`. À défaut, un solveur WLS Python
interne prend le relais (précision décimétrique).

## Configuration

Tout se règle dans [files/config.yaml](files/config.yaml). Sections clés :

- **`sensor`** : port COM, baudrate, hauteur d'antenne (soustraite de
  l'altitude), basculement mock / réel.
- **`bases`** : liste des cinq balises Centipede (id, host, port,
  mountpoint, lat/lon/alt approximatives). À adapter à votre zone : un
  réseau de bases proches du rover donne les meilleures corrections.
- **`ntrip`** : identifiants du caster, version NTRIP (2 recommandé),
  possibilité de simuler les bases (`mock: true`) tout en gardant un
  capteur réel.
- **`rtklib`** : chemins vers `rtkrcv`/`rtkpost`, paramètres de résolution
  (élévation, navsys, AR ratio…).
- **`vrs.enabled`** / **`vrs.pure_mode`** : sélectionnent le mode pont
  direct, hybride ou VRS pure (voir la section [Modes VRS](#modes-vrs)
  ci-dessous). `vrs.station_id` est la valeur DF003 inscrite dans les
  trames synthétisées.
- **`mock`** : paramètres de simulation (position rover, bruit, cadence
  NMEA/RTCM, probabilité de coupure).
- **`ui`** : cadence de rafraîchissement, nombre de décimales affichées.

## Utilisation

Depuis la racine du projet :

```bash
python files/main.py                  # mode défaut (lit config.yaml)
python files/main.py --real           # force le capteur USB réel
python files/main.py --no-ui          # mode console (serveur headless)
python files/main.py --config mon.yaml
python files/main.py --log-level DEBUG
```

Arrêt propre : `Ctrl + C` ou fermeture de la fenêtre tkinter — tous les
threads (NTRIP, série, VRS) sont stoppés et le port série refermé.

## Modes de fonctionnement

Le mode effectif découle de la combinaison `sensor.mock` × `ntrip.mock`
(et de l'éventuel flag `--real`) :

| `sensor.mock` | `ntrip.mock` | Mode                | Cas d'usage                                 |
|:-------------:|:------------:|---------------------|---------------------------------------------|
| `true`        | `true`       | **MOCK COMPLET**    | Développement, tests, démos sans matériel   |
| `true`        | `false`      | **HYBRIDE**         | Validation NTRIP réel sans récepteur        |
| `false`       | `true`       | **HYBRIDE**         | Récepteur réel, données simulées (debug)    |
| `false`       | `false`      | **RÉEL COMPLET**    | Production                                  |

En mock complet, l'application génère elle-même des trames NMEA et RTCM3
cohérentes (5 bases simulées, satellites visibles communs, erreurs
différentielles troposphère/ionosphère injectées), ce qui permet de
valider l'ensemble du pipeline VRS sans matériel ni connexion réseau.

## Modes VRS

Trois modes de fonctionnement sont sélectionnables via la section
`vrs` du `config.yaml`. Tous garantissent que le UM980 reçoit un flux
RTCM3 conforme et peut atteindre un FIX RTK ; ils se distinguent par la
*provenance* de ce flux.

| `enabled` | `pure_mode` | Nom court        | Flux RTCM envoyé au UM980                                                | Moteur VRS                |
|:---------:|:-----------:|------------------|--------------------------------------------------------------------------|---------------------------|
| `false`   | *           | **Pont direct**  | flux brut intégral de `bases[0]`                                         | éteint                    |
| `true`    | `false`     | **Hybride** *(défaut)* | flux brut intégral de `bases[0]`                                   | actif, télémétrie seule   |
| `true`    | `true`      | **VRS pure**     | éphémérides du flux brut + 1005 et 1002 synthétisés à la position du rover | actif, RTCM poussé        |

**Pont direct** est le plus simple : aucune calcul, le UM980 fait du RTK
mono-base classique avec `bases[0]` comme référence. Baseline = distance
réelle au caster.

**Hybride** est le mode par défaut. Le UM980 reçoit toujours le flux brut
de `bases[0]` (donc le FIX RTK est garanti, comme en pont direct), mais
le moteur VRS tourne en parallèle pour fournir une position interpolée
multi-balises exposée dans l'UI tkinter, le dashboard HTTP et l'API
REST/WS. Le RTCM synthétisé **n'est pas** envoyé au récepteur.

**VRS pure** est expérimental. Le récepteur reçoit :

- depuis le flux brut, uniquement les éphémérides (messages 1019, 1020,
  1042, 1044, 1045, 1046) — indispensables pour positionner les satellites ;
- depuis le moteur VRS, un **RTCM 1005 conforme** (152 bits, ECEF signé)
  annonçant une base virtuelle exactement à la position du rover, et un
  **RTCM 1002 conforme** (Extended L1 only GPS, 74 bits/satellite, lock
  time DF013 persistant) avec les pseudoranges et phases interpolées
  depuis plusieurs balises.

L'objectif de la VRS pure est de présenter au UM980 une baseline théorique
de 0 m, ce qui accélère la résolution des ambiguïtés de phase entières et
réduit l'erreur résiduelle d'atmosphère. La précision en altitude peut être
meilleure si les balises sont bien réparties autour du rover. Le délai
d'obtention du FIX est généralement plus long (30 s à 1 min) parce que
le lock time DF013 repart de 0 à chaque démarrage.

```yaml
vrs:
  enabled: true        # true = moteur VRS actif (hybride ou pure)
  pure_mode: false     # true = pousse 1005+1002 au UM980 (VRS pure)
  station_id: 4042     # DF003 inscrit dans les trames synthétisées
```

**Limites actuelles de la VRS pure** : pas de GLONASS (message 1012 non
synthétisé), TOW courant utilisé tel quel sans alignement entre balises,
CNR codé en dur à ≈ 45 dB-Hz. La feuille de route détaillée et les
pointeurs d'implémentation sont dans
[`.claude/skills/nrtk-vrs-hybrid.md`](.claude/skills/nrtk-vrs-hybrid.md).

> Bonne pratique : démarrer en hybride pour valider la chaîne, puis
> basculer en pure pour comparer la précision. Si le UM980 reste en
> SinglePoint en pure, repasser en hybride en un changement de config
> (le `pure_mode: false` suffit, pas besoin de redémarrer la session
> NTRIP).

## Dashboard HTTP et API mobile

L'état du moteur VRS peut être exposé hors de l'UI tkinter via deux serveurs
optionnels qui peuvent cohabiter sur des ports distincts. Tous deux lisent
le même état en lecture seule (le `PositionResult` courant et la liste des
statuts NTRIP), sans contention.

| Critère          | Option A — dashboard stdlib       | Option C — API FastAPI            |
|------------------|------------------------------------|------------------------------------|
| Module           | `files/http_server.py`            | `files/api_server.py`             |
| Dépendances      | **Aucune** (stdlib `http.server`) | `fastapi` + `uvicorn` (extra `api`) |
| Port par défaut  | `8080`                            | `8081`                            |
| Cible            | Humain (navigateur)               | Programmatique (app Android)      |
| Pull/Push        | Polling JS (`setInterval`)        | REST + WebSocket push             |
| Doc OpenAPI      | aucune                            | générée automatiquement sur `/docs` |
| CORS             | `Access-Control-Allow-Origin: *`  | configurable (`api.cors`)         |

Les deux sont **désactivés par défaut**. Pour les activer, dans
[`files/config.yaml`](files/config.yaml) :

```yaml
http:
  enabled: true       # ouvre le dashboard sur 8080
  host: "0.0.0.0"
  port: 8080
  refresh_ms: 500     # cadence du polling navigateur

api:
  enabled: true       # ouvre l'API REST/WS sur 8081
  host: "0.0.0.0"
  port: 8081
  cors: true
```

L'API nécessite un `uv sync --extra api` au préalable (voir [Installation](#installation)).
Si l'extra n'a pas été synchronisé, `api.enabled: true` ne plante pas
l'application : un warning est loggé et le serveur est simplement passé.

### Endpoints

**Dashboard (port 8080)**

| Méthode | Chemin            | Réponse                                            |
|---------|-------------------|----------------------------------------------------|
| GET     | `/`               | Dashboard HTML embarqué auto-rafraîchi en JS       |
| GET     | `/api/position`   | `{position: PositionResult, timestamp}`            |
| GET     | `/api/bases`      | `{bases: [...], timestamp}`                        |
| GET     | `/api/status`     | `{position, bases, timestamp}` (utilisé par la page) |

**API FastAPI (port 8081)**

| Méthode | Chemin            | Réponse                                            |
|---------|-------------------|----------------------------------------------------|
| GET     | `/api/health`     | `{ok, uptime}` — ping                              |
| GET     | `/api/position`   | identique au port 8080                             |
| GET     | `/api/bases`      | identique au port 8080                             |
| GET     | `/api/status`     | identique au port 8080                             |
| GET     | `/docs`           | Doc OpenAPI interactive (Swagger UI)               |
| WS      | `/ws/position`    | Push JSON identique au payload `/api/status` à chaque résultat VRS publié |

Le champ binaire `vrs_rtcm` du `PositionResult` est retiré avant
sérialisation : il n'est ni utile côté client ni nativement JSON.

### Cas d'usage

- **Test terrain** : activer uniquement `http.enabled` pour avoir un
  écran de supervision sur n'importe quel téléphone ou tablette connecté
  au même réseau que le PC de labo.
- **Intégration Android** : activer `api.enabled` et faire pointer le
  client Kotlin sur `http://<ip-du-pc>:8081/ws/position`. La WebSocket
  pousse un message JSON à chaque tour de boucle du moteur VRS (1 Hz).
  Pour une intégration plus simple, le polling REST sur `/api/status`
  fonctionne aussi très bien. **Feuille de route détaillée** dans
  [`docs/android-integration-roadmap.md`](docs/android-integration-roadmap.md)
  (3 phases : lecture seule → reconfiguration des balises → pipeline
  complet avec GNSS branché au mobile).
- **Démo** : activer les deux. Le dashboard pour l'audience humaine, l'API
  pour une éventuelle app de démonstration mobile.

Voir [`.claude/skills/nrtk-http-api.md`](.claude/skills/nrtk-http-api.md)
pour le détail de l'architecture (cohabitation, mécanisme de push WebSocket,
pièges connus, points d'extension comme l'authentification ou les endpoints
de configuration).

## Altitude et géoïde

Le pipeline distingue strictement deux références verticales et les
manipule dans cet ordre :

| Symbole | Sens                                            | Fournisseur                      |
|---------|-------------------------------------------------|----------------------------------|
| `H`     | altitude orthométrique (« terrain »)            | NMEA GGA, champ 9                |
| `N`     | ondulation du géoïde                            | NMEA GGA champ 11 (en entrée) ; RAF20 (en sortie) |
| `h`     | altitude ellipsoïdale = `H + N`                 | déduite                          |

**Convention interne** : `VrsEngine.update_rover_approx()` reçoit toujours
une altitude **ellipsoïdale `h`**. Les deux entrées GGA s'alignent dessus :

- En mode capteur réel, `serial_manager._parse_and_dispatch_gga` somme
  champ 9 + champ 11 avant d'appeler le callback.
- En mode mock, `_parse_and_update_gga` ([main.py:107](files/main.py#L107))
  fait le même calcul.

Le moteur VRS travaille en ellipsoïdal en interne (les calculs ECEF /
interpolation IDW / synthèse RTCM 1004-1005 le requièrent) et **redescend
en orthométrique uniquement à la publication du résultat**, via RAF20 :
`H_publiée = h_solveur − N_RAF20(lat, lon)`. Cela garantit que la sortie
est exprimée dans le quasi-géoïde français IGN69 indépendamment du modèle
de géoïde utilisé en interne par le récepteur.

> Limite connue : en mode *pont RTCM direct* (`vrs.enabled: false`),
> l'altitude affichée provient du champ 9 brut du GGA, donc référencée au
> modèle de géoïde du récepteur (typiquement EGM96/EGM2008 pour l'UM980),
> **et non à RAF20**. Un écart de quelques décimètres avec un matériel de
> référence calé sur IGN69 est alors attendu.

## Logs

Chaque exécution crée un fichier `files/logs/nrtk_log_AAAAMMJJ_HHMMSS.log`
contenant l'intégralité des messages applicatifs (connexions NTRIP, statut
fix, ondulations géoïdales, erreurs…), en plus de la sortie console. Le
chemin du log est rappelé à l'arrêt.