# Intégration Android ↔ Python NRTK — feuille de route

## Contexte

L'application Android existante (Kotlin / Android Studio) permet aujourd'hui à un
opérateur de se connecter à **une seule** balise Centipede, avec le récepteur
GNSS (UM980 + antenne) branché à l'appareil mobile via USB OTG.

L'objectif est de faire collaborer cette application avec le serveur Python NRTK
(ce dépôt) pour bénéficier d'un calcul VRS multi-balises.

Le but final est que l'**Android** soit le point de branchement du matériel
GNSS et que le **PC Python** soit le moteur de calcul, communicants par
WiFi/LAN via l'API REST + WebSocket déjà exposée par
[`files/api_server.py`](../files/api_server.py).

Pour minimiser les risques, on procède en **trois phases** indépendantes
livrables et testables séparément.

```
                       Phase 1                   Phase 2                  Phase 3
                    ┌───────────┐             ┌───────────┐            ┌───────────┐
        Android ───►│  Lecture  │             │  Choix    │            │   GGA     │◄── GNSS USB
                    │  position │   ───►      │  balises  │   ───►     │   RTCM    │──► GNSS USB
                    │   (RO)    │             │  (config) │            │   bidir   │
                    └─────┬─────┘             └─────┬─────┘            └─────┬─────┘
                          │                         │                        │
                          ▼                         ▼                        ▼
                       Python                    Python                   Python
                  (capteur + antenne          (reconfig.                (capteur côté
                   sur PC, calcul VRS         dynamique des              Android, calcul
                   en cours)                  NTRIP clients)             VRS sur PC)
```

État actuel du projet (à la date d'écriture) : **Phase 1 est entièrement
réalisable côté Python** — il ne reste qu'à brancher le client Android.

---

## Phase 1 — Visualisation read-only sur Android

### Objectif

L'utilisateur saisit l'adresse IP du PC qui fait tourner le serveur Python,
et voit sur son téléphone, en temps réel, la position VRS calculée par le PC
ainsi que le statut des connexions NTRIP.

Aucun envoi de données depuis Android vers Python : c'est strictement de la
lecture.

### Pré-requis

- Le PC et le téléphone sont sur le même réseau WiFi (LAN).
- Le serveur Python tourne avec `api.enabled: true` dans `config.yaml` et les
  dépendances FastAPI installées (`uv sync --extra api`).
- Le pare-feu Windows autorise le port `8081` en entrée (à documenter dans
  les notes utilisateur).

### Côté Python — déjà fait

Aucune modification nécessaire. Les endpoints suivants sont déjà exposés :

- `GET /api/health` → ping (`{ok: true, uptime: float}`)
- `GET /api/status` → `{position, bases, timestamp}` (snapshot)
- `WS /ws/position` → push JSON identique à chaque résultat VRS (1 Hz)
- `GET /docs` → documentation OpenAPI interactive
- CORS `Access-Control-Allow-Origin: *` activé par défaut

Référence : [`.claude/skills/nrtk-http-api.md`](../.claude/skills/nrtk-http-api.md).

### Côté Android — à faire

1. **Écran « Configuration serveur »** *(nouveau)*
   - Champ `Adresse serveur` (ex. `192.168.1.10:8081`)
   - Bouton `Tester` → effectue un `GET /api/health` avec timeout 3 s,
     affiche ✅ / ❌
   - Persistance dans `SharedPreferences` ou `DataStore`
2. **Dépendance HTTP**
   - Recommandé : **OkHttp** (déjà dans la plupart des projets Android)
     ou **Ktor-Client** pour une stack 100 % Kotlin
   - Sérialisation : **kotlinx-serialization** (recommandé) ou Gson
3. **Modèles Kotlin** *(à mapper depuis le JSON Python)*
   ```kotlin
   @Serializable
   data class StatusPayload(
       val position: PositionResult?,
       val bases: List<BaseStatus>,
       val timestamp: Double,
   )

   @Serializable
   data class PositionResult(
       val timestamp: Double = 0.0,
       val lat: Double = 0.0,
       val lon: Double = 0.0,
       val alt: Double = 0.0,                       // orthométrique RAF20
       @SerialName("alt_ellipsoidal") val altEllipsoidal: Double = 0.0,
       @SerialName("fix_status") val fixStatus: String = "NONE",
       @SerialName("sigma_h") val sigmaH: Double = 999.0,
       @SerialName("sigma_v") val sigmaV: Double = 999.0,
       @SerialName("n_sats_used") val nSatsUsed: Int = 0,
       @SerialName("n_bases_used") val nBasesUsed: Int = 0,
       @SerialName("geoid_undulation") val geoidUndulation: Double = 0.0,
       // ... voir vrs_engine.PositionResult pour le détail
   )

   @Serializable
   data class BaseStatus(
       val id: String,
       val mountpoint: String,
       val host: String,
       val connected: Boolean,
       @SerialName("msg_count") val msgCount: Int,
       @SerialName("last_msg_age") val lastMsgAge: Double?,
       @SerialName("last_error") val lastError: String?,
   )
   ```
4. **Couche réseau** — deux variantes au choix :
   - **Variante simple : polling REST** (recommandé pour le MVP)
     - `GET /api/status` toutes les 500 ms via OkHttp + coroutine
     - Émet sur un `StateFlow<StatusPayload?>`
   - **Variante propre : WebSocket** (à privilégier pour la latence)
     - `ws://IP:PORT/ws/position` via OkHttp `WebSocketListener`
     - Reconnexion automatique avec backoff exponentiel : 1 s, 2 s, 5 s, 10 s, max 30 s
     - Émet sur un `StateFlow<StatusPayload?>`
5. **Écran « Position serveur »** *(nouveau)*
   - Bandeau coloré « FIX / FLOAT / SINGLE / NONE » avec la palette de
     `ui.py` (vert/orange/bleu/rouge) — voir
     [`files/ui.py`](../files/ui.py) pour les codes hex
   - Champs `Latitude`, `Longitude`, `Altitude`, `Ondulation N (RAF20)`
   - Bloc précision : `σH`, `σV`, `# sats`, `# bases`
   - Liste des bases (id, mountpoint, statut connecté/déconnecté, msg/s)
   - Pastille connexion serveur (verte/rouge)
6. **Permission `INTERNET`** *(déjà standard dans `AndroidManifest.xml`)*
7. **`network_security_config.xml`** — autoriser le trafic cleartext HTTP
   en LAN (Android Pie+ le bloque par défaut)
   ```xml
   <network-security-config>
     <domain-config cleartextTrafficPermitted="true">
       <domain includeSubdomains="true">192.168.0.0/16</domain>
       <domain includeSubdomains="true">10.0.0.0/8</domain>
     </domain-config>
   </network-security-config>
   ```

### Validation Phase 1

1. Démarrer le Python en mode mock complet
   (`sensor.mock: true`, `ntrip.mock: true`, `api.enabled: true`).
2. Récupérer l'IP du PC (`ipconfig`) et la saisir dans Android.
3. Vérifier que la position s'affiche et se rafraîchit toutes les 500 ms.
4. Couper le WiFi du téléphone → l'app doit afficher « serveur injoignable »
   sans crasher.
5. Couper le serveur Python → idem.
6. Relancer le serveur → la reconnexion automatique doit prendre la main.

### Pièges Phase 1

- **Cleartext HTTP** : sans `network_security_config.xml`, Android 9+ refuse
  les requêtes vers `http://`. Symptôme : `CleartextNotPermittedException`.
- **Pare-feu Windows** : il bloque le port 8081 par défaut pour les
  connexions entrantes. À débloquer via PowerShell :
  `New-NetFirewallRule -DisplayName "NRTK API" -Direction Inbound -LocalPort 8081 -Protocol TCP -Action Allow`
- **Polling vs WebSocket** : pour un MVP, le polling 500 ms suffit. Si la
  latence visible est gênante, basculer sur WS — c'est ~ 50 lignes de Kotlin
  en plus.
- **IP dynamique du PC** : si le DHCP change l'IP, il faut la ressaisir.
  Une découverte mDNS (cf. annexe) résoudrait ça en Phase 4 si besoin.

---

## Phase 2 — Sélection de balises depuis Android

### Objectif

L'écran de sélection de balises existant côté Android transmet la sélection
au serveur Python, qui reconfigure dynamiquement ses clients NTRIP. Pas
besoin de redémarrer le PC.

### Côté Python — à faire

1. **Modèle Pydantic** dans `files/api_server.py`
   ```python
   from pydantic import BaseModel

   class BaseConfig(BaseModel):
       id: str
       host: str
       port: int = 2101
       mountpoint: str
       lat: float
       lon: float
       alt: float = 0.0

   class BasesConfigRequest(BaseModel):
       bases: list[BaseConfig]
   ```
2. **Endpoint POST `/api/config/bases`**
   - Reçoit `BasesConfigRequest`, retourne `{"ok": true, "count": N}`
   - En cas d'erreur (config invalide, échec de connexion NTRIP),
     retourne `{"ok": false, "error": "..."}`
   - Appelle une nouvelle méthode `NrtkApp.reconfigure_bases(bases_list)`
3. **Méthode `NrtkApp.reconfigure_bases`** dans `files/main.py`
   - Stoppe tous les `NtripClient` en cours (`for c in self._ntrip_clients: c.stop()`)
   - Vide `self._ntrip_clients = []`
   - Met à jour `self._cfg["bases"] = [b.dict() for b in bases]`
   - Relance `_start_real_ntrip()` (en mode réel) ou `_start_mock_ntrip()`
   - Protéger l'ensemble par un `threading.Lock` pour éviter qu'une trame
     RTCM en cours de réception ne référence une liste de bases obsolète
4. **Notification du moteur VRS** que la liste a changé
   - Le `VrsInterpolator` n'est pas couplé à une liste de bases fixe (il
     itère sur les bases présentes dans `ObservationStore`), donc rien à
     changer côté VRS — mais purger `self._store` pour repartir propre
5. **Documentation** : ajouter le nouvel endpoint dans
   [`.claude/skills/nrtk-http-api.md`](../.claude/skills/nrtk-http-api.md)
   et dans la doc OpenAPI (FastAPI le fait automatiquement)

### Côté Android — à faire

1. Sur l'écran de sélection existant, au clic sur le bouton « Démarrer
   NRTK » :
   - Récupérer la liste des balises cochées (avec `id`, `host`, `port`,
     `mountpoint`, `lat`, `lon`, `alt` déjà connus à l'app)
   - `POST /api/config/bases` avec body JSON
   - Sur 200 OK → naviguer vers l'écran de position de la Phase 1
   - Sur erreur → snackbar avec le message renvoyé par le serveur
2. Cas particuliers à gérer :
   - L'utilisateur ne coche aucune balise → désactiver le bouton
   - L'utilisateur coche plus de 10 balises → warning (la qualité VRS
     n'augmente pas linéairement avec le nombre de bases)

### Validation Phase 2

1. Démarrer Python en mode réel partiel
   (`sensor.mock: true`, `ntrip.mock: false`, `api.enabled: true`).
2. Cocher 5 balises Centipede différentes sur Android, lancer NRTK.
3. Vérifier dans les logs Python : 5 lignes
   `[INFO] NTRIP démarré : <id> → <host>:2101/<mountpoint>`
4. Vérifier que le dashboard HTTP (`http://PC:8080/`) montre les 5
   nouvelles balises (et plus celles du `config.yaml` initial).
5. Modifier la sélection Android et relancer → les anciennes connexions
   doivent être coupées, les nouvelles établies.

### Pièges Phase 2

- **Arrêt propre des threads NTRIP** : `NtripClient.stop()` met
  `_running = False` et ferme le socket, mais le thread peut mettre une
  seconde à terminer son `recv()` en cours. Attendre avec
  `t.join(timeout=2.0)` avant de relancer.
- **Race condition sur `ObservationStore`** : pendant la reconfiguration,
  une trame RTCM en vol peut être décodée alors que la base correspondante
  n'est plus dans la liste. Acceptable (elle ira dans le store, ne sera
  juste pas utilisée), mais à logger pour le diagnostic.
- **Validation côté serveur** : si une balise n'existe pas (mauvais
  mountpoint), le caster NTRIP ferme la connexion. Remonter l'erreur à
  l'Android par le retour de l'endpoint POST plutôt que par les logs.

---

## Phase 3 — Pipeline complet : GNSS sur mobile, calcul sur PC

### Objectif

L'antenne GNSS branchée au téléphone alimente le calcul VRS sur le PC, et
les corrections RTCM3 reviennent vers le téléphone pour être écrites au
récepteur via USB OTG. Le PC n'a plus besoin du matériel GNSS.

### Côté Python — à faire

1. **WebSocket bidirectionnel `/ws/rover`** dans `files/api_server.py`
   - Le client (Android) envoie périodiquement (1 Hz) un message JSON :
     ```json
     {"type": "gga", "lat": 48.84, "lon": 2.36, "alt": 100.0,
      "fix_quality": 4, "timestamp": 1718469000.123}
     ```
   - Le serveur pousse à chaque résultat VRS une trame RTCM3 :
     ```json
     {"type": "rtcm", "data": "<base64-de-bytes>",
      "timestamp": 1718469000.456}
     ```
     ou directement en frame binaire WebSocket si on veut épargner la
     bande passante (~ 30 % d'overhead base64).
2. **Hook `NrtkApp.update_rover_from_remote(lat, lon, alt)`**
   - Court-circuite la lecture du `SerialManager` local
   - Pousse la position dans `self._vrs.update_rover_approx(lat, lon, alt)`
3. **Bascule du mode capteur**
   - Nouvelle valeur `sensor.source: "local" | "remote"` dans `config.yaml`
   - `local` : comportement actuel, lecture du UM980 sur le PC
   - `remote` : ne démarre pas le `SerialManager`, attend les GGA via WS
4. **Push RTCM via `/ws/rover`**
   - Réutiliser le mécanisme `publish_result` existant
   - Quand un client `/ws/rover` est actif et qu'on est en `pure_mode` ou
     que `result.vrs_rtcm` est non vide, envoyer la trame
5. **Endpoint REST de fallback** pour les transitions
   - `GET /api/rover/last_rtcm` qui retourne la dernière trame RTCM en
     base64 — utile pour debugging Android sans Wireshark

### Côté Android — à faire

1. **Lecture du flux NMEA via USB OTG** *(probablement déjà fait par
   l'app actuelle pour le mode mono-balise)*
   - Lib usuelle : `usb-serial-for-android` (`mik3y`)
2. **Parsing GGA** *(déjà fait)*
   - Extraire lat, lon, alt, fix_quality
3. **Envoi WS périodique** au serveur Python
   - Une trame GGA par seconde suffit
4. **Réception des frames RTCM** depuis le WS
   - Décoder base64 → `ByteArray`
   - Écrire au récepteur via la même connexion USB OTG (en sens inverse
     du flux NMEA)
5. **Écran « Mode VRS distant »**
   - Pastille « PC connecté » + IP
   - Compteur d'octets RTCM reçus / GGA envoyés
   - Indicateur de latence aller-retour
6. **Désactiver le mode mono-balise existant** quand le mode distant est
   actif, ou les faire cohabiter avec un toggle

### Validation Phase 3

1. Mode Python : `sensor.source: "remote"`, `api.enabled: true`.
2. Brancher le UM980 au téléphone (USB OTG).
3. Lancer l'app Android, basculer en mode VRS distant.
4. Vérifier dans les logs Python : `GGA reçue de l'Android (lat, lon, alt)`
   à 1 Hz, et `RTCM poussée vers Android` à 1 Hz.
5. Vérifier que le UM980 (via les LED ou via NMEA renvoyé) passe en FIX.
6. Comparer la précision avec le mode « PC seul » sur les mêmes balises.

### Pièges Phase 3

- **Latence WiFi** : si supérieure à 100 ms, l'effet RTK se dégrade.
  Tester en LAN 5 GHz idéalement.
- **Désynchronisation d'horloges** : le TOW inscrit dans les trames RTCM
  côté Python est `time.time()`, et le récepteur côté Android compare à
  ses propres observations. Une dérive > 50 ms entre les deux machines
  provoque des écarts inexploitables. Mesurer la dérive et corriger côté
  serveur si nécessaire (ou utiliser NTP).
- **Bande passante** : RTCM ~ 2 kB/s par direction, GGA ~ 100 B/s. Tient
  largement sur un WiFi domestique mais peut être problématique sur 4G.
- **Driver USB OTG** : la compatibilité dépend du modèle Android et du
  récepteur. À tester avant tout le reste.
- **Cohabitation avec le mode mono-balise existant** : les deux modes
  utilisent le même port USB. Prévoir un sémaphore Android pour éviter
  les conflits.
- **Sécurité** : pas de chiffrement TLS pour l'instant — acceptable en
  LAN privé, à durcir si exposition Internet (cf. annexe).

---

## Annexes

### A. Authentification

Pour un MVP en LAN privé, pas d'authentification. À terme, prévoir un
token Bearer dans `config.yaml: api.token`, vérifié par un middleware
FastAPI :

```python
from fastapi import Header, HTTPException

async def verify_token(authorization: str = Header(...)):
    expected = f"Bearer {self._cfg['api']['token']}"
    if authorization != expected:
        raise HTTPException(status_code=401)

@app.get("/api/status", dependencies=[Depends(verify_token)])
async def get_status(): ...
```

Côté Android : ajouter `Authorization: Bearer <token>` à toutes les
requêtes via un OkHttp `Interceptor`.

### B. Découverte automatique (mDNS / Zeroconf)

Pour éviter à l'utilisateur de saisir l'IP du PC manuellement :

- Côté Python : annoncer le service via la lib `zeroconf` :
  ```python
  from zeroconf import ServiceInfo, Zeroconf
  info = ServiceInfo("_nrtk._tcp.local.", "NRTK-Server._nrtk._tcp.local.",
                     addresses=[socket.inet_aton(local_ip)],
                     port=8081, properties={})
  Zeroconf().register_service(info)
  ```
- Côté Android : utiliser `NsdManager` pour découvrir les services
  `_nrtk._tcp` sur le LAN.

À faire après Phase 2 si l'ergonomie le demande.

### C. Persistance côté Android

- `DataStore` (recommandé sur AndroidX moderne) pour `serverAddress`,
  `selectedBases`, `authToken` (futur).
- Pas de cache long terme du `PositionResult` : c'est volatile.

### D. Tests sans matériel

Pour qu'un développeur Android puisse travailler sans antenne ni
récepteur :

1. Lancer Python en mock complet
   (`sensor.mock: true`, `ntrip.mock: true`, `vrs.enabled: true`).
2. L'application génère elle-même des trames NMEA + RTCM3 cohérentes.
3. L'API expose un `PositionResult` synthétique à 1 Hz, parfaitement
   réaliste pour développer l'UI.

### E. Logs côté Android

- Utiliser **Timber** plutôt que `Log.d` directement.
- À terme, un endpoint Python `POST /api/logs` pourrait recevoir les logs
  Android pour debugging à distance — utile en démo terrain.

### F. Liste de contrôle finale (Phase 1)

À cocher avant de considérer la Phase 1 terminée :

- [ ] Écran de configuration serveur fonctionnel, avec test de connexion
- [ ] Persistance de l'IP serveur
- [ ] Modèles Kotlin alignés sur le JSON Python (cf. tests par mocking)
- [ ] Connexion REST polling **OU** WebSocket fonctionnelle
- [ ] Reconnexion automatique sur perte de connexion
- [ ] Écran de visualisation : position, fix, précision, bases
- [ ] `network_security_config.xml` configuré pour LAN
- [ ] Documentation utilisateur : comment trouver l'IP du PC, comment
      ouvrir le port 8081 dans le pare-feu Windows
- [ ] Test de bout en bout : Python mock → Android affiche position

---

## Documentation associée

- État actuel de l'API Python : [`.claude/skills/nrtk-http-api.md`](../.claude/skills/nrtk-http-api.md)
- Modes VRS et leur impact sur ce qui est publié :
  [`.claude/skills/nrtk-vrs-hybrid.md`](../.claude/skills/nrtk-vrs-hybrid.md)
- Vue d'ensemble du projet : [`.claude/skills/nrtk-project.md`](../.claude/skills/nrtk-project.md)
- README principal : [`README.md`](../README.md)
