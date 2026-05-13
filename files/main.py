"""
main.py
=======
Point d'entrée du programme de test NRTK.

Orchestration :
  - Charge la configuration (config.yaml)
  - Démarre le générateur mock (ou le capteur USB réel)
  - Démarre 5 clients NTRIP mock (ou réels)
  - Instancie le décodeur RTCM et l'ObservationStore
  - Démarre le moteur VRS dans son thread
  - Lance l'interface tkinter (bloque jusqu'à fermeture)
  - Arrête proprement tous les threads à la sortie

Usage :
    python main.py                        # mode mock (config.yaml : sensor.mock=true)
    python main.py --config mon_cfg.yaml  # config personnalisée
    python main.py --real                 # force le mode capteur réel
    python main.py --no-ui               # mode console (pour serveur headless)
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    logger_init = logging.getLogger("main")
    logger_init.error("PyYAML module not found. Install with: pip install pyyaml")
    sys.exit(1)

from mock_generator import MockSensor, MockNtripBase
from rtcm_decoder import ObservationStore, RtcmDecoder
from vrs_engine import VrsEngine, PositionResult
from ui import NrtkUI

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Chargement de la config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    # Resolve config path relative to this script's directory for portability
    p = Path(path)
    if not p.is_absolute():
        # If a relative path is provided, resolve it against the script's directory
        script_dir = Path(__file__).resolve().parent
        p = (script_dir / p).resolve()
    if not p.exists():
        logger.error(f"Fichier de config introuvable : {p}")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # Résolution des chemins RTKLIB relatifs au fichier de configuration
    base_dir = p.parent
    rtklib_cfg = cfg.get("rtklib")
    if isinstance(rtklib_cfg, dict):
        for key in ("rtkrcv_path", "rtkpost_path"):
            val = rtklib_cfg.get(key)
            if isinstance(val, str) and val:
                expanded = os.path.expanduser(val)
                if not os.path.isabs(expanded):
                    rtklib_cfg[key] = str((base_dir / expanded).resolve())
        cfg["rtklib"] = rtklib_cfg

    logger.info(f"Config chargée : {p}")
    return cfg


# ---------------------------------------------------------------------------
# Composants réels (capteur USB + NTRIP)
# ---------------------------------------------------------------------------

def start_real_sensor(cfg: dict, nmea_callback, gga_update_callback) -> threading.Thread:
    """Lance la lecture série du capteur GNSS USB."""
    try:
        import serial
    except ImportError:
        logger.error("pyserial non installé. Lancez : pip install pyserial")
        sys.exit(1)

    ports = []
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
    except Exception:
        ports = []

    connected_event = threading.Event()
    sensor_cfg = cfg["sensor"]
    port    = sensor_cfg["port"]
    baud    = sensor_cfg["baudrate"]
    timeout = sensor_cfg.get("timeout", 2.0)

    def _read_loop():
        logger.info(f"Ouverture port série {port} @ {baud} baud")
        if ports:
            if port not in ports:
                logger.warning(
                    f"Port configuré {port} non détecté. Ports disponibles : {ports}"
                )
            else:
                logger.info(f"Port {port} détecté sur le système")

        try:
            ser = serial.Serial(port, baud, timeout=timeout)
            logger.info(f"Rover connecté sur {port}")
            connected_event.set()
        except serial.SerialException as e:
            logger.error(f"Impossible d'ouvrir {port} : {e}")
            return

        buffer = b""
        last_gga = [None]

        while True:
            try:
                chunk = ser.read(256)
            except serial.SerialException as e:
                logger.error(f"Erreur lecture série : {e}")
                time.sleep(1)
                continue

            buffer += chunk
            while b"\r\n" in buffer:
                line, buffer = buffer.split(b"\r\n", 1)
                line = line + b"\r\n"
                nmea_callback(line)

                if line.startswith(b"$GPGGA") or line.startswith(b"$GNGGA"):
                    last_gga[0] = line
                    _parse_and_update_gga(line, gga_update_callback)

    t = threading.Thread(target=_read_loop, name="sensor-usb", daemon=True)
    t.start()
    return t, connected_event


def _parse_and_update_gga(gga_line: bytes, callback):
    """Parse rapidement une trame GGA et extrait lat/lon/alt."""
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
        alt = float(parts[9]) if parts[9] else 0.0

        if lat != 0.0 or lon != 0.0:
            callback(lat, lon, alt)
    except Exception:
        pass


def start_real_ntrip(base_cfg: dict, credentials: dict,
                     rtcm_callback, gga_provider):
    """Lance un client NTRIP réel pour une balise."""
    from ntrip_client import NtripClient
    client = NtripClient(base_cfg, credentials, rtcm_callback, gga_provider)
    thread = client.start()
    return thread, client


# ---------------------------------------------------------------------------
# Application principale
# ---------------------------------------------------------------------------

class NrtkApp:
    """
    Orchestre tous les composants.

    Mode mock  : MockSensor + MockNtripBase × 5
    Mode réel  : serial port + NtripClient × 5
    """

    def __init__(self, cfg: dict, force_real: bool = False, no_ui: bool = False):
        self._cfg      = cfg
        self._no_ui    = no_ui
        self._use_mock = cfg["sensor"].get("mock", True) and not force_real

        # Shared state
        self._store   = ObservationStore(max_age=5.0)
        self._decoder = RtcmDecoder(self._store)

        # Position GGA courante du rover (protégée par lock)
        self._gga_lock    = threading.Lock()
        self._current_gga: Optional[bytes] = None

        # Threads actifs
        self._threads: list[threading.Thread] = []
        self._ntrip_clients = []
        self._sensor_connected_event = threading.Event()

        # UI (initialisée avant le démarrage des threads pour le log)
        base_ids = [b["id"] for b in cfg["bases"]]
        if not no_ui:
            self._ui = NrtkUI(
                base_ids=base_ids,
                update_rate_ms=cfg.get("ui", {}).get("update_rate_ms", 500),
            )
        else:
            self._ui = None

        # Moteur VRS
        self._vrs = VrsEngine(
            store=self._store,
            cfg=cfg,
            result_callback=self._on_position_result,
        )

        # Objets mock (pour accès au statut)
        self._mock_bases: list[MockNtripBase] = []

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_rtcm(self, base_id: str, frame: bytes):
        """Appelé à chaque trame RTCM reçue d'une base."""
        self._decoder.decode(base_id, frame)

        # Mise à jour statut UI
        if self._ui:
            # Chercher l'objet mock correspondant pour le statut
            for mb in self._mock_bases:
                if mb.base_name == base_id:
                    self._ui.update_base_status(
                        base_id,
                        connected=mb.connected,
                        msg_count=mb.msg_count,
                        last_msg_age=mb.last_msg_age,
                    )
                    break
            else:
                # Pas de mock : statut des clients NTRIP réels
                for client in self._ntrip_clients:
                    if client.base_id == base_id:
                        self._ui.update_base_status(
                            base_id,
                            connected=client.connected,
                            msg_count=client.msg_count,
                            last_msg_age=client.last_msg_age,
                        )
                        break

    def _on_nmea(self, line: bytes):
        """Appelé à chaque trame NMEA du capteur."""
        with self._gga_lock:
            if line.startswith(b"$GPGGA") or line.startswith(b"$GNGGA"):
                self._current_gga = line

    def _get_gga(self) -> Optional[bytes]:
        with self._gga_lock:
            return self._current_gga

    def _on_gga_position(self, lat: float, lon: float, alt: float):
        """Mise à jour position rover depuis NMEA GGA (pour l'interpolation VRS)."""
        self._vrs.update_rover_approx(lat, lon, alt)

    def _on_position_result(self, result: PositionResult):
        """Reçoit chaque résultat du moteur VRS."""
        if self._ui:
            self._ui.update_position(result)

        # Log console
        ts  = time.strftime("%H:%M:%S")
        msg = (f"[{result.fix_status:6s}] "
               f"Lat={result.lat:+.8f}  Lon={result.lon:+.8f}  "
               f"Alt={result.alt:+.3f}m  "
               f"σH={result.sigma_h:.3f}m  "
               f"Bases={result.n_bases_used}  Sats={result.n_sats_used}")

        logger.info(msg)

        if self._ui:
            level_map = {
                "FIX": "fix", "FLOAT": "float",
                "SINGLE": "warn", "NONE": "error"
            }
            self._ui.log(msg, level_map.get(result.fix_status, "info"))

    # ------------------------------------------------------------------
    # Démarrage
    # ------------------------------------------------------------------

    def start(self):
        mode = "MOCK" if self._use_mock else "RÉEL"
        logger.info(f"═══ Démarrage NRTK — mode {mode} ═══")

        if self._use_mock:
            self._start_mock()
        else:
            self._start_real()

        # Démarrage moteur VRS
        t_vrs = self._vrs.start()
        self._threads.append(t_vrs)
        logger.info("Moteur VRS démarré")

        # Statut de démarrage
        if self._ui:
            self._ui.log(
                f"Démarrage {'mock' if self._use_mock else 'réel'} — "
                f"{len(self._cfg['bases'])} bases NTRIP",
                "info"
            )

    def _start_mock(self):
        """Démarre capteur mock + 5 bases mock."""
        mock_cfg = self._cfg["mock"]

        # Capteur mock
        sensor = MockSensor(mock_cfg)
        sensor.add_callback(self._on_nmea)

        # Mise à jour position rover depuis mock directement
        def _mock_gga_update(data: bytes):
            if data.startswith(b"$GPGGA"):
                _parse_and_update_gga(data, self._on_gga_position)
        sensor.add_callback(_mock_gga_update)

        t_sensor = sensor.start()
        self._threads.append(t_sensor)
        logger.info("Capteur mock démarré")

        # 5 bases mock
        for base_cfg in self._cfg["bases"]:
            mb = MockNtripBase(
                base_cfg=base_cfg,
                rover_cfg=mock_cfg,
                mock_cfg=mock_cfg,
            )
            mb.add_callback(self._on_rtcm)
            t = mb.start()
            self._mock_bases.append(mb)
            self._threads.append(t)
            logger.info(f"Base mock démarrée : {base_cfg['id']}")

    def _start_real(self):
        """Démarre capteur USB réel + 5 clients NTRIP réels."""
        # Capteur USB
        t_sensor, sensor_connected = start_real_sensor(
            cfg=self._cfg,
            nmea_callback=self._on_nmea,
            gga_update_callback=self._on_gga_position,
        )
        self._threads.append(t_sensor)
        self._sensor_connected_event = sensor_connected
        logger.info(f"Capteur USB démarré : {self._cfg['sensor']['port']}")

        # Vérifie la détection du rover après quelques secondes
        def _check_sensor():
            if self._sensor_connected_event.is_set():
                logger.info(f"Rover détecté sur {self._cfg['sensor']['port']}")
            else:
                logger.warning(
                    f"Rover non détecté sur {self._cfg['sensor']['port']} après démarrage. "
                    "Vérifiez le câble, le port COM et les paramètres du capteur."
                )
        threading.Timer(5.0, _check_sensor).start()

        # 5 clients NTRIP
        credentials = self._cfg["ntrip"]
        for base_cfg in self._cfg["bases"]:
            t, client = start_real_ntrip(
                base_cfg=base_cfg,
                credentials=credentials,
                rtcm_callback=self._on_rtcm,
                gga_provider=self._get_gga,
            )
            self._threads.append(t)
            self._ntrip_clients.append(client)
            logger.info(f"NTRIP démarré : {base_cfg['id']} → "
                        f"{base_cfg['host']}:{base_cfg['port']}/{base_cfg['mountpoint']}")

        threading.Timer(8.0, self._log_ntrip_status).start()

    def _log_ntrip_status(self):
        if not self._ntrip_clients:
            return
        for client in self._ntrip_clients:
            status = "connecté" if client.connected else "non connecté"
            detail = f" ({client.last_error})" if client.last_error else ""
            logger.info(f"NTRIP statut {client.base_id} : {status}{detail}")

    # ------------------------------------------------------------------
    # Arrêt propre
    # ------------------------------------------------------------------

    def stop(self):
        logger.info("Arrêt des composants…")
        self._vrs.stop()
        for mb in self._mock_bases:
            mb.stop()
        for client in self._ntrip_clients:
            try:
                client.stop()
            except Exception:
                pass
        logger.info("Arrêt terminé.")

    # ------------------------------------------------------------------
    # Mode console (sans UI)
    # ------------------------------------------------------------------

    def run_headless(self):
        """Boucle infinie en mode console (pas d'interface tkinter)."""
        logger.info("Mode headless — Ctrl+C pour quitter")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Programme de test NRTK — réseau Centipede 5 balises"
    )
    parser.add_argument(
        "--config", default=str(Path(__file__).resolve().parent / "config.yaml"),
        help="Chemin vers le fichier de configuration YAML (par défaut: config.yaml dans le répertoire du script)"
    )
    parser.add_argument(
        "--real", action="store_true",
        help="Forcer le mode capteur réel (ignore sensor.mock dans la config)"
    )
    parser.add_argument(
        "--no-ui", action="store_true",
        help="Mode headless sans interface graphique (logs console uniquement)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Niveau de verbosité des logs"
    )
    args = parser.parse_args()

    # Niveau de log
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Chargement config
    cfg = load_config(args.config)

    # Vérification dépendances
    _check_dependencies()

    # Application
    app = NrtkApp(cfg=cfg, force_real=args.real, no_ui=args.no_ui)

    # Gestion Ctrl+C
    def _signal_handler(sig, frame):
        logger.info("Signal d'arrêt reçu")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Démarrage
    app.start()

    if args.no_ui:
        app.run_headless()
    else:
        # Lance l'UI (bloquant jusqu'à fermeture de la fenêtre)
        try:
            app._ui.run()
        finally:
            app.stop()


def _check_dependencies():
    """Vérifie les dépendances Python et affiche des avertissements."""
    deps = {
        "yaml":   ("pyyaml",    "Obligatoire pour la config"),
        "numpy":  ("numpy",     "Obligatoire pour le calcul VRS"),
        "serial": ("pyserial",  "Requis uniquement en mode capteur réel"),
        "pyrtcm": ("pyrtcm",    "Optionnel — décodage RTCM amélioré"),
    }
    for module, (pkg, note) in deps.items():
        try:
            __import__(module)
        except ImportError:
            if module in ("yaml", "numpy"):
                logger.error(f"[MANQUANT] {pkg} — {note}")
                logger.error(f"  → pip install {pkg}")
                sys.exit(1)
            else:
                logger.warning(f"[OPTIONNEL] {pkg} absent — {note}")


if __name__ == "__main__":
    main()
