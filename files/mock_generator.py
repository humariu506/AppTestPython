"""
mock_generator.py
=================
Génère des trames NMEA et des messages RTCM3 réalistes pour tester
le programme sans capteur physique ni connexion réseau.

Les données RTCM générées imitent les messages de type :
  - 1004 : GPS RTK Extended L1/L2
  - 1006 : Antenna Reference Position (ARP) — position de la base
  - 1033 : Receiver/Antenna descriptor

Les observables de phase sont cohérents entre les 5 bases simulées,
avec injection d'erreurs différentielles (troposphère, ionosphère)
qui seront corrigées par le moteur VRS.
"""

import math
import time
import random
import struct
import threading
from datetime import datetime, timezone

# Constantes GNSS
SPEED_OF_LIGHT = 299_792_458.0   # m/s
L1_FREQ = 1_575_420_000.0        # Hz
L1_WAVELENGTH = SPEED_OF_LIGHT / L1_FREQ  # ~0.1903 m

# Satellites GPS visibles simulés (PRN, azimuth°, elevation°)
MOCK_SATELLITES = [
    (1,  45,  55), (3,  120, 38), (6,  200, 72), (9,  310, 25),
    (11, 80,  60), (14, 155, 42), (17, 270, 35), (19, 330, 50),
    (22, 95,  68), (28, 240, 28), (31, 15,  45), (32, 190, 33),
]


# ---------------------------------------------------------------------------
# NMEA helpers
# ---------------------------------------------------------------------------

def _nmea_checksum(sentence: str) -> str:
    """Calcule le checksum XOR d'une trame NMEA (sans $ et *)."""
    cs = 0
    for c in sentence:
        cs ^= ord(c)
    return f"{cs:02X}"


def _dd_to_nmea(degrees: float, is_lat: bool) -> tuple[str, str]:
    """Convertit degrés décimaux → format NMEA dddmm.mmmm + hémisphère."""
    if is_lat:
        hemi = "N" if degrees >= 0 else "S"
        d = int(abs(degrees))
        m = (abs(degrees) - d) * 60
        return f"{d:02d}{m:09.6f}", hemi
    else:
        hemi = "E" if degrees >= 0 else "W"
        d = int(abs(degrees))
        m = (abs(degrees) - d) * 60
        return f"{d:03d}{m:09.6f}", hemi


def make_gga(lat: float, lon: float, alt: float,
             fix_quality: int = 4, num_sats: int = 11,
             hdop: float = 0.8) -> bytes:
    """
    Génère une trame NMEA GGA.
    fix_quality : 0=invalid, 1=GPS, 2=DGPS, 4=RTK fix, 5=RTK float
    La séparation géoïdale est lue depuis RAF20 si disponible.
    """
    now = datetime.now(timezone.utc)
    time_str = now.strftime("%H%M%S.%f")[:10]

    lat_str, lat_hemi = _dd_to_nmea(lat, is_lat=True)
    lon_str, lon_hemi = _dd_to_nmea(lon, is_lat=False)

    # Séparation géoïdale depuis RAF20 (ou valeur par défaut)
    geoid_sep = 47.3
    try:
        from geoid import get_geoid
        geoid = get_geoid()
        if geoid and geoid.loaded:
            n = geoid.get_undulation(lat, lon)
            if n is not None:
                geoid_sep = n
    except ImportError:
        pass

    body = (f"GPGGA,{time_str},{lat_str},{lat_hemi},{lon_str},{lon_hemi},"
            f"{fix_quality},{num_sats:02d},{hdop:.1f},{alt:.3f},M,{geoid_sep:.1f},M,,")
    cs = _nmea_checksum(body)
    return f"${body}*{cs}\r\n".encode()


def make_gsv(satellites: list) -> list[bytes]:
    """Génère les trames NMEA GSV (satellites en vue)."""
    frames = []
    total = len(satellites)
    n_msgs = math.ceil(total / 4)
    for msg_num in range(1, n_msgs + 1):
        sats = satellites[(msg_num - 1) * 4: msg_num * 4]
        sat_fields = ""
        for prn, az, el in sats:
            snr = random.randint(35, 50)
            sat_fields += f",{prn:02d},{el:02d},{az:03d},{snr:02d}"
        body = f"GPGSV,{n_msgs},{msg_num},{total:02d}{sat_fields}"
        cs = _nmea_checksum(body)
        frames.append(f"${body}*{cs}\r\n".encode())
    return frames


def make_rmc(lat: float, lon: float, speed_knots: float = 0.0) -> bytes:
    """Génère une trame NMEA RMC."""
    now = datetime.now(timezone.utc)
    time_str = now.strftime("%H%M%S.%f")[:9]
    date_str = now.strftime("%d%m%y")
    lat_str, lat_hemi = _dd_to_nmea(lat, is_lat=True)
    lon_str, lon_hemi = _dd_to_nmea(lon, is_lat=False)
    body = (f"GPRMC,{time_str},A,{lat_str},{lat_hemi},{lon_str},{lon_hemi},"
            f"{speed_knots:.2f},0.0,{date_str},,,A")
    cs = _nmea_checksum(body)
    return f"${body}*{cs}\r\n".encode()


# ---------------------------------------------------------------------------
# RTCM3 helpers
# ---------------------------------------------------------------------------

def _rtcm3_frame(msg_type: int, payload: bytes) -> bytes:
    """
    Encapsule un payload dans une trame RTCM3 complète.
    Structure : 0xD3 | 6-bit réservé + 10-bit longueur | payload | CRC-24Q
    """
    length = len(payload)
    header = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    data = header + payload
    crc = _crc24q(data)
    return data + bytes([(crc >> 16) & 0xFF, (crc >> 8) & 0xFF, crc & 0xFF])


def _crc24q(data: bytes) -> int:
    """CRC-24Q utilisé par RTCM3."""
    poly = 0x1864CFB
    crc = 0
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= poly
    return crc & 0xFFFFFF


def make_rtcm1006(base_id: int, lat: float, lon: float, alt: float) -> bytes:
    """
    RTCM Message Type 1006 — Stationary RTK Reference Station ARP.
    Transmet la position ECEF précise de la base.
    """
    # Conversion lat/lon/alt → ECEF
    ecef_x, ecef_y, ecef_z = _lla_to_ecef(lat, lon, alt)

    # Encodage bitfield (simplifié — version fonctionnelle)
    # En production : utiliser pyrtcm pour encoder proprement
    payload = bytearray(24)
    msg_type = 1006
    # Les 12 premiers bits = type de message
    payload[0] = (msg_type >> 4) & 0xFF
    payload[1] = ((msg_type & 0x0F) << 4) | ((base_id >> 8) & 0x0F)
    payload[2] = base_id & 0xFF
    # ECEF en millimètres (encodage entier 38 bits — simplifié ici)
    x_mm = int(ecef_x * 10000)
    y_mm = int(ecef_y * 10000)
    z_mm = int(ecef_z * 10000)
    struct.pack_into(">i", payload, 3, x_mm >> 6)
    struct.pack_into(">i", payload, 7, y_mm >> 6)
    struct.pack_into(">i", payload, 11, z_mm >> 6)
    return _rtcm3_frame(msg_type, bytes(payload))


def make_rtcm1004(base_id: int, base_lat: float, base_lon: float,
                  rover_lat: float, rover_lon: float,
                  rover_alt: float, noise_m: float = 0.001) -> bytes:
    """
    RTCM Message Type 1004 — GPS RTK Extended Raw Observations.
    Simule les observables de phase et pseudorange pour chaque satellite.

    Les corrections différentielles (distance base→satellite − distance rover→satellite)
    sont calculées géométriquement et bruitées pour simuler les effets
    atmosphériques résiduels que le moteur VRS devra corriger.
    """
    base_ecef = _lla_to_ecef(base_lat, base_lon, 50.0)
    rover_ecef = _lla_to_ecef(rover_lat, rover_lon, rover_alt)

    # Sélectionner les satellites au-dessus du masque d'élévation
    visible = [s for s in MOCK_SATELLITES if s[2] >= 10]

    # Construction payload simplifié
    payload = bytearray(4 + len(visible) * 16)
    msg_type = 1004
    payload[0] = (msg_type >> 4) & 0xFF
    payload[1] = ((msg_type & 0x0F) << 4) | ((base_id >> 8) & 0x0F)
    payload[2] = base_id & 0xFF
    payload[3] = len(visible)

    for i, (prn, az, el) in enumerate(visible):
        # Vecteur unitaire vers le satellite (approximé depuis az/el)
        el_r = math.radians(el)
        az_r = math.radians(az)
        sat_dir = (
            math.cos(el_r) * math.sin(az_r),
            math.cos(el_r) * math.cos(az_r),
            math.sin(el_r)
        )
        # Distance géométrique base→sat et rover→sat (pseudo, distance fixe grande)
        base_range = 20_200_000.0 + random.gauss(0, 1000)
        rover_range = base_range + sum(
            (r - b) * d for r, b, d in zip(rover_ecef, base_ecef, sat_dir)
        )
        # Correction différentielle (en cycles L1)
        diff_cycles = (base_range - rover_range) / L1_WAVELENGTH
        # Bruit atmosphérique résiduel (ce que VRS doit corriger)
        atmo_noise = random.gauss(0, noise_m / L1_WAVELENGTH)
        phase_obs = diff_cycles + atmo_noise

        # Pack PRN + phase (int32 en 1/256 cycle)
        offset = 4 + i * 16
        payload[offset] = prn & 0xFF
        phase_int = int(phase_obs * 256) & 0xFFFFFFFF
        struct.pack_into(">I", payload, offset + 1, phase_int)
        # SNR simulé
        snr = random.randint(35, 48)
        payload[offset + 5] = snr & 0xFF

    return _rtcm3_frame(msg_type, bytes(payload))


# ---------------------------------------------------------------------------
# Géodésie
# ---------------------------------------------------------------------------

def _lla_to_ecef(lat: float, lon: float, alt: float) -> tuple[float, float, float]:
    """Conversion géodésique WGS84 → ECEF (mètres)."""
    a = 6_378_137.0
    f = 1 / 298.257223563
    e2 = 2 * f - f ** 2
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    N = a / math.sqrt(1 - e2 * math.sin(lat_r) ** 2)
    x = (N + alt) * math.cos(lat_r) * math.cos(lon_r)
    y = (N + alt) * math.cos(lat_r) * math.sin(lon_r)
    z = (N * (1 - e2) + alt) * math.sin(lat_r)
    return x, y, z


# ---------------------------------------------------------------------------
# Générateurs de flux (itérateurs threading)
# ---------------------------------------------------------------------------

class MockSensor:
    """
    Simule le capteur GNSS branché en USB.
    Génère un flux de trames NMEA à cadence configurable.
    Avant fix RTK : position bruitée (qualité 1=GPS).
    Après quelques secondes : passage en RTK float (5) puis RTK fix (4).
    """

    def __init__(self, cfg: dict):
        self.lat = cfg["rover_lat"]
        self.lon = cfg["rover_lon"]
        self.alt = cfg["rover_alt"]
        self.noise = cfg["position_noise"]
        self.rate = cfg["nmea_rate"]
        self._running = False
        self._lock = threading.Lock()
        self._fix_quality = 1       # GPS seulement au départ
        self._fix_timer = 0
        self._callbacks = []        # list[callable(bytes)]

    def add_callback(self, fn):
        self._callbacks.append(fn)

    def _emit(self, data: bytes):
        for fn in self._callbacks:
            try:
                fn(data)
            except Exception:
                pass

    def _progress_fix(self):
        """Simule la progression du fix RTK avec le temps."""
        elapsed = time.time() - self._fix_timer
        if elapsed < 10:
            self._fix_quality = 1     # GPS brut
        elif elapsed < 25:
            self._fix_quality = 5     # RTK float
        else:
            self._fix_quality = 4     # RTK fix

    def start(self):
        self._running = True
        self._fix_timer = time.time()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        return t

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            self._progress_fix()

            # Bruit de position (réduit avec le fix)
            noise_factor = {1: 1.0, 5: 0.3, 4: 0.02}.get(self._fix_quality, 1.0)
            noise_deg = self.noise * noise_factor / 111_111

            lat_n = self.lat + random.gauss(0, noise_deg)
            lon_n = self.lon + random.gauss(0, noise_deg / math.cos(math.radians(self.lat)))
            alt_n = self.alt + random.gauss(0, self.noise * noise_factor * 1.5)

            num_sats = len([s for s in MOCK_SATELLITES if s[2] >= 10])
            hdop = 0.6 + random.uniform(0, 0.4) * noise_factor

            # Émission des trames NMEA
            self._emit(make_gga(lat_n, lon_n, alt_n, self._fix_quality, num_sats, hdop))
            self._emit(make_rmc(lat_n, lon_n))
            for gsv in make_gsv(MOCK_SATELLITES):
                self._emit(gsv)

            time.sleep(1.0 / self.rate)


class MockNtripBase:
    """
    Simule une balise Centipede (flux RTCM3).
    Génère des messages 1004 (observations) et 1006 (position base)
    à cadence configurable, avec dropouts aléatoires.
    """

    def __init__(self, base_cfg: dict, rover_cfg: dict, mock_cfg: dict):
        self.base_id = list(range(10)).index(0) + 1  # simplifié
        self.base_lat = base_cfg["lat"]
        self.base_lon = base_cfg["lon"]
        self.base_alt = base_cfg["alt"]
        self.base_name = base_cfg["id"]
        self.rover_lat = rover_cfg["rover_lat"]
        self.rover_lon = rover_cfg["rover_lon"]
        self.rover_alt = rover_cfg["rover_alt"]
        self.rate = mock_cfg["rtcm_rate"]
        self.dropout_prob = mock_cfg["dropout_prob"]
        self._running = False
        self._callbacks = []
        self._connected = False
        self._msg_count = 0
        self._last_msg_time = None

    def add_callback(self, fn):
        self._callbacks.append(fn)

    def _emit(self, data: bytes):
        for fn in self._callbacks:
            try:
                fn(self.base_name, data)
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def msg_count(self) -> int:
        return self._msg_count

    @property
    def last_msg_age(self) -> float | None:
        if self._last_msg_time is None:
            return None
        return time.time() - self._last_msg_time

    def start(self):
        self._running = True
        self._connected = True
        t = threading.Thread(target=self._run, name=f"mock-{self.base_name}", daemon=True)
        t.start()
        return t

    def stop(self):
        self._running = False
        self._connected = False

    def _run(self):
        tick = 0
        while self._running:
            # Simulation de dropout
            if random.random() < self.dropout_prob:
                self._connected = False
                time.sleep(random.uniform(0.5, 2.0))
                self._connected = True
                continue

            self._connected = True

            # Envoi position base (1006) toutes les 10 trames
            if tick % 10 == 0:
                msg1006 = make_rtcm1006(
                    tick % 4095,
                    self.base_lat, self.base_lon, self.base_alt
                )
                self._emit(msg1006)

            # Envoi observations (1004) à chaque tick
            msg1004 = make_rtcm1004(
                tick % 4095,
                self.base_lat, self.base_lon,
                self.rover_lat, self.rover_lon, self.rover_alt
            )
            self._emit(msg1004)

            self._msg_count += 1
            self._last_msg_time = time.time()
            tick += 1
            time.sleep(1.0 / self.rate)
