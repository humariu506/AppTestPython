"""
serial_manager.py
=================
Gestionnaire série unique pour le récepteur GNSS (UM980).
Garantit qu'un seul composant accède au port série en lecture et en écriture.

Architecture multithread :
  - Thread Lecture : Lit en continu les données du port (NMEA, réponses) et déclenche les callbacks.
  - Thread Écriture : Dépile une file d'attente (Queue) et écrit les données (RTCM, commandes) sur le port.
"""

import logging
import threading
import time
import queue
from typing import Callable

logger = logging.getLogger(__name__)

class SerialManager:
    """
    Abstrait l'accès au port série (lecture/écriture) de manière thread-safe.
    """
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._port = cfg["port"]
        self._baudrate = cfg["baudrate"]
        self._timeout = cfg.get("timeout", 2.0)

        self._ser = None
        self._running = False

        self._read_thread = None
        self._write_thread = None

        self._write_queue = queue.Queue()
        self._nmea_callbacks: list[Callable[[bytes], None]] = []
        self._gga_callbacks: list[Callable[[float, float, float], None]] = []
        self._nmea_ui_callbacks: list[Callable[[float, float, float, float, int, int, float], None]] = []

        self.connected_event = threading.Event()

    def add_nmea_callback(self, cb: Callable[[bytes], None]):
        self._nmea_callbacks.append(cb)

    def add_gga_callback(self, cb: Callable[[float, float, float], None]):
        self._gga_callbacks.append(cb)

    def add_nmea_ui_callback(self, cb: Callable[[float, float, float, float, int, int, float], None]):
        self._nmea_ui_callbacks.append(cb)

    def start(self) -> bool:
        """Ouvre le port série et démarre les threads de lecture/écriture."""
        try:
            import serial
        except ImportError:
            logger.error("pyserial non installé. Lancez : pip install pyserial")
            return False

        try:
            self._ser = serial.Serial(self._port, self._baudrate, timeout=self._timeout)
            logger.info(f"SerialManager : Port {self._port} ouvert @ {self._baudrate} baud")
            self.connected_event.set()
        except serial.SerialException as e:
            logger.error(f"Impossible d'ouvrir le port {self._port} : {e}")
            return False

        self._running = True

        self._read_thread = threading.Thread(target=self._read_loop, name="serial-read", daemon=True)
        self._write_thread = threading.Thread(target=self._write_loop, name="serial-write", daemon=True)

        self._read_thread.start()
        self._write_thread.start()

        return True

    def stop(self):
        """Arrête les threads et ferme le port série."""
        self._running = False
        if self._ser:
            try:
                self._ser.cancel_read()
            except Exception:
                pass
            
        # Débloquer la queue en écriture
        self._write_queue.put(b"")

        if self._read_thread and self._read_thread.is_alive():
            self._read_thread.join(timeout=1.0)
        if self._write_thread and self._write_thread.is_alive():
            self._write_thread.join(timeout=1.0)

        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
                logger.info(f"SerialManager : Port {self._port} fermé")
            except Exception as e:
                logger.error(f"Erreur lors de la fermeture du port : {e}")

    def write_data(self, data: bytes):
        """Ajoute des données dans la file d'attente pour être écrites sur le port série."""
        if self._running and self._ser and self._ser.is_open:
            self._write_queue.put(data)

    def _read_loop(self):
        """Thread 1 : Lecture en continu depuis le port série."""
        import serial
        buffer = b""
        while self._running:
            try:
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buffer += chunk
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    line = line + b"\r\n"
                    
                    for cb in self._nmea_callbacks:
                        try:
                            cb(line)
                        except Exception as e:
                            logger.error(f"Erreur callback NMEA: {e}")

                    if line.startswith(b"$GPGGA") or line.startswith(b"$GNGGA"):
                        self._parse_and_dispatch_gga(line)
            except serial.SerialException as e:
                if self._running:
                    logger.error(f"Erreur lecture série : {e}")
                    time.sleep(1)

    def _write_loop(self):
        """Thread 3 : Dépile la file d'attente et écrit sur le port série."""
        while self._running:
            try:
                # Utiliser un timeout pour permettre la sortie de la boucle si _running passe à False
                data = self._write_queue.get(timeout=0.5)
                if data and self._ser and self._ser.is_open:
                    self._ser.write(data)
                    # self._ser.flush() # Optionnel selon le besoin de vidage immédiat
            except queue.Empty:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Erreur écriture série : {e}")
                    time.sleep(1)

    def _parse_and_dispatch_gga(self, gga_line: bytes):
        """Parse une trame GGA et appelle les callbacks GGA."""
        try:
            line = gga_line.decode(errors="replace").strip()
            parts = line.split(",")
            if len(parts) < 10:
                return

            def _nmea_to_dd(val: str, hemi: str) -> float:
                if not val:
                    return 0.0
                dot = val.index(".") - 2
                deg = float(val[:dot])
                mn  = float(val[dot:])
                dd  = deg + mn / 60
                return -dd if hemi in ("S", "W") else dd

            lat = _nmea_to_dd(parts[2], parts[3])
            lon = _nmea_to_dd(parts[4], parts[5])
            
            fix_quality = int(parts[6]) if len(parts) > 6 and parts[6] else 0
            num_sats = int(parts[7]) if len(parts) > 7 and parts[7] else 0
            hdop = float(parts[8]) if len(parts) > 8 and parts[8] else 99.9
            alt = float(parts[9]) if len(parts) > 9 and parts[9] else 0.0
            geoid_sep = float(parts[11]) if len(parts) > 11 and parts[11] else 0.0

            if lat != 0.0 or lon != 0.0:
                for cb in self._gga_callbacks:
                    try:
                        cb(lat, lon, alt + geoid_sep)
                    except Exception as e:
                        logger.error(f"Erreur callback GGA: {e}")
                
                for cb in self._nmea_ui_callbacks:
                    try:
                        cb(lat, lon, alt, geoid_sep, fix_quality, num_sats, hdop)
                    except Exception as e:
                        logger.error(f"Erreur callback NMEA UI: {e}")
        except Exception:
            pass
