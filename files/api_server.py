"""
api_server.py
=============
Serveur REST + WebSocket basé sur FastAPI, destiné à l'intégration mobile
(application Android). Tourne en parallèle du dashboard stdlib (http_server.py),
sur un port distinct, en lisant le même état partagé.

Endpoints REST
--------------
    GET  /api/health                  : ping ; retourne {"ok": true, "uptime": float}
    GET  /api/position                : dernier PositionResult en JSON
    GET  /api/bases                   : liste des statuts NTRIP
    GET  /api/status                  : position + bases (un seul appel)
    GET  /docs                        : doc OpenAPI auto-générée (FastAPI)

WebSocket
---------
    WS   /ws/position
        Le serveur pousse, à chaque nouveau résultat VRS publié par
        l'application, un message JSON identique au payload de /api/status.
        Le client n'a rien à envoyer (les messages reçus sont ignorés).

Le serveur s'exécute dans son propre thread, avec son propre event loop
asyncio. La méthode `publish_result(...)` est thread-safe : elle est appelée
depuis le thread sync du moteur VRS et schedule la diffusion vers tous les
WebSocket connectés dans l'event loop du serveur.
"""

import asyncio
import logging
import threading
import time
from dataclasses import asdict
from typing import Callable, Optional, Set

logger = logging.getLogger(__name__)

# Import différé : si fastapi/uvicorn ne sont pas installés, on ne plante pas
# l'application ; la classe se contentera de refuser de démarrer en log d'erreur.
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    FastAPI = None  # type: ignore
    WebSocket = None  # type: ignore


def _result_to_dict(result) -> Optional[dict]:
    """Sérialise un PositionResult en dict JSON-compatible."""
    if result is None:
        return None
    d = asdict(result)
    d.pop("vrs_rtcm", None)
    return d


class NrtkApiServer:
    """
    Serveur FastAPI REST + WebSocket.

    Paramètres
    ----------
    vrs_engine : VrsEngine
        Source du `last_result`.
    bases_status_provider : Callable[[], list[dict]]
        Retourne la liste des statuts des bases sous forme de dicts.
    host, port : adresse d'écoute (par défaut 0.0.0.0:8081)
    enable_cors : autorise toute origine (utile depuis un client Android en
        développement). Désactiver pour un déploiement plus strict.
    """

    def __init__(self, vrs_engine, bases_status_provider: Callable[[], list],
                 host: str = "0.0.0.0", port: int = 8081,
                 enable_cors: bool = True):
        if not _FASTAPI_AVAILABLE:
            raise ImportError(
                "fastapi et uvicorn sont requis pour l'API server. "
                "Installation : `uv add fastapi uvicorn`."
            )

        self._vrs = vrs_engine
        self._get_bases = bases_status_provider
        self._host = host
        self._port = port
        self._enable_cors = enable_cors
        self._start_time = time.time()

        # Concurrence : l'event loop est créé dans le thread du serveur.
        # `publish_result` (appelée depuis un autre thread) doit donc utiliser
        # `asyncio.run_coroutine_threadsafe` pour atteindre cet event loop.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_ready = threading.Event()

        # Clients WebSocket actifs (manipulés uniquement dans l'event loop)
        self._ws_clients: Set[WebSocket] = set()

        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._app = self._build_app()

    # ------------------------------------------------------------------
    # Construction FastAPI
    # ------------------------------------------------------------------

    def _build_app(self) -> "FastAPI":
        app = FastAPI(
            title="NRTK API",
            description="API REST + WebSocket pour le moteur NRTK Centipede.",
            version="0.1.0",
        )

        if self._enable_cors:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )

        # ---- REST ----

        @app.get("/api/health")
        async def health():
            return {"ok": True, "uptime": time.time() - self._start_time}

        @app.get("/api/position")
        async def get_position():
            return {
                "position": _result_to_dict(self._vrs.last_result),
                "timestamp": time.time(),
            }

        @app.get("/api/bases")
        async def get_bases():
            return {"bases": self._get_bases(), "timestamp": time.time()}

        @app.get("/api/status")
        async def get_status():
            return self._build_status_payload()

        # ---- WebSocket ----

        @app.websocket("/ws/position")
        async def ws_position(ws: WebSocket):
            await ws.accept()
            self._ws_clients.add(ws)
            client = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
            logger.info(f"WS connecté : {client} ({len(self._ws_clients)} clients)")
            try:
                # Snapshot initial
                await ws.send_json(self._build_status_payload())
                # On reste connecté : on lit le flux entrant pour détecter la
                # déconnexion, mais on ignore le contenu (le client n'a rien
                # à dire dans cette version REST/WS-pull).
                while True:
                    await ws.receive_text()
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.debug(f"WS erreur ({client}) : {e}")
            finally:
                self._ws_clients.discard(ws)
                logger.info(f"WS déconnecté : {client} ({len(self._ws_clients)} clients restants)")

        return app

    def _build_status_payload(self) -> dict:
        return {
            "position": _result_to_dict(self._vrs.last_result),
            "bases": self._get_bases(),
            "timestamp": time.time(),
        }

    # ------------------------------------------------------------------
    # Publication thread-safe vers les WebSocket
    # ------------------------------------------------------------------

    def publish_result(self, result) -> None:
        """
        Appelé depuis le thread du moteur VRS à chaque nouveau résultat.
        Schedule un broadcast asynchrone vers tous les WebSocket connectés.
        """
        if not self._loop_ready.is_set() or self._loop is None:
            return
        if not self._ws_clients:
            return  # rien à pousser, on évite d'allouer
        payload = self._build_status_payload()
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)
        except RuntimeError:
            # Event loop déjà fermé pendant l'arrêt — on ignore
            pass

    async def _broadcast(self, payload: dict) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        if dead:
            self._ws_clients -= dead
            logger.info(f"WS purgés : {len(dead)} clients morts, {len(self._ws_clients)} restants")

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------

    def start(self) -> threading.Thread:
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            config = uvicorn.Config(
                self._app, host=self._host, port=self._port,
                log_level="warning", loop="asyncio",
                lifespan="off",  # pas de startup/shutdown handlers, simplifie
            )
            self._server = uvicorn.Server(config)
            try:
                self._loop.run_until_complete(self._server.serve())
            except Exception as e:
                logger.error(f"Serveur API arrêté sur erreur : {e}")
            finally:
                self._loop_ready.clear()

        self._thread = threading.Thread(target=_run, name="fastapi-server", daemon=True)
        self._thread.start()
        logger.info(f"Serveur API REST/WS démarré sur http://{self._host}:{self._port}/ "
                    f"(docs: /docs, websocket: /ws/position)")
        return self._thread

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        # On laisse le thread daemon mourir avec l'application
        logger.info("Serveur API REST/WS arrêté")