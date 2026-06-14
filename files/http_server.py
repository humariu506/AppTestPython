"""
http_server.py
==============
Serveur HTTP minimaliste (stdlib uniquement) exposant l'état du moteur VRS
en JSON, accompagné d'un dashboard HTML auto-rafraîchi par polling JavaScript.

Conçu comme un complément du serveur REST FastAPI (api_server.py) :
- http_server.py : zéro dépendance, dashboard humain, port par défaut 8080
- api_server.py  : API REST documentée pour intégration mobile, port 8081

Les deux peuvent tourner simultanément, ils lisent le même état partagé en
lecture seule (vrs_engine.last_result et un collecteur de statuts de bases).
"""

import json
import logging
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _result_to_dict(result) -> Optional[dict]:
    """Sérialise un PositionResult en dict JSON-compatible."""
    if result is None:
        return None
    d = asdict(result)
    # `vrs_rtcm: bytes` n'est ni utile ni sérialisable côté client : on l'enlève.
    d.pop("vrs_rtcm", None)
    return d


# ---------------------------------------------------------------------------
# Dashboard HTML (auto-contenu : pas de fichier statique externe)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8" />
<title>NRTK — Dashboard</title>
<style>
:root {
  --bg: #1A1A2E; --card: #16213E; --card2: #0F3460;
  --accent: #E94560; --text: #EAEAEA; --dim: #8892A4;
  --green: #00C853; --orange: #FF8F00; --blue: #1976D2; --red: #B71C1C;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px; font-family: -apple-system, Segoe UI, Helvetica, sans-serif;
  background: var(--bg); color: var(--text);
}
h1 { color: var(--accent); font-weight: 600; margin: 0 0 18px; font-size: 18px; }
.row { display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 14px; }
.card {
  background: var(--card); border-radius: 8px; padding: 14px 18px;
  flex: 1 1 220px; min-width: 220px;
}
.card.big { flex: 1 1 100%; }
.label { color: var(--dim); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.value { font-family: Consolas, monospace; font-size: 22px; margin-top: 4px; }
.value.small { font-size: 14px; }
.badge {
  display: inline-block; padding: 4px 10px; border-radius: 4px;
  font-weight: bold; font-size: 13px;
}
.fix-FIX    { background: var(--green);  color: white; }
.fix-FLOAT  { background: var(--orange); color: white; }
.fix-SINGLE { background: var(--blue);   color: white; }
.fix-NONE   { background: var(--red);    color: white; }
.bases { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.base {
  background: var(--card2); border-radius: 6px; padding: 10px 12px;
  border-left: 4px solid var(--red);
}
.base.connected { border-left-color: var(--green); }
.base .name { font-family: Consolas, monospace; font-size: 13px; }
.base .info { color: var(--dim); font-size: 11px; margin-top: 4px; }
footer { color: var(--dim); font-size: 11px; margin-top: 14px; text-align: right; }
</style>
</head>
<body>
<h1>⬡ NRTK Position Monitor — Centipede Network</h1>

<div class="row">
  <div class="card">
    <div class="label">Statut RTK</div>
    <div class="value"><span id="fix" class="badge fix-NONE">—</span></div>
  </div>
  <div class="card">
    <div class="label">Précision</div>
    <div class="value small" id="precision">—</div>
  </div>
  <div class="card">
    <div class="label">Satellites / Bases</div>
    <div class="value" id="sats">— / —</div>
  </div>
</div>

<div class="row">
  <div class="card"><div class="label">Latitude (°N)</div>   <div class="value" id="lat">—</div></div>
  <div class="card"><div class="label">Longitude (°E)</div>  <div class="value" id="lon">—</div></div>
  <div class="card"><div class="label">Altitude (m, RAF20)</div> <div class="value" id="alt">—</div></div>
  <div class="card"><div class="label">Géoïde N (m)</div>    <div class="value" id="geoid">—</div></div>
</div>

<div class="card big">
  <div class="label">Connexions NTRIP</div>
  <div class="bases" id="bases"></div>
</div>

<footer>Auto-rafraîchissement toutes les __REFRESH__ ms — <span id="ts">en attente…</span></footer>

<script>
const REFRESH_MS = __REFRESH__;
const $ = (id) => document.getElementById(id);

function fmt(n, d=8)  { return (n === null || n === undefined) ? "—" : Number(n).toFixed(d); }

async function tick() {
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const data = await r.json();
    const p = data.position;
    if (p) {
      const badge = $("fix");
      badge.textContent = p.fix_status;
      badge.className = "badge fix-" + p.fix_status;
      $("precision").textContent =
        (p.fix_status === "FIX")
          ? `±${(p.sigma_h*100).toFixed(1)} cm H  ±${(p.sigma_v*100).toFixed(1)} cm V`
          : `±${p.sigma_h.toFixed(3)} m H  ±${p.sigma_v.toFixed(3)} m V`;
      $("sats").textContent = `${p.n_sats_used} / ${p.n_bases_used}`;
      $("lat").textContent = fmt(p.lat, 8);
      $("lon").textContent = fmt(p.lon, 8);
      $("alt").textContent = fmt(p.alt, 3);
      $("geoid").textContent = (p.geoid_undulation && p.geoid_undulation !== 0)
        ? fmt(p.geoid_undulation, 3) : "—";
    }
    const basesContainer = $("bases");
    basesContainer.innerHTML = "";
    for (const b of (data.bases || [])) {
      const div = document.createElement("div");
      div.className = "base" + (b.connected ? " connected" : "");
      const age = (b.last_msg_age !== null && b.last_msg_age !== undefined)
        ? `${b.last_msg_age.toFixed(1)} s` : "—";
      div.innerHTML = `<div class="name">${b.id} — ${b.mountpoint || ""}</div>
                       <div class="info">${b.connected ? "✓ connecté" : "✗ déconnecté"} · ${b.msg_count} msg · age ${age}</div>`;
      basesContainer.appendChild(div);
    }
    $("ts").textContent = new Date().toLocaleTimeString();
  } catch (err) {
    $("ts").textContent = "erreur réseau : " + err.message;
  }
}
tick();
setInterval(tick, REFRESH_MS);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Serveur
# ---------------------------------------------------------------------------

class NrtkHttpServer:
    """
    Serveur HTTP stdlib.

    Paramètres
    ----------
    vrs_engine : VrsEngine
        Pour accéder à `last_result` à chaque requête.
    bases_status_provider : Callable[[], list[dict]]
        Retourne la liste des statuts de bases sous forme de dicts JSON-compatibles.
        Chaque dict doit contenir au minimum : id, mountpoint, connected, msg_count, last_msg_age.
    host, port : adresse d'écoute
    refresh_ms : cadence de polling injectée dans le dashboard
    """

    def __init__(self, vrs_engine, bases_status_provider: Callable[[], list],
                 host: str = "0.0.0.0", port: int = 8080,
                 refresh_ms: int = 500):
        self._vrs = vrs_engine
        self._get_bases = bases_status_provider
        self._host = host
        self._port = port
        self._refresh_ms = refresh_ms
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _build_handler(self):
        srv = self  # capture pour les closures du handler

        class Handler(BaseHTTPRequestHandler):

            def log_message(self, fmt, *args):  # silence le bruit par défaut
                logger.debug("http %s - %s", self.address_string(), fmt % args)

            def _send_json(self, payload: dict, status: int = 200):
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_html(self, body: str):
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send_html(_DASHBOARD_HTML.replace("__REFRESH__", str(srv._refresh_ms)))
                    return
                if self.path == "/api/position":
                    self._send_json({"position": _result_to_dict(srv._vrs.last_result),
                                     "timestamp": time.time()})
                    return
                if self.path == "/api/bases":
                    self._send_json({"bases": srv._get_bases(),
                                     "timestamp": time.time()})
                    return
                if self.path == "/api/status":
                    self._send_json({"position": _result_to_dict(srv._vrs.last_result),
                                     "bases": srv._get_bases(),
                                     "timestamp": time.time()})
                    return
                self.send_error(404, "Endpoint inconnu")

        return Handler

    def start(self) -> threading.Thread:
        self._httpd = ThreadingHTTPServer((self._host, self._port), self._build_handler())
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="http-server", daemon=True,
        )
        self._thread.start()
        logger.info(f"Serveur HTTP démarré sur http://{self._host}:{self._port}/")
        return self._thread

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception as e:
                logger.warning(f"Arrêt serveur HTTP : {e}")
        logger.info("Serveur HTTP arrêté")