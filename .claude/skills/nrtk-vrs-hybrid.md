---
name: nrtk-vrs-hybrid
description: Charge le contexte du patch hybride VRS (forward du flux RTCM brut de la première balise vers le UM980 + moteur VRS pour télémétrie seulement). À invoquer dès qu'on discute du FIX RTK, de l'option `vrs.enabled`, du forwarding de trames RTCM3 vers le port série, ou qu'on envisage d'implémenter une « vraie » VRS conforme au standard 10403.3. Documente les invariants, la raison du choix actuel, et la feuille de route pour passer à une VRS complète.
---

# Patch hybride VRS — savoir-faire interne

Référence : ce patch a été mis en place après le constat que le mode `vrs.enabled: true` empêchait le UM980 d'établir un FIX RTK (il restait en SinglePoint). La cause racine était l'encodage non-conforme des trames 1004/1005 synthétisées par `vrs_engine.py` (voir le skill `nrtk-project` pour le détail des bugs RTCM3).

## Invariants actuels

À partir du patch hybride, deux invariants tiennent en mode capteur réel :

1. **Le flux RTCM3 brut de la première balise est toujours forwardé** vers le UM980 via le port série, **indépendamment de `vrs.enabled`**. C'est ce flux qui assure le FIX RTK (mono-base classique).
2. **Le `vrs_rtcm` synthétisé par le moteur VRS n'est jamais poussé** au port série tant que son encodage n'est pas conforme. Il reste calculé et publié dans le `PositionResult` (UI tkinter, dashboard HTTP, API REST/WS) pour la télémétrie, mais n'atteint pas le récepteur.

Ces deux règles sont implémentées dans [`files/main.py`](files/main.py) — chercher les commentaires « **patch hybride** » dans `_on_rtcm` et `_on_position_result`.

## Sémantique de `vrs.enabled`

Avant le patch, `vrs.enabled` choisissait entre « calcul VRS poussé au UM980 » et « pont direct ». Après le patch, sa sémantique est plus douce :

| Valeur | Forward brut → UM980 | Moteur VRS tourne | Position affichée dans l'UI mock |
|---|---|---|---|
| `true` | ✅ | ✅ (télémétrie) | celle du moteur VRS |
| `false` | ✅ | ❌ | NMEA brut du UM980 |

En mode capteur réel, l'UI tkinter affiche dans les deux cas la position issue de la GGA renvoyée par le UM980 (`_on_real_sensor_ui_update` dans `main.py`). La position « VRS calculée » apparaît à côté, comme repère, mais ne pilote pas le FIX.

## Ce qui a été corrigé incidentellement

Pendant le patch, `build_vrs_rtcm_1005` dans [`files/vrs_engine.py`](files/vrs_engine.py) a été réécrit pour être **strictement conforme à RTCM 10403.3** :

- 152 bits (= 19 octets) au lieu de 148 — les flags DF022/DF023/DF024/DF141, DF142 et DF364 sont maintenant présents.
- ECEF X/Y/Z encodés en **complément à deux signé** sur 38 bits (l'ancien code masquait simplement les bits, ce qui corrompait les coordonnées négatives).
- Construction du payload bit-par-bit en MSB-first (`bits = (bits << n) | value`) pour éviter le décalage de 4 bits introduit par l'ancien `to_bytes(19, "big")` sur un entier de 148 bits.
- Helper `_to_signed_bits(value_m, n_bits)` réutilisable pour les futurs encodages signés (1006, 1033, etc.).

Ce 1005 corrigé n'est *pas* utilisé en routine (puisque le patch hybride forwarde le 1005 brut de Centipede), mais il est testé/utilisable et constitue la première brique d'une vraie VRS.

`build_vrs_rtcm_1004` reste **non conforme** (74 bits/satellite au lieu des 125 du standard). Le réparer demande d'ajouter la partie L2 complète (DF016 à DF020). Voir « feuille de route » ci-dessous.

## Pourquoi pas remplacer juste le 1005 par celui synthétisé au rover ?

C'était la première intuition pour faire « croire » au UM980 que la base est au rover (baseline ≈ 0 → FIX instantané). Mais c'est **incorrect** sans rééécriture des observations : si on annonce la base au rover mais qu'on envoie des observations issues d'une base réelle à 10 km, le calcul différentiel diverge — le UM980 voit des delta-pseudoranges incohérents avec la baseline annoncée et soit rejette le fix, soit converge sur une position fausse.

Une vraie VRS recalcule les pseudoranges (et les phases) comme si une base virtuelle observait depuis le rover. C'est ce que font les casters commerciaux. Tant que cette rééécriture n'est pas en place, le 1005 réel de Centipede doit rester en place pour cohérence avec les observations brutes reçues.

## Feuille de route pour une vraie VRS

Si on veut faire évoluer le patch hybride vers une vraie VRS (multi-balises, baseline théorique ≈ 0) :

1. **Réparer `build_vrs_rtcm_1004`** pour qu'il produise 125 bits/satellite avec tous les champs L2 (DF016 = code indicator, DF017 = pseudorange diff L2-L1, DF018 = phase L2 vs L1, DF019 = lock time L2, DF020 = CNR L2). À faire en s'appuyant sur le pattern bit-par-bit de `_to_signed_bits` + chaînage `bits = (bits << n) | v`.
2. **Maintenir le lock time** entre époques au lieu de remettre à 0 à chaque trame. Le UM980 a besoin de continuité pour résoudre les ambiguïtés entières — c'est ce qui fait toute la différence entre FLOAT et FIX.
3. **Forwarder les éphémérides** (1019 GPS, 1020 GLONASS, 1042 BeiDou, 1045/1046 Galileo) depuis l'une des balises Centipede. Aujourd'hui le patch hybride forwarde *tout* le stream donc ces messages passent ; si on bascule en « vraie VRS pure », il faudra les router explicitement.
4. **Encoder GLONASS** via le message 1012 (équivalent du 1004 pour GLO) — Centipede diffuse de la GLO, et le UM980 sait la consommer. Doubler le nombre de satellites disponibles améliore drastiquement la résolution d'ambiguïtés.
5. **Aligner les TOW** entre balises. Les casters Centipede n'envoient pas leurs trames de manière synchronisée — l'interpolation VRS doit fixer un TOW commun et corriger chaque observation par dérive d'horloge.
6. **Tester sans `pyrtcm`**. La bibliothèque a été désactivée parce qu'elle interférait avec la réception GNSS (voir le skill `nrtk-project`). Tant que cette interférence n'est pas comprise, ne pas la réactiver pour faire le décodage des trames Centipede en VRS pure.

Étapes 1+2 suffisent à atteindre un FLOAT côté UM980 sur la VRS synthétisée. Atteindre un FIX demande typiquement 3-5 époques continues avec lock time stable.

## Bascule retour : comment retrouver le comportement pré-patch

Si pour une raison de diagnostic on veut tester le comportement « pure VRS » (synthétisé envoyé seul, brut bloqué), il faut :

1. Dans `main.py:_on_rtcm`, restreindre le forward au cas `not vrs_enabled` (comportement historique).
2. Dans `main.py:_on_position_result`, décommenter la branche qui pousse `result.vrs_rtcm` au port série.

C'est utile uniquement pour vérifier qu'une correction d'encodage (par exemple un `build_vrs_rtcm_1004` réparé) débloque enfin un FIX en VRS pure. **Ne pas faire ça en production** tant qu'on n'a pas un encodage testé bit à bit contre le standard.

## Points d'attention

### Le mode mock continue de fonctionner

Le patch hybride ne touche au port série que si `not self._mock_sensor`. En mode mock complet, aucun forward de RTCM n'a lieu (il n'y a pas de UM980 réel), et le moteur VRS pilote l'UI comme avant.

### Le mode `vrs.enabled: false` reste utile

C'est le mode « pont RTCM direct » historique, sans calcul VRS. Plus léger côté CPU, et pratique pour valider la chaîne d'acquisition pure. Garder cette option, même si la sémantique est désormais très proche du mode hybride (la seule différence est que le moteur VRS ne tourne pas).

### Diagnostic « pas de FIX » après le patch

Si après le patch le UM980 reste en SinglePoint :

1. Vérifier le forward brut : un `logger.debug` dans `_on_rtcm` peut compter les trames forwardées. Si `0`, c'est que `base_id != self._cfg["bases"][0]["id"]` (mauvaise config) ou que `_serial_manager` est `None`.
2. Vérifier que `bases[0]` est bien atteignable (état NTRIP « connecté ✓ »). Si la balise prioritaire perd la connexion, on perd aussi le FIX — il faudrait un fallback automatique vers `bases[1]`, qui n'existe pas encore.
3. Vérifier que le UM980 reçoit bien les éphémérides. Sur un démarrage à froid, il faut une minute avant qu'il en accumule assez pour faire du RTK.
4. Vérifier que la balise prioritaire émet bien des messages d'observation (1004/1077/1012/1087) — certaines mountpoints Centipede diffusent uniquement la position 1005 et pas d'observations. Choisir une mountpoint avec un « stream » complet.

### Hauteur d'antenne

Inchangée par le patch. Toujours vérifier qu'elle n'est pas comptée deux fois (firmware UM980 + `config.yaml`) — voir le skill `nrtk-project`.

### Effet sur l'API REST/WebSocket

Le `PositionResult` publié continue d'être celui calculé par le moteur VRS. Les clients (dashboard HTTP, app Android) voient donc la position « VRS télémétrie », pas la position « UM980 fixée ». Si on veut exposer la position réelle du UM980 aussi, il faudrait ajouter un champ `gnss_position` au `PositionResult` ou un endpoint dédié.

## Validation

Après application du patch :

- `vrs.enabled: true` + `sensor.mock: false` → le UM980 doit passer en FLOAT puis FIX dans la minute après le démarrage, exactement comme en pont direct.
- L'UI tkinter affiche la position du UM980 (depuis sa GGA) ; à côté, les champs `VRS Lat/Lon/Alt` montrent la position interpolée par le moteur VRS — c'est attendu qu'il y ait quelques cm/dm d'écart entre les deux.
- Le dashboard `http://localhost:8080/` et l'API `http://localhost:8081/api/status` exposent le `PositionResult` du moteur VRS, donc avec la position « VRS télémétrie ».
- Les logs `files/logs/nrtk_log_*.log` ne doivent plus contenir de messages d'erreur RTCM côté UM980.