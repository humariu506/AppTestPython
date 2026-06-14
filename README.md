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
- [Mode VRS hybride](#mode-vrs-hybride)
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
- **`vrs.enabled`** : sélectionne le mode hybride ou le pont direct (voir
  la section [Mode VRS hybride](#mode-vrs-hybride) ci-dessous).
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

## Mode VRS hybride

Pour obtenir un **FIX RTK** côté récepteur tout en gardant le moteur VRS
actif pour la télémétrie, l'application utilise un schéma hybride :

- Le flux RTCM3 brut de la **première balise** (éphémérides, observations,
  position 1005) est **toujours forwardé** au UM980, qu'on soit en
  `vrs.enabled: true` ou `false`. C'est ce flux qui permet l'obtention
  du FIX (RTK mono-base classique, baseline = distance réelle au caster).
- Le moteur VRS continue de tourner en parallèle quand `vrs.enabled: true` :
  il interpole les corrections multi-balises, synthétise un `PositionResult`
  exposé dans l'UI tkinter, sur le dashboard HTTP et via l'API REST/WS.
- Les trames 1004/1005 synthétisées **ne sont plus poussées** vers le port
  série tant que leur encodage n'est pas conforme à RTCM 10403.3 — sans
  quoi le UM980 rejette les trames et perd le FIX.

Conséquence pratique :

| Mode                       | FIX obtenu ?    | Multi-balises ?             |
|----------------------------|-----------------|-----------------------------|
| `vrs.enabled: true`        | ✅ (via base[0]) | ❌ (en attendant vraie VRS) |
| `vrs.enabled: false`       | ✅ (via base[0]) | ❌                          |

Autrement dit, `vrs.enabled` ne change pas (encore) la précision RTK ; il
contrôle uniquement la richesse de la télémétrie. L'effet « vraie VRS »
nécessiterait de réparer `build_vrs_rtcm_1004` (74 bits/satellite au lieu
des 125 du standard) et d'ajouter le forwarding des éphémérides — voir
[`.claude/skills/nrtk-vrs-hybrid.md`](.claude/skills/nrtk-vrs-hybrid.md)
pour la feuille de route.

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