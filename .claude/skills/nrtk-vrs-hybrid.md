---
name: nrtk-vrs-hybrid
description: Charge le contexte des trois modes VRS de l'application (pont direct, hybride, VRS pure). À invoquer dès qu'on discute du FIX RTK, de l'option `vrs.enabled` ou `vrs.pure_mode`, du forwarding de trames RTCM3 vers le port série, ou des messages synthétisés 1005/1002. Documente la matrice des modes, les invariants par mode, ce qui est conforme aujourd'hui dans la synthèse RTCM, et les étapes encore ouvertes (GLONASS 1012, alignement TOW).
---

# VRS — savoir-faire interne

Référence : ce skill couvre la chronologie de mise au point du chemin VRS, depuis l'état initial où `vrs.enabled: true` empêchait le UM980 d'obtenir un FIX RTK (à cause des encodages 1004/1005 non conformes) jusqu'à l'introduction d'un mode hybride (FIX garanti, télémétrie complète) et d'un mode VRS pure expérimental (synthèse 1005 + 1002 conforme + éphémérides forwardées).

## Matrice des modes

Trois modes, sélectionnés par `config.yaml: vrs.enabled` × `vrs.pure_mode` :

| `enabled` | `pure_mode` | Nom court    | Flux RTCM vers UM980                                          | Moteur VRS            |
|:---------:|:-----------:|--------------|---------------------------------------------------------------|-----------------------|
| `false`   | *           | Pont direct  | flux brut intégral de `bases[0]`                              | éteint                |
| `true`    | `false`     | **Hybride**  | flux brut intégral de `bases[0]`                              | actif, télémétrie seule |
| `true`    | `true`      | VRS pure     | éphémérides du flux brut + 1005/1002 synthétisés au rover     | actif, RTCM poussé    |

Par défaut : **hybride**. Le pont direct est l'option simple si on veut désactiver tout calcul. La VRS pure est **expérimentale** : à activer en conditions terrain, avec un plan B (basculer en hybride) si le UM980 reste en SinglePoint.

Implémentation : voir les commentaires « Forward RTCM vers le UM980 » dans `_on_rtcm` ([files/main.py](files/main.py)) et « Envoi du flux VRS synthétisé » dans `_on_position_result`.

## Sémantique du flag `vrs.enabled`

- En **mode mock** : `enabled: true` fait piloter l'UI par le moteur VRS (position interpolée), `enabled: false` laisse le mock générer la position directement.
- En **mode capteur réel** : `enabled` ne change pas la position affichée dans l'UI tkinter (toujours la GGA du UM980 via `_on_real_sensor_ui_update`). Il contrôle uniquement la richesse de la télémétrie côté API/dashboard et, croisé avec `pure_mode`, le contenu du flux série.

## État de la synthèse RTCM (au moment de l'écriture)

### ✅ Conforme RTCM 10403.3

- **`build_vrs_rtcm_1005`** ([files/vrs_engine.py](files/vrs_engine.py)) — 152 bits exactement, ECEF X/Y/Z en complément à deux signé 38 bits via `_to_signed_bits`, flags DF022/DF023/DF024/DF141, DF142 et DF364 présents. Smoke-tests passent (longueur trame, type, ref ID).
- **`build_vrs_rtcm_1002`** ([files/vrs_engine.py](files/vrs_engine.py)) — RTCM 1002 (Extended L1-Only GPS RTK Observables), en-tête 64 bits, 74 bits/satellite, padding LSB pour aligner sur octet. Signe respecté pour DF012 (PhaseRange − Pseudorange). DF010 = 0 (C/A code). DF015 = 180 (≈ 45 dB-Hz fixe — voir limites).
- **`_encode_lock_time_7b`** — encodage DF013 selon la table 4.3-3 du standard (échelle non linéaire, 0..127).
- **Suivi du lock time par satellite** dans `VrsEngine._lock_times` ([files/vrs_engine.py](files/vrs_engine.py)) — incrémenté à chaque époque où le PRN reste observé, purgé sur disparition (cycle slip = perte de lock signalée au récepteur).

### ⚠️ Limites assumées

- **CNR codé en dur** à 180 (≈ 45 dB-Hz). Idéalement on devrait propager le SNR mesuré par chaque base via `SatObs.snr_L1` jusqu'à l'interpolateur, puis pondérer dans DF015. Comme l'interpolateur ne le fait pas encore, on reste sur une valeur constante crédible.
- **Pas de GLONASS** dans la synthèse (le 1012 n'est pas généré). Voir étape « À faire » ci-dessous.
- **TOW** courant utilisé tel quel via `time.time() % 604800` au lieu d'un alignement entre les époques des balises. Acceptable pour un test simple, à fixer pour une VRS robuste.
- **`build_vrs_rtcm_1004`** est conservé comme wrapper deprecated qui délègue à 1002 — à supprimer une fois qu'on a confirmé qu'aucun caller externe ne s'en sert.

## Pourquoi pas remplacer juste le 1005 sans toucher aux observations ?

C'était la première intuition pour faire « croire » au UM980 que la base est au rover (baseline ≈ 0 → FIX instantané). Mais c'est **incorrect** sans rééécriture des observations : si on annonce la base au rover mais qu'on envoie des observations issues d'une base réelle à 10 km, le calcul différentiel diverge — le UM980 voit des delta-pseudoranges incohérents avec la baseline annoncée et soit rejette le fix, soit converge sur une position fausse.

C'est précisément ce que fait `VrsInterpolator.interpolate()` ([files/vrs_engine.py](files/vrs_engine.py)) : il recompose les observations comme si une base virtuelle observait depuis le rover. Le mode VRS pure pousse ces observations dans un 1002 conforme. La conformité du 1005 seule ne suffit donc pas — c'est la cohérence 1005 ↔ 1002 qui fait la VRS.

## Feuille de route — état d'avancement

L'objectif est de passer de la VRS expérimentale à une VRS robuste en production.

| # | Étape                                                            | Statut       |
|---|------------------------------------------------------------------|--------------|
| 1 | Réparer l'encodage du message d'observations GPS L1              | ✅ (1002 conforme) |
| 2 | Maintenir le lock time entre époques                             | ✅ (`_lock_times`)  |
| 3 | Forwarder explicitement les éphémérides en mode VRS pure         | ✅ (`EPHEMERIS_RTCM_TYPES` + `_peek_rtcm_type` dans main.py) |
| 4 | Encoder GLONASS (message 1012)                                   | ❌ TODO       |
| 5 | Aligner les TOW entre balises au moment de l'interpolation       | ❌ TODO       |
| 6 | Tester sans pyrtcm en condition réelle                           | À valider    |

### Étape 4 — GLONASS 1012 (non faite)

Pour propager les observations GLONASS dans la VRS, il faudrait :

1. Étendre `VrsInterpolator` pour distinguer GPS de GLO : itérer sur `(prn, system)` au lieu de `prn` seul, et garder une longueur d'onde par-satellite (GLO utilise FDMA, longueur dépendante du FCN).
2. Tracker le FCN (Frequency Channel Number, −7 à +6) par satellite. Aujourd'hui le décodeur l'extrait peut-être déjà — vérifier `rtcm_decoder.py` autour de `SatObs`. Sinon le récupérer dans les éphémérides 1020.
3. Écrire `build_vrs_rtcm_1012` sur le modèle de `build_vrs_rtcm_1002` :
   - En-tête 65 bits (un bit GLONASS-Smoothing-Indicator de plus que 1002).
   - 130 bits par satellite (similaire à 1004 mais avec FCN 5 bits encodé en plus).
   - Réutiliser `_to_signed_bits` et `_encode_lock_time_7b`.

Sans 1012, le UM980 reçoit moins de satellites → ambiguïtés plus longues à fixer, voire FLOAT permanent dans des environnements urbains.

### Étape 5 — Alignement TOW (non faite)

Aujourd'hui `_compute_epoch` prend les observations brutes telles que stockées par `ObservationStore` et passe le `time.time()` courant dans le 1002 généré. Conséquences :

- Les balises Centipede n'émettent pas de manière synchronisée — un epoch à 1 Hz contient des observations de balises voisines à des instants différents (jusqu'à 500 ms d'écart).
- Le TOW inscrit dans le 1002 ne correspond pas exactement aux observations interpolées, ce qui peut introduire une erreur résiduelle de quelques cm à quelques dm.

Fix idéal :

1. Snapshotter `epochs` à un TOW cible (ex. dernier multiple de 1 s).
2. Pour chaque observation, propager la mesure du TOW source au TOW cible en utilisant la dérive d'horloge et le taux Doppler.
3. Inscrire ce TOW cible dans le 1002.

Implique de tracker le Doppler par satellite (DF018 dans le 1004 — pas dans le 1002), donc ne se fait pas sans passer à 1004 complet.

### Étape 6 — Test sans pyrtcm

`pyrtcm` est désactivé dans `pyproject.toml` parce qu'il interférait avec la réception GNSS (voir le skill `nrtk-project`). Le mode VRS pure doit être validé sur le terrain sans le réactiver — sinon on ne peut pas distinguer un bug de synthèse d'un effet pyrtcm.

## Bascule retour : retrouver le pont direct

Pour diagnostic, si la VRS pure ne donne pas de FIX, basculer rapidement :

```yaml
vrs:
  enabled: true
  pure_mode: false   # ← bascule en hybride immédiate, FIX garanti via flux brut
```

Si l'hybride ne donne pas non plus de FIX, la cause est ailleurs (NTRIP, antenne, configuration UM980), pas dans la synthèse VRS.

## Points d'attention

### Le mode mock continue de fonctionner

Aucun forward RTCM n'a lieu en mode mock (`self._mock_sensor` est `True`). `pure_mode` est donc ignoré en mock — le moteur VRS pilote toujours l'UI directement.

### Lock time réinitialisé à chaque démarrage

Le dict `_lock_times` est initialisé à vide au démarrage de l'application. La première trame 1002 envoyée aura donc des DF013 = 0 pour tous les satellites, ce qui demande au UM980 de redémarrer ses ambiguïtés. C'est normal après un cold start — comptez ~30 s avant FIX.

### `station_id` (DF003) collision

Le `vrs.station_id` (4042 par défaut) est inscrit dans le 1005 et le 1002 synthétisés. Il ne doit pas collisionner avec un ID utilisé par une vraie balise Centipede de la même session, sinon le UM980 voit des positions contradictoires sous la même station ID et invalide les corrections.

### `_peek_rtcm_type` ne valide pas le CRC

L'inspection se fait sur les 5 premiers octets de la trame, sans vérification de CRC. C'est volontaire — il s'agit uniquement de router, pas de valider. Le décodeur `RtcmDecoder` qui suit fait la vraie validation pour son propre usage.

### Hauteur d'antenne

Inchangée par les modes VRS. Toujours vérifier qu'elle n'est pas comptée deux fois (firmware UM980 + `config.yaml`) — voir le skill `nrtk-project`.

### Effet sur l'API REST/WebSocket

Le `PositionResult` publié continue d'être celui calculé par le moteur VRS (interpolation IDW) quel que soit le mode. Les clients (dashboard HTTP, app Android) voient donc la position « VRS télémétrie », pas la position « UM980 fixée ». Si on veut exposer la position réelle du UM980 aussi, il faudrait ajouter un champ `gnss_position` au `PositionResult` ou un endpoint dédié.

## Validation attendue

Après le patch, en conditions terrain :

- **Hybride** (`pure_mode: false`) : le UM980 doit passer en FLOAT puis FIX dans la minute après le démarrage, comme en pont direct. L'UI tkinter affiche la position du UM980 ; à côté, `VRS Lat/Lon/Alt` montrent la position interpolée par le moteur — écart attendu de quelques cm/dm.
- **VRS pure** (`pure_mode: true`) : le UM980 doit également atteindre FIX, mais après un délai un peu plus long (typiquement 30 s à 1 min) car les ambiguïtés repartent de zéro à chaque démarrage. La précision en altitude peut différer si l'interpolation à plusieurs balises est meilleure (ou moins bonne) que la mono-base.
- **Logs** `files/logs/nrtk_log_*.log` : chercher la ligne « RTCM VRS construit (… bytes), N sats, max lock=Xs » à chaque époque pour vérifier que le lock time croît.

Si le UM980 reste en SinglePoint en pure :

1. Vérifier que les éphémérides sont bien forwardées : `_peek_rtcm_type` doit retourner 1019/1020 et le compteur de trames série doit augmenter.
2. Vérifier le `station_id` dans les logs UM980 — il doit être stable entre 1005 et 1002.
3. Tester le repli hybride pour isoler la cause (cf. « Bascule retour »).
4. Voir aussi la section « Diagnostic » plus bas dans le skill `nrtk-project`.

## Implémentation — pointeurs rapides

- Encodage RTCM : `_to_signed_bits`, `_encode_lock_time_7b`, `build_vrs_rtcm_1005`, `build_vrs_rtcm_1002`, `build_vrs_rtcm_1004` (deprecated wrapper) dans [files/vrs_engine.py](files/vrs_engine.py).
- Tracker lock time : `VrsEngine._lock_times`, `_update_lock_times`, `_last_epoch_time`, appel dans `_compute_epoch`.
- Routage RTCM : `EPHEMERIS_RTCM_TYPES`, `_peek_rtcm_type`, branche `pure_mode` dans `_on_rtcm` et `_on_position_result` dans [files/main.py](files/main.py).
- Config : section `vrs:` dans [files/config.yaml](files/config.yaml) avec `enabled`, `pure_mode`, `station_id`.
