"""
ntrip_client.py
===============
Client NTRIP v1/v2 pour se connecter aux balises Centipede.
Chaque instance tourne dans son propre thread et livre les données
RTCM3 reçues via un callback.

Protocole NTRIP :
  - Connexion HTTP GET vers le caster
  - Le caster répond ICY 200 OK (NTRIP v1) ou HTTP/1.1 200 OK (v2)
  - Le flux entrant est du RTCM3 binaire continu
  - On envoie périodiquement la position GGA du rover (NMEA) pour
    que le caster sache quelle correction envoyer (requis par certains
    mountpoints VRS côté serveur)
"""

import base64
import socket
import threading
import time
import logging

logger = logging.getLogger(__name__)


class NtripClient:
    """
    Client NTRIP pour une balise.

    Paramètres
    ----------
    base_cfg : dict
        id, host, port, mountpoint, lat, lon, alt
    credentials : dict
        user, password, version
    rtcm_callback : callable(base_id: str, data: bytes)
        Appelé à chaque chunk RTCM3 reçu
    gga_provider : callable() → bytes | None
        Retourne la dernière trame GGA du rover (position approx.)
    """

    RECONNECT_DELAY = 5.0       # secondes entre tentatives
    GGA_SEND_INTERVAL = 5.0     # secondes entre envois de position GGA
    RECV_BUFFER = 4096

    def __init__(self, base_cfg: dict, credentials: dict,
                 rtcm_callback, gga_provider=None):
        self.base_id = base_cfg["id"]
        self.host = base_cfg["host"]
        self.port = base_cfg["port"]
        self.mountpoint = base_cfg["mountpoint"]
        self.user = credentials.get("user", "centipede")
        self.password = credentials.get("password", "centipede")
        self.ntrip_version = credentials.get("version", 2)

        self._rtcm_callback = rtcm_callback
        self._gga_provider = gga_provider

        self._running = False
        self._connected = False
        self._socket = None
        self._thread = None
        self._lock = threading.Lock()

        self._msg_count = 0
        self._byte_count = 0
        self._last_msg_time = None
        self._last_error = None

    # ------------------------------------------------------------------
    # Propriétés de statut (thread-safe)
    # ------------------------------------------------------------------

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

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def start(self) -> threading.Thread:
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"ntrip-{self.base_id}",
            daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self):
        self._running = False
        self._connected = False
        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None

    # ------------------------------------------------------------------
    # Boucle principale avec reconnexion automatique
    # ------------------------------------------------------------------

    def _run_loop(self):
        while self._running:
            try:
                self._connect_and_stream()
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                logger.warning(f"[{self.base_id}] Erreur : {e} — reconnexion dans {self.RECONNECT_DELAY}s")
                time.sleep(self.RECONNECT_DELAY)

    def _connect_and_stream(self):
        """Établit la connexion NTRIP et lit le flux RTCM3."""
        logger.info(f"[{self.base_id}] Connexion à {self.host}:{self.port}/{self.mountpoint}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect((self.host, self.port))

        with self._lock:
            self._socket = sock

        # Authentification Basic
        creds = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()

        if self.ntrip_version == 2:
            request = (
                f"GET /{self.mountpoint} HTTP/1.1\r\n"
                f"Host: {self.host}:{self.port}\r\n"
                f"Ntrip-Version: Ntrip/2.0\r\n"
                f"User-Agent: NGSRTKTEST/1.0\r\n"
                f"Authorization: Basic {creds}\r\n"
                f"Ntrip-GGA: {self._get_gga_str()}\r\n"
                f"Connection: keep-alive\r\n\r\n"
            )
        else:
            request = (
                f"GET /{self.mountpoint} HTTP/1.0\r\n"
                f"User-Agent: NGSRTKTEST/1.0\r\n"
                f"Authorization: Basic {creds}\r\n\r\n"
            )

        sock.sendall(request.encode())

        # Lecture de la réponse HTTP
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(256)
            if not chunk:
                raise ConnectionError("Connexion fermée avant réponse HTTP")
            response += chunk

        header = response.split(b"\r\n\r\n")[0].decode(errors="replace")
        logger.debug(f"[{self.base_id}] Réponse : {header.splitlines()[0]}")

        if "200" not in header and "ICY 200" not in header:
            raise ConnectionError(f"Caster refusé : {header.splitlines()[0]}")

        self._connected = True
        self._last_error = None
        logger.info(f"[{self.base_id}] Connecté ✓")

        # Flux RTCM3 en continu
        sock.settimeout(15.0)
        last_gga_send = time.time()
        buffer = response.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in response else b""

        while self._running:
            try:
                chunk = sock.recv(self.RECV_BUFFER)
            except socket.timeout:
                raise TimeoutError("Timeout lecture RTCM — flux coupé")

            if not chunk:
                raise ConnectionError("Flux RTCM fermé par le serveur")

            buffer += chunk

            # Extraction des trames RTCM3 complètes du buffer
            frames, buffer = self._extract_rtcm_frames(buffer)
            for frame in frames:
                self._rtcm_callback(self.base_id, frame)
                self._msg_count += 1
                self._byte_count += len(frame)
                self._last_msg_time = time.time()

            # Envoi GGA périodique (indique notre position au caster)
            if time.time() - last_gga_send >= self.GGA_SEND_INTERVAL:
                gga_str = self._get_gga_str()
                if gga_str:
                    try:
                        sock.sendall((gga_str + "\r\n").encode())
                    except OSError:
                        pass
                last_gga_send = time.time()

    @staticmethod
    def _extract_rtcm_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
        """
        Extrait les trames RTCM3 complètes d'un buffer binaire.
        Une trame RTCM3 commence toujours par 0xD3.
        Structure : 0xD3 | 2 octets longueur (10 bits) | payload | 3 octets CRC
        """
        frames = []
        while len(buffer) >= 3:
            # Chercher le préambule RTCM3
            start = buffer.find(b"\xD3")
            if start == -1:
                buffer = b""
                break
            if start > 0:
                buffer = buffer[start:]

            if len(buffer) < 3:
                break

            # Longueur du payload (10 bits dans les 6 premiers bits du 2e octet)
            payload_len = ((buffer[1] & 0x03) << 8) | buffer[2]
            frame_len = 3 + payload_len + 3  # header + payload + CRC

            if len(buffer) < frame_len:
                break  # trame incomplète, attendre plus de données

            frame = buffer[:frame_len]
            buffer = buffer[frame_len:]
            frames.append(frame)

        return frames, buffer

    def _get_gga_str(self) -> str:
        """Récupère la trame GGA courante du rover (sans \r\n)."""
        if self._gga_provider:
            gga = self._gga_provider()
            if gga:
                return gga.decode(errors="replace").strip()
        # GGA par défaut (Paris) si pas de position connue
        return "$GPGGA,120000.00,4851.8350,N,00222.3200,E,1,08,1.0,42.0,M,47.3,M,,*5F"
