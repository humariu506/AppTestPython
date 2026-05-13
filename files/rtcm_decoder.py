"""
rtcm_decoder.py
===============
Décode les trames RTCM3 brutes reçues de chaque balise Centipede
et extrait les observables nécessaires au calcul VRS :
  - Position ECEF précise de chaque base (msg 1005/1006)
  - Pseudoranges et phases L1/L2 par satellite (msg 1001-1004 / 1009-1012)

Les observables décodés sont stockés dans ObservationStore (thread-safe)
consulté par le moteur VRS.
"""

import math
import threading
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes physiques
# ---------------------------------------------------------------------------
SPEED_OF_LIGHT = 299_792_458.0
L1_FREQ_GPS    = 1_575_420_000.0
L2_FREQ_GPS    = 1_227_600_000.0
L1_WAVE_GPS    = SPEED_OF_LIGHT / L1_FREQ_GPS   # ~0.19029 m
L2_WAVE_GPS    = SPEED_OF_LIGHT / L2_FREQ_GPS   # ~0.24421 m

# Types de messages supportés
MSG_BASE_POS         = 1005
MSG_BASE_POS_HEIGHT  = 1006
MSG_GPS_L1L2_EXT     = 1004
MSG_GLO_L1L2_EXT     = 1012
MSG_GPS_L1_OBS       = 1001
MSG_GPS_L1_EXT       = 1002
MSG_GPS_L1L2_OBS     = 1003
MSG_GLO_L1_OBS       = 1009
MSG_GLO_L1_EXT       = 1010
MSG_GLO_L1L2_OBS     = 1011


# ---------------------------------------------------------------------------
# Structures de données
# ---------------------------------------------------------------------------

@dataclass
class SatObs:
    """Observation d'un satellite depuis une base à un instant t."""
    prn: int
    system: str                     # 'GPS' | 'GLO' | 'GAL'
    timestamp: float = 0.0
    pseudorange_L1: float = 0.0     # mètres
    pseudorange_L2: float = 0.0
    phase_L1: float = 0.0           # cycles
    phase_L2: float = 0.0
    snr_L1: float = 0.0             # dB-Hz
    snr_L2: float = 0.0
    lock_indicator: int = 0


@dataclass
class BasePosition:
    """Position ECEF précise d'une base de référence."""
    base_id: str
    ecef_x: float = 0.0
    ecef_y: float = 0.0
    ecef_z: float = 0.0
    timestamp: float = 0.0
    antenna_height: float = 0.0

    @property
    def lat_lon_alt(self) -> tuple[float, float, float]:
        return _ecef_to_lla(self.ecef_x, self.ecef_y, self.ecef_z)


@dataclass
class BaseEpoch:
    """Toutes les observations d'une base pour une époque donnée."""
    base_id: str
    timestamp: float
    position: BasePosition | None = None
    observations: dict[int, SatObs] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Store partagé (thread-safe)
# ---------------------------------------------------------------------------

class ObservationStore:
    """
    Stocke les dernières observations de chaque base.
    Thread-safe via RLock.
    """

    def __init__(self, max_age: float = 5.0):
        self._lock = threading.RLock()
        self._base_positions: dict[str, BasePosition] = {}
        self._epochs: dict[str, BaseEpoch] = {}
        self._max_age = max_age

    def update_position(self, base_id: str, pos: BasePosition):
        with self._lock:
            self._base_positions[base_id] = pos

    def update_observations(self, base_id: str, obs_list: list, ts: float):
        with self._lock:
            epoch = BaseEpoch(base_id=base_id, timestamp=ts)
            epoch.position = self._base_positions.get(base_id)
            epoch.observations = {o.prn: o for o in obs_list}
            self._epochs[base_id] = epoch

    def get_all_epochs(self) -> dict[str, BaseEpoch]:
        now = time.time()
        with self._lock:
            return {
                bid: ep for bid, ep in self._epochs.items()
                if now - ep.timestamp <= self._max_age
            }

    def get_common_satellites(self) -> set[int]:
        """PRNs visibles par au moins 3 bases simultanément."""
        epochs = self.get_all_epochs()
        if len(epochs) < 3:
            return set()
        sat_counts: dict[int, int] = defaultdict(int)
        for ep in epochs.values():
            for prn in ep.observations:
                sat_counts[prn] += 1
        return {prn for prn, count in sat_counts.items() if count >= 3}

    def base_count(self) -> int:
        with self._lock:
            return len(self._epochs)

    def positions(self) -> dict[str, BasePosition]:
        with self._lock:
            return dict(self._base_positions)


# ---------------------------------------------------------------------------
# Lecteur de bits (RTCM3 est packed en big-endian bits)
# ---------------------------------------------------------------------------

class BitReader:

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n_bits: int) -> int:
        result = 0
        for _ in range(n_bits):
            byte_idx = self._pos >> 3
            bit_idx  = 7 - (self._pos & 7)
            if byte_idx < len(self._data):
                result = (result << 1) | ((self._data[byte_idx] >> bit_idx) & 1)
            self._pos += 1
        return result

    def read_signed(self, n_bits: int) -> int:
        val = self.read(n_bits)
        if n_bits > 0 and val >= (1 << (n_bits - 1)):
            val -= (1 << n_bits)
        return val

    def skip(self, n_bits: int):
        self._pos += n_bits


# ---------------------------------------------------------------------------
# Décodeur principal
# ---------------------------------------------------------------------------

class RtcmDecoder:
    """
    Décode les trames RTCM3 et alimente l'ObservationStore.
    Utilise pyrtcm si disponible, sinon bascule sur le décodeur interne.
    """

    def __init__(self, store: ObservationStore):
        self._store = store
        self._use_pyrtcm = self._check_pyrtcm()
        mode = "pyrtcm" if self._use_pyrtcm else "décodeur interne"
        logger.info(f"RtcmDecoder initialisé — mode : {mode}")

    @staticmethod
    def _check_pyrtcm() -> bool:
        try:
            import pyrtcm  # noqa: F401
            return True
        except ImportError:
            return False

    # ---- Point d'entrée ----

    def decode(self, base_id: str, frame: bytes):
        if len(frame) < 6:
            return
        if not self._verify_crc(frame):
            logger.debug(f"[{base_id}] CRC invalide")
            return

        payload = frame[3:-3]
        if len(payload) < 2:
            return

        msg_type = (payload[0] << 4) | (payload[1] >> 4)

        if self._use_pyrtcm:
            self._decode_pyrtcm(base_id, frame, msg_type, payload)
        else:
            self._decode_internal(base_id, payload, msg_type)

    # ---- Décodage via pyrtcm ----

    def _decode_pyrtcm(self, base_id: str, frame: bytes, msg_type: int, payload: bytes):
        try:
            from pyrtcm import RTCMReader
            import io
            _, parsed = RTCMReader(io.BytesIO(frame)).read()
            if parsed is None:
                return

            if msg_type in (MSG_BASE_POS, MSG_BASE_POS_HEIGHT):
                pos = BasePosition(
                    base_id=base_id,
                    ecef_x=float(getattr(parsed, "DF025", 0)),
                    ecef_y=float(getattr(parsed, "DF027", 0)),
                    ecef_z=float(getattr(parsed, "DF029", 0)),
                    timestamp=time.time(),
                    antenna_height=float(getattr(parsed, "DF031", 0))
                                   if msg_type == MSG_BASE_POS_HEIGHT else 0.0,
                )
                self._store.update_position(base_id, pos)

            elif msg_type in (MSG_GPS_L1_OBS, MSG_GPS_L1_EXT,
                              MSG_GPS_L1L2_OBS, MSG_GPS_L1L2_EXT):
                obs_list = []
                i = 1
                while True:
                    sfx = f"_{i:02d}" if i > 1 else ""
                    prn = getattr(parsed, f"DF009{sfx}", None)
                    if prn is None:
                        break
                    obs_list.append(SatObs(
                        prn=int(prn), system="GPS", timestamp=time.time(),
                        pseudorange_L1=float(getattr(parsed, f"DF011{sfx}", 0)),
                        phase_L1=float(getattr(parsed, f"DF012{sfx}", 0)),
                        snr_L1=float(getattr(parsed, f"DF014{sfx}", 0)),
                        pseudorange_L2=float(getattr(parsed, f"DF017{sfx}", 0)),
                        phase_L2=float(getattr(parsed, f"DF018{sfx}", 0)),
                        snr_L2=float(getattr(parsed, f"DF020{sfx}", 0)),
                    ))
                    i += 1
                if obs_list:
                    self._store.update_observations(base_id, obs_list, time.time())

            elif msg_type in (MSG_GLO_L1_OBS, MSG_GLO_L1_EXT,
                              MSG_GLO_L1L2_OBS, MSG_GLO_L1L2_EXT):
                obs_list = []
                i = 1
                while True:
                    sfx = f"_{i:02d}" if i > 1 else ""
                    prn = getattr(parsed, f"DF038{sfx}", None)
                    if prn is None:
                        break
                    obs_list.append(SatObs(
                        prn=int(prn) + 64, system="GLO", timestamp=time.time(),
                        pseudorange_L1=float(getattr(parsed, f"DF040{sfx}", 0)),
                        phase_L1=float(getattr(parsed, f"DF041{sfx}", 0)),
                        snr_L1=float(getattr(parsed, f"DF043{sfx}", 0)),
                    ))
                    i += 1
                if obs_list:
                    self._store.update_observations(base_id, obs_list, time.time())

        except Exception as e:
            logger.debug(f"[{base_id}] pyrtcm msg {msg_type}: {e}")
            # Fallback sur décodeur interne
            self._decode_internal(base_id, payload, msg_type)

    # ---- Décodeur interne ----

    def _decode_internal(self, base_id: str, payload: bytes, msg_type: int):
        br = BitReader(payload)
        br.skip(12)  # type déjà lu
        try:
            if msg_type == MSG_BASE_POS:
                self._parse_1005(base_id, br)
            elif msg_type == MSG_BASE_POS_HEIGHT:
                self._parse_1006(base_id, br)
            elif msg_type == MSG_GPS_L1L2_EXT:
                self._parse_1004(base_id, br)
            elif msg_type == MSG_GLO_L1L2_EXT:
                self._parse_1012(base_id, br)
        except Exception as e:
            logger.debug(f"[{base_id}] Décodage interne msg {msg_type}: {e}")

    def _parse_1005(self, base_id: str, br: BitReader):
        br.skip(12)   # ref station id
        br.skip(6)    # ITRF + reserved
        br.skip(3)    # GPS/GLO/GAL indicator
        br.skip(1)    # ref station indicator
        ecef_x = br.read_signed(38) * 0.0001
        br.skip(2)
        ecef_y = br.read_signed(38) * 0.0001
        br.skip(2)
        ecef_z = br.read_signed(38) * 0.0001
        self._store.update_position(base_id, BasePosition(
            base_id=base_id, ecef_x=ecef_x, ecef_y=ecef_y, ecef_z=ecef_z,
            timestamp=time.time()
        ))

    def _parse_1006(self, base_id: str, br: BitReader):
        self._parse_1005(base_id, br)
        antenna_h = br.read(16) * 0.0001
        pos = self._store.positions().get(base_id)
        if pos:
            pos.antenna_height = antenna_h
            self._store.update_position(base_id, pos)

    def _parse_1004(self, base_id: str, br: BitReader):
        br.skip(12)   # ref station id
        br.skip(30)   # GPS TOW
        br.skip(1)    # sync flag
        n_sats = br.read(5)
        br.skip(4)    # smoothing

        obs_list = []
        for _ in range(n_sats):
            prn         = br.read(6)
            br.skip(1)  # code indicator L1
            pr_mod      = br.read(24)
            phase_raw   = br.read_signed(20)
            lock_L1     = br.read(7)
            amb         = br.read(8)
            snr_L1      = br.read(8) * 0.25
            br.skip(2)  # code indicator L2
            pr_diff     = br.read_signed(14)
            phase_L2_r  = br.read_signed(20)
            br.skip(7)  # lock L2
            snr_L2      = br.read(8) * 0.25

            pr_L1 = amb * 299792.458 + pr_mod * 0.02
            pr_L2 = pr_L1 + pr_diff * 0.02
            obs_list.append(SatObs(
                prn=prn if prn != 0 else 32, system="GPS",
                timestamp=time.time(),
                pseudorange_L1=pr_L1, pseudorange_L2=pr_L2,
                phase_L1=phase_raw * 0.0005,
                phase_L2=phase_L2_r * 0.0005,
                snr_L1=snr_L1, snr_L2=snr_L2,
                lock_indicator=lock_L1,
            ))

        if obs_list:
            self._store.update_observations(base_id, obs_list, time.time())

    def _parse_1012(self, base_id: str, br: BitReader):
        br.skip(12)   # ref station id
        br.skip(27)   # epoch
        br.skip(1)
        n_sats = br.read(5)
        br.skip(4)

        obs_list = []
        for _ in range(n_sats):
            sat_id   = br.read(6)
            br.skip(5)   # freq channel
            br.skip(1)   # code ind
            pr_raw   = br.read(25)
            phase_r  = br.read_signed(20)
            lock_ind = br.read(7)
            amb      = br.read(7)
            snr_L1   = br.read(8) * 0.25
            br.skip(2)
            pr_diff  = br.read_signed(14)
            ph_L2r   = br.read_signed(20)
            br.skip(7)
            snr_L2   = br.read(8) * 0.25

            pr_L1 = amb * 599584.916 + pr_raw * 0.02
            pr_L2 = pr_L1 + pr_diff * 0.02
            obs_list.append(SatObs(
                prn=sat_id + 64, system="GLO",
                timestamp=time.time(),
                pseudorange_L1=pr_L1, pseudorange_L2=pr_L2,
                phase_L1=phase_r * 0.0005,
                phase_L2=ph_L2r * 0.0005,
                snr_L1=snr_L1, snr_L2=snr_L2,
                lock_indicator=lock_ind,
            ))

        if obs_list:
            self._store.update_observations(base_id, obs_list, time.time())

    # ---- CRC-24Q ----

    @staticmethod
    def _verify_crc(frame: bytes) -> bool:
        poly = 0x1864CFB
        crc = 0
        for byte in frame[:-3]:
            crc ^= byte << 16
            for _ in range(8):
                crc <<= 1
                if crc & 0x1000000:
                    crc ^= poly
        crc &= 0xFFFFFF
        return crc == ((frame[-3] << 16) | (frame[-2] << 8) | frame[-1])


# ---------------------------------------------------------------------------
# Géodésie
# ---------------------------------------------------------------------------

def _ecef_to_lla(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Conversion ECEF → WGS84 (méthode itérative de Bowring)."""
    a   = 6_378_137.0
    f   = 1 / 298.257223563
    e2  = 2 * f - f ** 2
    lon = math.atan2(y, x)
    p   = math.sqrt(x ** 2 + y ** 2)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        lat_new = math.atan2(z + e2 * N * math.sin(lat), p)
        if abs(lat_new - lat) < 1e-12:
            lat = lat_new
            break
        lat = lat_new
    N   = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    cos_lat = math.cos(lat)
    alt = (p / cos_lat - N) if abs(cos_lat) > 1e-10 else (abs(z) / math.sin(lat) - N * (1 - e2))
    return math.degrees(lat), math.degrees(lon), alt
