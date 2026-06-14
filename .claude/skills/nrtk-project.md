---
name: nrtk-project
description: Charge le contexte du projet NRTK/Centipede (application Python de positionnement GNSS centimétrique). À invoquer dès qu'on touche au code de `files/` (main.py, vrs_engine.py, geoid.py, serial_manager.py, mock_generator.py, ntrip_client.py, rtcm_decoder.py, ui.py), au config.yaml, ou qu'on diagnostique un problème de précision (latitude/longitude/altitude), de fix RTK, ou de flux RTCM3. Contient les conventions altimétriques, les pièges connus, les modes de fonctionnement et la liste des bugs documentés.
---

# Projet NRTK Centipede — savoir-faire interne

Application Python de positionnement GNSS centimétrique par méthode **NRTK** (Network RTK) sur le réseau open-source **Centipede**, développée pendant un stage à NaTran SPESY. Récepteur cible : Unicore **UM980** connecté en USB. Référence verticale officielle : **RAF20** (IGN).

## Organisation du code (`files/`)

| Fichier | Rôle |
|---|---|
| `main.py` | Orchestration, gestion des modes, callbacks UI |
| `vrs_engine.py` | Interpolation IDW, synthèse RTCM 1004/1005, solveur RTKLIB/WLS |
| `rtcm_decoder.py` | Parsing RTCM3 in-house + `ObservationStore` thread-safe |
| `ntrip_client.py` | Client NTRIP v1/v2 (1 instance par balise) |
| `serial_manager.py` | Accès thread-safe au port série du UM980 |
| `mock_generator.py` | Simulation NMEA + RTCM3 pour développement hors site |
| `geoid.py` | Chargeur grille RAF20 (.gtx) + interpolation bilinéaire |
| `ui.py` | Interface tkinter (couleurs sombres, rafraîchissement non-bloquant) |
| `config.yaml` | Config unique (bases, NTRIP, sensor, vrs, mock, ui) |

## Modes de fonctionnement

Le mode effectif est la combinaison de **trois booléens** dans `config.yaml` :

| `sensor.mock` | `ntrip.mock` | `vrs.enabled` | Comportement UI |
|:---:|:---:|:---:|---|
| `true` | `true` | `true` | UI pilotée par le moteur VRS (mock complet, **RAF20 appliqué ✅**) |
| `true` | `false` | `true` | Hybride : UI pilotée par VRS, balises réelles |
| `false` | * | `true` | UI pilotée par `_on_real_sensor_ui_update` à partir de la GGA du UM980. Le VRS génère le RTCM envoyé en série. |
| `false` | * | `false` | **Pont RTCM direct** : la trame brute de `bases[0]` est forwardée au UM980 sans calcul. C'est ce qui marche le mieux pour obtenir un FIX RTK. |

## Pipeline altimétrique — convention impérative

**Trois grandeurs** se promènent dans le code, à ne JAMAIS confondre :

- `H` = altitude **orthométrique** (« terrain »), champ 9 du GGA
- `N` = ondulation du géoïde (champ 11 du GGA = N selon le récepteur ; ou lue depuis RAF20)
- `h` = altitude **ellipsoïdale** = `H + N`, indispensable pour les calculs ECEF

**Règle d'or** : `update_rover_approx(lat, lon, alt)` ([vrs_engine.py:494](files/vrs_engine.py)) attend **toujours** une altitude ellipsoïdale `h`. Les deux chemins GGA doivent donc être alignés :

- Mode mock : `_parse_and_update_gga` ([main.py:107](files/main.py)) lit champ 9 + champ 11 et passe la somme
- Mode réel : `serial_manager._parse_and_dispatch_gga` ([serial_manager.py:150](files/serial_manager.py)) fait pareil

Le moteur VRS travaille en `h` partout, et reconvertit en `H_RAF20 = h - N_RAF20(lat, lon)` **uniquement à la dernière étape**, dans `_compute_epoch` ([vrs_engine.py:586-593](files/vrs_engine.py)).

**Piège historique du projet** : double-comptage de `N` (une fois via GGA champ 11, une fois via RAF20) → décalage d'environ 45-50 m en région parisienne. Si on observe à nouveau un écart de cet ordre, c'est le premier suspect : tracer chaque altitude entre modules avec `logger.debug`.

## Limite connue : `_on_real_sensor_ui_update`

En mode capteur réel, l'UI affichée n'est pas celle du moteur VRS mais celle calculée à partir de la GGA renvoyée par le UM980 (`_on_real_sensor_ui_update`, [main.py:299](files/main.py)). RAF20 est appliqué à cet endroit (lignes 308-320) ; bien vérifier que :
- `alt_ellip_antenna = alt + geoid_sep` (passage en h via le N du UM980)
- `alt_corrected = alt_ellip_ground - N_RAF20` (descente vers H IGN69 via RAF20)

Côté `serial_manager.py:185`, le callback est appelé avec **7 arguments** (`lat, lon, alt, geoid_sep, fix_quality, num_sats, hdop`) ; la signature de `_on_real_sensor_ui_update` doit accepter ces 7 paramètres dans cet ordre. Une désynchronisation entraîne un fix_quality lu comme = geoid_sep ≈ 47 → fix toujours en `NONE`.

## Architecture multithread

8 threads coopérants, jamais d'appel bloquant dans le thread UI :

- `Principal` : mainloop tkinter (ou idle en `--no-ui`)
- `serial-read` : lecture continue du UM980
- `serial-write` : dépile une `queue.Queue` vers le port série
- `ntrip-<base_id>` × 5 : un par balise Centipede (HTTP NTRIP + envoi GGA périodique)
- `vrs-engine` : recalcule à 1 Hz, interpole IDW, synthétise RTCM, appelle solveur

Tous les états partagés sont protégés par `threading.Lock` ou `RLock`. Le `SerialManager` est le **seul** point d'entrée pour lire/écrire le port — toute concurrence (terminal Unicore ouvert en parallèle) casse le flux.

## Bugs RTCM3 connus dans la synthèse VRS

⚠️ Ces bugs expliquent pourquoi `vrs.enabled: true` ne donne **PAS** de FIX RTK actuellement (le UM980 reste en `SinglePoint`).

### 1. RTCM 1005 mal aligné — [vrs_engine.py:249-259](files/vrs_engine.py)

Le code packe 148 bits dans 19 octets (152 bits), laissant **4 bits de padding en tête** au lieu d'en queue. Le standard RTCM 1005 fait exactement 152 bits utiles (les flags GPS/GLO/GAL/RefStation + Oscillator + Quarter-Cycle manquent dans la version actuelle).

### 2. RTCM 1004 tronqué — [vrs_engine.py:262-284](files/vrs_engine.py)

Chaque satellite est packé sur **74 bits** au lieu des **125 bits** exigés par le standard. Toute la partie L2 (Code indicator, Pseudorange diff, PhaseRange, Lock Time, CNR) est absente. Le UM980 rejette les trames.

### 3. Pas de continuité du `lock_time` — [vrs_engine.py:281](files/vrs_engine.py)

`(0 << 17)` à chaque trame → le UM980 voit un reset permanent du verrouillage de phase, impossible de résoudre les ambiguïtés entières.

### 4. Pas d'éphémérides en mode VRS — [main.py:213-218](files/main.py)

Quand `vrs.enabled: true`, seules les trames synthétisées 1004+1005 sont envoyées au UM980. Les messages 1019 (éphémérides GPS), 1020 (GLONASS), etc. sont perdus.

### Patch pragmatique recommandé

L'approche la plus rapide pour obtenir un FIX en VRS, sans réécrire tout l'encodage : **mode hybride**. Continuer à forwarder le flux brut de `bases[0]` (éphémérides + observations) ET injecter en plus un 1005 synthétisé corrigé à la position du rover. C'est ce que font les vrais casters VRS commerciaux.

À très court terme, pour découpler le diagnostic « calcul VRS correct ? » de « encodage RTCM correct ? » : forwarder *aussi* le flux brut quand VRS est activé.

## Hauteur d'antenne — vérification matérielle

`config.yaml: sensor.antenna_height` est retirée de l'altitude dans `_on_position_result` ([main.py:259-262](files/main.py)) et dans `_on_real_sensor_ui_update` ([main.py:313-314](files/main.py)).

Si le firmware UM980 a aussi été configuré avec un offset d'antenne (`CONFIG ANTENNADELTAHEN` côté Unicore), la soustraction est **comptée deux fois**. À vérifier avec la commande `CONFIG` sur le récepteur avant chaque campagne de mesures.

## Stack technique et outils

- Python ≥ 3.14, env géré par `uv`, lockfile dans `uv.lock`
- Dépendances : `numpy`, `pyrtcm`, `pyserial`, `pyyaml`
- ⚠️ **`pyrtcm` est actuellement désactivé** dans le code (cf. README) — entrait en conflit avec la réception GNSS. Décodeur RTCM interne uniquement.
- RTKLIB optionnel (chemin dans `config.yaml: rtklib.rtkpost_path`). À défaut, solveur WLS Python interne (précision décimétrique).
- Mode console : `python files/main.py --no-ui --real --log-level DEBUG`
- Logs horodatés dans `files/logs/nrtk_log_AAAAMMJJ_HHMMSS.log`

## Tests terrain

Le récepteur étalon utilise un réseau RTK commercial (**Ophéon**, abonnement payant). Comparer toujours en logs DEBUG pour tracer la chaîne complète d'altitudes. Les points de référence sont des clous plantés sur le parking arrière du centre R&I Villeneuve-la-Garenne.

## Référentiels et géodésie

- WGS84 / ITRF pour le GNSS interne (ellipsoïde de référence)
- RAF20.gtx pour le quasi-géoïde IGN69 sur la France métropolitaine
- Le UM980 utilise EGM96 ou EGM2008 en interne (selon la version firmware) — peut différer de RAF20 de quelques dizaines de cm. C'est attendu, et c'est pour ça que tout le pipeline se ramène en ellipsoïdal avant la conversion finale RAF20.

## Quand intervenir avec prudence

- Ne pas modifier les conventions altimétriques (`h` partout, conversion via RAF20 à la sortie) sans relire d'abord [README.md § Altitude et géoïde](README.md).
- Ne pas réactiver `pyrtcm` sans tester en condition réelle qu'il n'interfère plus avec la réception GNSS.
- Toute modification du `SerialManager` doit préserver l'invariant : un seul point d'accès en lecture/écriture.
- Toute modification de l'encodage RTCM doit être validée bit à bit contre le standard 10403.3.
