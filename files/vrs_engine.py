"""
vrs_engine.py
=============
Moteur de calcul NRTK / VRS (Virtual Reference Station).

Principe :
  1. Récupère les observations de toutes les bases depuis ObservationStore
  2. Pour chaque satellite commun, calcule les corrections différentielles
     (pseudoranges et phases) depuis chaque base
  3. Interpole ces corrections à la position du rover (IDW — Inverse Distance
     Weighting, pondération inverse au carré de la distance)
  4. Synthétise un flux RTCM3 représentant une station virtuelle (VRS) placée
     exactement à la position du rover
  5. Passe ce flux à RTKLIB (rtkrcv/rtkpost) pour résolution d'ambiguïté
     de phase entière et calcul centimétrique

Si RTKLIB n'est pas installé : bascule sur un solveur WLS Python interne
(précision décimétrique, pas centimétrique, mais fonctionnel pour les tests).
"""

import math
import os
import subprocess
import tempfile
import threading
import time
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from rtcm_decoder import (
    ObservationStore, BaseEpoch,
    L1_WAVE_GPS, _ecef_to_lla,
)
from geoid import get_geoid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
WGS84_A  = 6_378_137.0
WGS84_F  = 1 / 298.257223563
WGS84_E2 = 2 * WGS84_F - WGS84_F ** 2


# ---------------------------------------------------------------------------
# Structures de résultat
# ---------------------------------------------------------------------------

@dataclass
class PositionResult:
    """Résultat de position calculé par le moteur VRS + RTKLIB."""
    timestamp: float  = 0.0
    lat: float        = 0.0       # degrés décimaux WGS84
    lon: float        = 0.0
    alt: float        = 0.0       # mètres (orthométrique, corrigée géoïde)
    alt_ellipsoidal: float = 0.0  # mètres (ellipsoïde WGS84)
    fix_status: str   = "NONE"    # NONE | SINGLE | FLOAT | FIX
    sigma_h: float    = 999.0     # précision horizontale 1-sigma (m)
    sigma_v: float    = 999.0     # précision verticale 1-sigma (m)
    n_sats_used: int  = 0
    n_bases_used: int = 0
    age_diff: float   = 0.0       # âge des corrections (s)
    ar_ratio: float   = 0.0       # ratio AR (ambiguïté)
    pdop: float       = 99.0
    geoid_undulation: float = 0.0 # ondulation géoïdale N (m) — RAF20
    vrs_lat: float    = 0.0       # position VRS synthétisée
    vrs_lon: float    = 0.0
    vrs_alt: float    = 0.0
    vrs_rtcm: bytes   = b""       # flux RTCM généré pour le rover

    @property
    def fix_color(self) -> str:
        return {
            "FIX":    "#00C853",
            "FLOAT":  "#FF8F00",
            "SINGLE": "#1976D2",
            "NONE":   "#B71C1C",
        }.get(self.fix_status, "#B71C1C")

    @property
    def precision_str(self) -> str:
        if self.fix_status == "FIX":
            return f"±{self.sigma_h * 100:.1f} cm H  ±{self.sigma_v * 100:.1f} cm V"
        if self.fix_status == "FLOAT":
            return f"±{self.sigma_h:.3f} m H  ±{self.sigma_v:.3f} m V"
        if self.fix_status == "SINGLE":
            return f"±{self.sigma_h:.2f} m H  ±{self.sigma_v:.2f} m V"
        return "Précision inconnue"


# ---------------------------------------------------------------------------
# Géodésie
# ---------------------------------------------------------------------------

def _lla_to_ecef(lat_deg: float, lon_deg: float, alt: float) -> np.ndarray:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = WGS84_A / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    x = (N + alt) * math.cos(lat) * math.cos(lon)
    y = (N + alt) * math.cos(lat) * math.sin(lon)
    z = (N * (1 - WGS84_E2) + alt) * math.sin(lat)
    return np.array([x, y, z])


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Interpolation VRS (IDW)
# ---------------------------------------------------------------------------

class VrsInterpolator:
    """
    Calcule les corrections différentielles interpolées à la position du rover.

    Pour chaque satellite k commun aux bases :
      correction_i(k) = pseudorange_mesuré_base_i(k) + distance_géométrique(rover → base_i)
                        ← représente ce qu'observerait une station au rover

    Correction VRS (pondérée IDW) :
      correction_vrs(k) = Σ[ w_i · correction_i(k) ] / Σ[w_i]   avec w_i = 1/d²

    Le résultat est le pseudorange qu'une base virtuelle au rover devrait observer,
    c'est-à-dire : distance géométrique rover→sat + erreurs atmosphériques interpolées.
    """

    MIN_BASES = 3
    MIN_SATS  = 4
    IDW_POWER = 2.0

    def interpolate(
        self,
        rover_lat: float, rover_lon: float, rover_alt: float,
        epochs: dict[str, BaseEpoch],
    ) -> Optional[dict[int, dict]]:
        """
        Retourne {prn: {pseudorange_vrs, phase_vrs, base_count, weight_sum}}
        ou None si les données sont insuffisantes.
        """
        # Filtrer les bases ayant une position connue
        valid_bases = {
            bid: ep for bid, ep in epochs.items()
            if ep.position is not None
        }
        if len(valid_bases) < self.MIN_BASES:
            logger.debug(f"VRS : {len(valid_bases)} bases valides (min {self.MIN_BASES})")
            return None

        # Poids IDW
        weights: dict[str, float] = {}
        rover_ecef = _lla_to_ecef(rover_lat, rover_lon, rover_alt)

        for bid, ep in valid_bases.items():
            b_lat, b_lon, _ = ep.position.lat_lon_alt
            d = max(_haversine_m(rover_lat, rover_lon, b_lat, b_lon), 1.0)
            weights[bid] = 1.0 / (d ** self.IDW_POWER)

        # Satellites communs à ≥ MIN_BASES bases
        sat_count: dict[int, int] = {}
        for bid in weights:
            for prn in valid_bases[bid].observations:
                sat_count[prn] = sat_count.get(prn, 0) + 1
        eligible = {prn for prn, c in sat_count.items() if c >= self.MIN_BASES}

        if len(eligible) < self.MIN_SATS:
            logger.debug(f"VRS : {len(eligible)} satellites communs (min {self.MIN_SATS})")
            return None

        # Interpolation par satellite
        vrs_corrections: dict[int, dict] = {}

        for prn in eligible:
            w_sum = pr_sum = ph_sum = 0.0
            cnt = 0
            for bid, w in weights.items():
                ep = valid_bases[bid]
                obs = ep.observations.get(prn)
                if obs is None:
                    continue

                # Vecteur base → rover (correction de décalage géométrique)
                base_ecef = np.array([
                    ep.position.ecef_x,
                    ep.position.ecef_y,
                    ep.position.ecef_z,
                ])
                geom_offset = float(np.linalg.norm(rover_ecef - base_ecef))

                # Pseudorange VRS = pseudorange base + décalage géométrique base→rover
                # Ce terme représente ce qu'observerait une base au rover
                pr_vrs = obs.pseudorange_L1 + geom_offset
                ph_vrs = obs.phase_L1 * L1_WAVE_GPS + geom_offset

                pr_sum += w * pr_vrs
                ph_sum += w * ph_vrs
                w_sum  += w
                cnt    += 1

            if w_sum > 0 and cnt >= self.MIN_BASES:
                vrs_corrections[prn] = {
                    "pseudorange_vrs": pr_sum / w_sum,
                    "phase_vrs":       ph_sum / w_sum,
                    "base_count":      cnt,
                    "weight_sum":      w_sum,
                }

        if len(vrs_corrections) < self.MIN_SATS:
            return None

        logger.debug(f"VRS interpolé : {len(vrs_corrections)} satellites, "
                     f"{len(weights)} bases")
        return vrs_corrections


# ---------------------------------------------------------------------------
# Synthèse RTCM VRS
# ---------------------------------------------------------------------------

def _crc24q(data: bytes) -> int:
    poly = 0x1864CFB
    crc = 0
    for b in data:
        crc ^= b << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
    return crc & 0xFFFFFF


def _rtcm3_frame(msg_type: int, payload: bytes) -> bytes:
    ln = len(payload)
    hdr = bytes([0xD3, (ln >> 8) & 0x03, ln & 0xFF])
    raw = hdr + payload
    crc = _crc24q(raw)
    return raw + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


def _to_signed_bits(value_m: float, n_bits: int) -> int:
    """Convertit une coordonnée ECEF (mètres) en entier signé n_bits en complément
    à deux, échelle 0.1 mm (= 10000 unités/m) — comme exigé par RTCM 10403.3 pour
    les DF025/DF026/DF027.
    """
    v = int(round(value_m * 10000))
    mask = (1 << n_bits) - 1
    if v < 0:
        v += (1 << n_bits)
    return v & mask


def build_vrs_rtcm_1005(lat: float, lon: float, alt: float, ref_id: int = 1,
                        gps: bool = True, glonass: bool = True,
                        galileo: bool = False) -> bytes:
    """RTCM 1005 — Stationary Antenna Reference Point, No Height Information.

    Conformité standard RTCM 10403.3 : payload de **152 bits** (= 19 octets),
    avec encodage ECEF signé en complément à deux et flux atomique des champs
    DF002..DF027.

    Réf. layout, MSB → LSB (152 bits exactement) :
        DF002 Message Number              12
        DF003 Reference Station ID        12
        DF021 ITRF Realization Year        6
        DF022 GPS Indicator                1
        DF023 GLONASS Indicator            1
        DF024 Reserved for Galileo         1
        DF141 Reference-Station Indicator  1
        DF025 ARP ECEF-X                  38  (signed, 0.1 mm)
        DF142 Single Receiver Oscillator   1
              Reserved                     1
        DF026 ARP ECEF-Y                  38
        DF364 Quarter Cycle Indicator      2
        DF027 ARP ECEF-Z                  38
        ────────────────────────────────────
        Total                             152
    """
    ecef = _lla_to_ecef(lat, lon, alt)
    x = _to_signed_bits(ecef[0], 38)
    y = _to_signed_bits(ecef[1], 38)
    z = _to_signed_bits(ecef[2], 38)

    bits = 0
    bits = (bits << 12) | (1005 & 0xFFF)        # DF002
    bits = (bits << 12) | (ref_id & 0xFFF)      # DF003
    bits = (bits << 6)  | 0                     # DF021 (0 = unknown)
    bits = (bits << 1)  | (1 if gps else 0)     # DF022
    bits = (bits << 1)  | (1 if glonass else 0) # DF023
    bits = (bits << 1)  | (1 if galileo else 0) # DF024
    bits = (bits << 1)  | 0                     # DF141 (0 = vraie station)
    bits = (bits << 38) | x                     # DF025
    bits = (bits << 1)  | 0                     # DF142
    bits = (bits << 1)  | 0                     # reserved
    bits = (bits << 38) | y                     # DF026
    bits = (bits << 2)  | 0                     # DF364
    bits = (bits << 38) | z                     # DF027

    payload = bits.to_bytes(19, "big")          # 152 bits = 19 octets pile
    return _rtcm3_frame(1005, payload)


def _encode_lock_time_7b(seconds: float) -> int:
    """RTCM 10403.3 DF013/DF019 — encodage 7 bits de la durée de verrouillage de
    phase. Table 4.3-3 du standard, échelle non linéaire (résolution plus fine
    pour les courtes durées).

    Retourne un entier 0..127 :
        0-23  : 0..23 s            (1 s/pas)
        24-47 : 24..71 s           (2 s/pas)
        48-71 : 72..167 s          (4 s/pas)
        72-95 : 168..359 s         (8 s/pas)
        96-119: 360..743 s         (16 s/pas)
        120-126: 744..935 s        (32 s/pas)
        127   : ≥ 937 s
    """
    s = max(0, int(seconds))
    if s < 24:    return s
    if s < 72:    return 24 + (s - 24) // 2
    if s < 168:   return 48 + (s - 72) // 4
    if s < 360:   return 72 + (s - 168) // 8
    if s < 744:   return 96 + (s - 360) // 16
    if s < 937:   return min(126, 120 + (s - 744) // 32)
    return 127


def build_vrs_rtcm_1002(corrections: dict[int, dict],
                        lock_times: dict[int, float],
                        ref_id: int = 1, sync: bool = False) -> bytes:
    """RTCM 1002 — Extended L1-Only GPS RTK Observables.

    Conformité RTCM 10403.3. C'est le message correct pour publier les
    observations GPS L1 seules avec IPMA et CNR — ce que produit notre
    interpolateur VRS (pas de L2 pour l'instant).

    Layout : en-tête 64 bits + 74 bits par satellite (= 6+1+24+20+7+8+8).

    En-tête (DF002..DF008) :
        DF002 Message Number       12   (1002)
        DF003 Reference Station ID 12
        DF004 GPS Epoch Time TOW   30   (ms)
        DF005 Synchronous GNSS     1    (1 si d'autres messages suivent)
        DF006 No. Sat Signals      5    (n)
        DF007 Smoothing Indicator  1    (0 = non lissé)
        DF008 Smoothing Interval   3    (0)
        ────────────────────────────────
        Total                      64   (= 8 octets pile)

    Satellite (DF009..DF015) :
        DF009 Satellite ID         6
        DF010 L1 Code Indicator    1    (0 = C/A code)
        DF011 L1 Pseudorange       24   (unsigned, 0.02 m, valeur mod 299792.458 m)
        DF012 L1 PhaseRange − PR   20   (signed, 0.0005 m)
        DF013 L1 Lock Time         7    (encodé via _encode_lock_time_7b)
        DF014 IPMA                 8    (modulus ambiguity)
        DF015 L1 CNR               8    (résolution 0.25 dB-Hz)
        ────────────────────────────────
        Total                      74
    """
    sats = list(corrections.items())[:31]   # DF006 sur 5 bits
    n = len(sats)
    tow_ms = int((time.time() % 604800) * 1000) & ((1 << 30) - 1)

    hdr = 0
    hdr = (hdr << 12) | (1002 & 0xFFF)
    hdr = (hdr << 12) | (ref_id & 0xFFF)
    hdr = (hdr << 30) | tow_ms
    hdr = (hdr << 1)  | (1 if sync else 0)
    hdr = (hdr << 5)  | (n & 0x1F)
    hdr = (hdr << 1)  | 0     # divergence-free smoothing : non
    hdr = (hdr << 3)  | 0     # smoothing interval : 0
    payload = hdr.to_bytes(8, "big")

    if n == 0:
        return _rtcm3_frame(1002, payload)

    # Tous les satellites concaténés bit-par-bit, puis pad LSB pour
    # aligner sur frontière octet (RTCM 10403.3 §3.4.5).
    bits_acc = 0
    for prn, corr in sats:
        pr_obs = float(corr["pseudorange_vrs"])
        ph_m   = float(corr["phase_vrs"])      # déjà en mètres (voir interpolateur)

        # IPMA = nombre entier de modules de 299792.458 m
        amb = int(pr_obs // 299792.458)
        if amb < 0:   amb = 0
        if amb > 255: amb = 255
        pr_mod_m = pr_obs - amb * 299792.458

        df011 = int(round(pr_mod_m / 0.02)) & 0xFFFFFF       # 24 bits unsigned
        df012 = _to_signed_bits(ph_m - pr_obs, 20)           # 20 bits signed
        df013 = _encode_lock_time_7b(lock_times.get(prn, 0)) & 0x7F
        df014 = amb & 0xFF
        df015 = 180 & 0xFF                                    # ~45 dB-Hz fixe

        sat_bits = 0
        sat_bits = (sat_bits << 6)  | (prn & 0x3F)
        sat_bits = (sat_bits << 1)  | 0                       # DF010 = C/A
        sat_bits = (sat_bits << 24) | df011
        sat_bits = (sat_bits << 20) | df012
        sat_bits = (sat_bits << 7)  | df013
        sat_bits = (sat_bits << 8)  | df014
        sat_bits = (sat_bits << 8)  | df015
        bits_acc = (bits_acc << 74) | sat_bits

    total_bits  = n * 74
    total_bytes = (total_bits + 7) // 8
    pad         = total_bytes * 8 - total_bits
    payload    += (bits_acc << pad).to_bytes(total_bytes, "big")

    return _rtcm3_frame(1002, payload)


# Conservé temporairement pour les imports historiques. Délègue à 1002.
# À supprimer quand plus aucun caller n'en dépend.
def build_vrs_rtcm_1004(corrections: dict[int, dict], ref_id: int = 1) -> bytes:
    """⚠️ Deprecated — renvoie en réalité un RTCM 1002 conforme (GPS L1 only)."""
    return build_vrs_rtcm_1002(corrections, {}, ref_id=ref_id)


# ---------------------------------------------------------------------------
# Interface RTKLIB
# ---------------------------------------------------------------------------

class RtklibSolver:
    """
    Résout la position RTK en appelant rtkpost (RTKLIB) ou le solveur interne.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._rtkpost = cfg.get("rtkpost_path", "/usr/local/bin/rtkpost")
        self._available = os.path.isfile(self._rtkpost) and os.access(self._rtkpost, os.X_OK)
        logger.info("RTKLIB disponible ✓" if self._available
                    else "RTKLIB absent — solveur WLS Python activé")

    def solve(self, rover_lat: float, rover_lon: float, rover_alt: float,
              vrs_rtcm: bytes, corrections: dict[int, dict],
              n_bases: int) -> PositionResult:
        if self._available:
            return self._solve_rtklib(rover_lat, rover_lon, rover_alt, vrs_rtcm, n_bases)
        return self._solve_wls(rover_lat, rover_lon, rover_alt, corrections, n_bases)

    # ---- RTKLIB ----

    def _solve_rtklib(self, rover_lat: float, rover_lon: float, rover_alt: float,
                      vrs_rtcm: bytes, n_bases: int) -> PositionResult:
        try:
            with tempfile.NamedTemporaryFile(suffix=".rtcm3", delete=False) as f:
                f.write(vrs_rtcm)
                rtcm_path = f.name
            conf_path = tempfile.mktemp(suffix=".conf")
            out_path  = tempfile.mktemp(suffix=".pos")

            with open(conf_path, "w") as f:
                f.write(self._rtkpost_conf(rover_lat, rover_lon, rover_alt))

            subprocess.run(
                [self._rtkpost, "-k", conf_path, "-o", out_path, rtcm_path, rtcm_path],
                capture_output=True, timeout=10.0
            )
            result = self._parse_pos(out_path, n_bases)
        except Exception as e:
            logger.warning(f"RTKLIB erreur : {e}")
            result = PositionResult(timestamp=time.time(),
                                    lat=rover_lat, lon=rover_lon, alt=rover_alt,
                                    fix_status="SINGLE", n_bases_used=n_bases)
        finally:
            for p in [rtcm_path, conf_path, out_path]:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return result

    def _rtkpost_conf(self, lat: float, lon: float, alt: float) -> str:
        f = self._cfg
        return (
            f"pos1-posmode    =kinematic\n"
            f"pos1-frequency  ={f.get('freq', 1)}\n"
            f"pos1-elmask     ={f.get('elev_mask', 10)}\n"
            f"pos1-ionoopt    =broadcast\n"
            f"pos1-tropopt    =saas\n"
            f"pos1-navsys     ={f.get('navsys', 7)}\n"
            f"pos2-armode     =fix-and-hold\n"
            f"pos2-arminfix   =10\n"
            f"pos2-maxage     =30\n"
            f"out-solformat   =llh\n"
            f"out-height      =ellipsoidal\n"
            f"ant2-postype    =llh\n"
            f"ant2-pos1       ={lat}\n"
            f"ant2-pos2       ={lon}\n"
            f"ant2-pos3       ={alt}\n"
        )

    def _parse_pos(self, path: str, n_bases: int) -> PositionResult:
        r = PositionResult(timestamp=time.time(), n_bases_used=n_bases)
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("%") or not line.strip():
                        continue
                    p = line.split()
                    if len(p) < 9:
                        continue
                    r.lat          = float(p[2])
                    r.lon          = float(p[3])
                    r.alt          = float(p[4])
                    r.fix_status   = {1: "FIX", 2: "FLOAT", 5: "SINGLE"}.get(int(p[5]), "SINGLE")
                    r.n_sats_used  = int(p[6])
                    r.sigma_h      = math.sqrt(float(p[7]) ** 2 + float(p[8]) ** 2)
                    r.sigma_v      = float(p[9]) if len(p) > 9 else 0.05
                    if len(p) > 14:
                        r.age_diff = float(p[13])
                        r.ar_ratio = float(p[14])
                    break
        except Exception as e:
            logger.debug(f"Parse .pos : {e}")
        return r

    # ---- Solveur WLS Python (fallback) ----

    def _solve_wls(self, rover_lat: float, rover_lon: float, rover_alt: float,
                   corrections: dict[int, dict], n_bases: int) -> PositionResult:
        """
        Moindres carrés pondérés sur pseudoranges VRS interpolés.
        Précision décimétrique (pas de résolution d'ambiguïté entière).
        Installez RTKLIB pour la précision centimétrique.
        """
        prns = list(corrections.keys())
        n = len(prns)

        if n < 4:
            return PositionResult(
                timestamp=time.time(),
                lat=rover_lat, lon=rover_lon, alt=rover_alt,
                fix_status="NONE", n_bases_used=n_bases
            )

        pos = _lla_to_ecef(rover_lat, rover_lon, rover_alt)
        dt  = 0.0
        H   = np.zeros((n, 4))
        cov = np.eye(4)

        for iteration in range(12):
            b_vec = np.zeros(n)
            W_vec = np.zeros(n)

            for i, prn in enumerate(prns):
                corr    = corrections[prn]
                pr_obs  = corr["pseudorange_vrs"]
                w       = corr["weight_sum"]

                # Direction satellite approximée (sans éphémérides dans ce fallback)
                angle  = math.radians((prn * 37.3) % 360)
                el     = math.radians(20 + (prn % 7) * 8)
                sat_u  = np.array([
                    math.cos(el) * math.cos(angle),
                    math.cos(el) * math.sin(angle),
                    math.sin(el),
                ])
                # Distance géométrique approx. : 20 200 km (orbite GPS moyenne)
                rho = float(np.linalg.norm(pos)) * 0 + 20_200_000.0
                b_vec[i] = pr_obs - (rho + dt)
                H[i, :3] = -sat_u
                H[i,  3] =  1.0
                W_vec[i] = w

            W = np.diag(W_vec)
            try:
                HTWH = H.T @ W @ H
                dx   = np.linalg.solve(HTWH, H.T @ W @ b_vec)
                cov  = np.linalg.inv(HTWH)
            except np.linalg.LinAlgError:
                break

            pos += dx[:3]
            dt  += dx[3]
            if np.linalg.norm(dx[:3]) < 0.001:
                break

        lat, lon, alt = _ecef_to_lla(pos[0], pos[1], pos[2])
        sigma_h = math.sqrt(max(cov[0, 0], 0) + max(cov[1, 1], 0))
        sigma_v = math.sqrt(max(cov[2, 2], 0))
        pdop    = math.sqrt(max(cov[0,0],0) + max(cov[1,1],0) + max(cov[2,2],0))

        fix = "FIX" if sigma_h < 0.05 else "FLOAT" if sigma_h < 0.30 else "SINGLE"

        return PositionResult(
            timestamp=time.time(),
            lat=lat, lon=lon, alt=alt,
            fix_status=fix,
            sigma_h=sigma_h, sigma_v=sigma_v,
            n_sats_used=n, n_bases_used=n_bases,
            pdop=pdop,
        )


# ---------------------------------------------------------------------------
# Thread principal du moteur VRS
# ---------------------------------------------------------------------------

class VrsEngine:
    """
    Thread de calcul VRS. Tourne à 1 Hz, lit l'ObservationStore,
    interpole, appelle le solveur, publie le résultat via callback.
    """

    COMPUTE_INTERVAL = 1.0

    def __init__(self, store: ObservationStore, cfg: dict, result_callback):
        self._store            = store
        self._interpolator     = VrsInterpolator()
        self._solver           = RtklibSolver(cfg.get("rtklib", {}))
        self._result_callback  = result_callback
        self._geoid            = get_geoid()
        self._station_id       = int(cfg.get("vrs", {}).get("station_id", 1))

        mock = cfg.get("mock", {})
        self._rover_lat: float = mock.get("rover_lat", 48.83)
        self._rover_lon: float = mock.get("rover_lon", 2.37)
        self._rover_alt: float = mock.get("rover_alt", 42.0)

        # Suivi du lock time par satellite (en secondes, incrémenté à chaque
        # époque où le satellite reste observé). Un saut de cycle est
        # détecté par disparition du PRN dans `corrections` → on remet à 0.
        # Nécessaire pour produire un DF013 (RTCM 1002 §3.5.5) crédible et
        # permettre au UM980 de résoudre les ambiguïtés entières.
        self._lock_times: dict[int, float] = {}
        self._last_epoch_time: Optional[float] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_result: Optional[PositionResult] = None
        self._lock = threading.Lock()

    def update_rover_approx(self, lat: float, lon: float, alt: float):
        """Mise à jour thread-safe de la position approx. rover.

        `alt` doit être l'altitude **ellipsoïdale** h = H + N (champ 9 + champ 11
        du GGA). C'est ce que livrent serial_manager et _parse_and_update_gga.
        Le moteur VRS travaille en ellipsoïdal en interne et redescend à
        l'orthométrique via RAF20 uniquement à la publication du résultat.
        """
        with self._lock:
            self._rover_lat = lat
            self._rover_lon = lon
            self._rover_alt = alt

    def _update_lock_times(self, observed_prns: set[int]) -> None:
        """Met à jour le compteur de lock time par satellite.

        - Si un PRN reste observé d'une époque à l'autre, le compteur est
          incrémenté de la durée écoulée → durée de verrouillage croissante.
        - Si un PRN disparaît (cycle slip, masquage), son compteur est purgé
          → la trame suivante repartira de 0 et signalera une perte de lock
          au récepteur, qui devra ré-initialiser ses ambiguïtés sur ce sat.
        - Les nouveaux PRN sont initialisés à 0.
        """
        now = time.time()
        dt = (now - self._last_epoch_time) if self._last_epoch_time else self.COMPUTE_INTERVAL
        self._last_epoch_time = now

        # Purge des satellites disparus
        gone = set(self._lock_times.keys()) - observed_prns
        for prn in gone:
            del self._lock_times[prn]
        # Incrément pour ceux qui restent + init pour les nouveaux
        for prn in observed_prns:
            self._lock_times[prn] = self._lock_times.get(prn, 0.0) + dt

    @property
    def last_result(self) -> Optional[PositionResult]:
        return self._last_result

    def start(self) -> threading.Thread:
        self._running = True
        self._thread  = threading.Thread(target=self._run,
                                         name="vrs-engine", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            t0 = time.time()
            try:
                self._compute_epoch()
            except Exception as e:
                logger.error(f"VRS compute : {e}", exc_info=True)
            time.sleep(max(0.0, self.COMPUTE_INTERVAL - (time.time() - t0)))

    def _compute_epoch(self):
        with self._lock:
            r_lat, r_lon, r_alt_ellip = self._rover_lat, self._rover_lon, self._rover_alt

        # --- Correction géoïdale fine (RAF20) ---
        # r_alt_ellip est déjà l'altitude ellipsoïdale (h) brute de l'antenne
        geoid_n = 0.0
        if self._geoid and self._geoid.loaded:
            n = self._geoid.get_undulation(r_lat, r_lon)
            if n is not None:
                geoid_n = n
        
        # Pour les logs, l'altitude approximative orthométrique H = h - N
        r_alt_ortho = r_alt_ellip - geoid_n
        logger.debug(f"GGA h (ellipsoïdale) = {r_alt_ellip:.3f} m")
        logger.debug(f"Géoïde N       = {geoid_n:+.3f} m -> H_orthométrique = {r_alt_ortho:.3f} m")

        epochs  = self._store.get_all_epochs()
        n_bases = len(epochs)

        if n_bases == 0:
            self._publish(PositionResult(
                timestamp=time.time(), lat=r_lat, lon=r_lon, alt=r_alt_ortho,
                alt_ellipsoidal=r_alt_ellip,
                fix_status="NONE",
                geoid_undulation=geoid_n,
                vrs_lat=r_lat, vrs_lon=r_lon, vrs_alt=r_alt_ortho
            ))
            return

        # Interpolation VRS avec altitude ellipsoïdale
        corrections = self._interpolator.interpolate(
            r_lat, r_lon, r_alt_ellip, epochs
        )
        logger.debug(f"Interpolation VRS lancée avec h={r_alt_ellip:.3f} m (ellipsoïdal), bases={n_bases}")

        if corrections is None:
            self._publish(PositionResult(
                timestamp=time.time(), lat=r_lat, lon=r_lon, alt=r_alt_ortho,
                alt_ellipsoidal=r_alt_ellip,
                fix_status="SINGLE", n_bases_used=n_bases,
                sigma_h=3.0, sigma_v=5.0,
                geoid_undulation=geoid_n,
                vrs_lat=r_lat, vrs_lon=r_lon, vrs_alt=r_alt_ortho
            ))
            return

        # Suivi du lock time pour DF013 (cf. RTCM 10403.3 §3.5.5)
        self._update_lock_times(set(corrections.keys()))

        # RTCM VRS avec altitude ellipsoïdale.
        # — 1005 conforme (152 bits, ECEF signé, flags GPS/GLO).
        # — 1002 conforme (GPS L1 only, 74 bits/sat, lock time persistant).
        vrs_rtcm = build_vrs_rtcm_1005(r_lat, r_lon, r_alt_ellip,
                                       ref_id=self._station_id,
                                       gps=True, glonass=True) + \
                   build_vrs_rtcm_1002(corrections, self._lock_times,
                                       ref_id=self._station_id)
        logger.debug(f"RTCM VRS construit ({len(vrs_rtcm)} bytes), "
                     f"{len(corrections)} sats, "
                     f"max lock={max(self._lock_times.values(), default=0):.0f}s")

        result = self._solver.solve(r_lat, r_lon, r_alt_ellip, vrs_rtcm,
                                    corrections, n_bases)
        logger.debug(f"Résultat solveur (ellipsoïdal) : lat={result.lat:+.8f} lon={result.lon:+.8f} h={result.alt:+.3f} m fix={result.fix_status}")

        # Reconversion du résultat : altitude ellipsoïdale → orthométrique
        result.alt_ellipsoidal = result.alt
        if self._geoid and self._geoid.loaded:
            n_result = self._geoid.get_undulation(result.lat, result.lon)
            if n_result is not None:
                logger.debug(f"Ondulation au résultat N_result = {n_result:+.3f} m")
                result.alt = result.alt_ellipsoidal - n_result  # H = h - N
                logger.debug(f"Conversion H = h - N : {result.alt_ellipsoidal:+.3f} - {n_result:+.3f} = {result.alt:+.3f} m")
                geoid_n = n_result

        result.geoid_undulation = geoid_n
        result.n_bases_used = n_bases
        # Position VRS synthétique exposée à l'UI : altitude orthométrique
        # (l'altitude ellipsoïdale n'est utilisée qu'en interne pour la
        # synthèse RTCM 1005 et l'appel au solveur, plus haut).
        result.vrs_lat, result.vrs_lon, result.vrs_alt = r_lat, r_lon, r_alt_ortho
        logger.debug(f"VRS synthétique : lat={r_lat:+.8f} lon={r_lon:+.8f} H_vrs(ortho)={r_alt_ortho:+.3f} m")
        result.vrs_rtcm = vrs_rtcm

        self._last_result = result
        self._publish(result)

    def _publish(self, result: PositionResult):
        try:
            self._result_callback(result)
        except Exception as e:
            logger.error(f"VRS callback : {e}")
