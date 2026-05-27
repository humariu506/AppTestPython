"""
ui.py
=====
Interface graphique tkinter pour l'affichage temps réel des coordonnées
NRTK et du statut des connexions aux 5 balises Centipede.

Rafraîchissement non-bloquant via root.after() — jamais de blocking call
dans le thread UI. Toutes les données arrivent via update_position()
et update_base_status() appelés depuis les threads de calcul.
"""

import math
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Optional

from vrs_engine import PositionResult


# ---------------------------------------------------------------------------
# Palette de couleurs
# ---------------------------------------------------------------------------
BG          = "#1A1A2E"
BG_CARD     = "#16213E"
BG_CARD2    = "#0F3460"
ACCENT      = "#E94560"
TEXT_MAIN   = "#EAEAEA"
TEXT_DIM    = "#8892A4"
TEXT_LABEL  = "#B0BAC9"
GREEN       = "#00C853"
ORANGE      = "#FF8F00"
BLUE        = "#1976D2"
RED         = "#B71C1C"
YELLOW      = "#FFD600"

FIX_COLORS = {
    "FIX":    GREEN,
    "FLOAT":  ORANGE,
    "SINGLE": BLUE,
    "NONE":   RED,
}

FIX_LABELS = {
    "FIX":    "RTK FIX ✓",
    "FLOAT":  "RTK FLOAT ~",
    "SINGLE": "SINGLE POINT",
    "NONE":   "PAS DE FIX",
}


# ---------------------------------------------------------------------------
# Dataclass statut base
# ---------------------------------------------------------------------------

class BaseStatus:
    def __init__(self, base_id: str):
        self.base_id       = base_id
        self.connected     = False
        self.msg_count     = 0
        self.last_msg_age: Optional[float] = None
        self.last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Fenêtre principale
# ---------------------------------------------------------------------------

class NrtkUI:
    """
    Interface tkinter NRTK.

    Usage :
        ui = NrtkUI(base_ids=["BASE1", ..., "BASE5"], update_rate_ms=500)
        ui.update_position(result)           # depuis n'importe quel thread
        ui.update_base_status(base_id, ...)  # idem
        ui.run()                             # bloque (mainloop)
    """

    def __init__(self, base_dict: dict[str, str], update_rate_ms: int = 500):
        self._base_dict      = base_dict
        self._base_ids       = list(base_dict.keys())
        self._update_rate_ms = update_rate_ms
        self._lock           = threading.Lock()

        # État interne (mis à jour depuis threads externes)
        self._latest_result: Optional[PositionResult] = None
        self._base_statuses: dict[str, BaseStatus] = {
            bid: BaseStatus(bid) for bid in self._base_ids
        }
        self._start_time = time.time()
        self._n_fixes    = 0
        self._n_updates  = 0

        self._build_ui()

    # ------------------------------------------------------------------
    # API publique (thread-safe)
    # ------------------------------------------------------------------

    def update_position(self, result: PositionResult):
        """Appelé depuis le thread VrsEngine."""
        with self._lock:
            self._latest_result = result
            self._n_updates += 1
            if result.fix_status == "FIX":
                self._n_fixes += 1

    def update_base_status(self, base_id: str, connected: bool,
                            msg_count: int = 0,
                            last_msg_age: Optional[float] = None,
                            error: Optional[str] = None):
        """Appelé depuis les threads NtripClient / MockNtripBase."""
        with self._lock:
            if base_id in self._base_statuses:
                s = self._base_statuses[base_id]
                s.connected    = connected
                s.msg_count    = msg_count
                s.last_msg_age = last_msg_age
                s.last_error   = error

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("NRTK Position Monitor — Centipede Network")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        # Polices
        try:
            mono = "Consolas"
            self.root.tk.call("font", "create", "test", "-family", mono)
        except Exception:
            mono = "Courier"

        f_title  = tkfont.Font(family="Helvetica", size=13, weight="bold")
        f_big    = tkfont.Font(family=mono, size=22, weight="bold")
        f_med    = tkfont.Font(family=mono, size=13, weight="bold")
        f_small  = tkfont.Font(family=mono, size=10)
        f_label  = tkfont.Font(family="Helvetica", size=9)
        f_status = tkfont.Font(family="Helvetica", size=10, weight="bold")

        pad = dict(padx=10, pady=6)

        # ---- Titre ----
        title_frame = tk.Frame(self.root, bg=BG)
        title_frame.pack(fill="x", padx=16, pady=(12, 0))

        tk.Label(title_frame, text="⬡  NRTK Position Monitor",
                 bg=BG, fg=ACCENT, font=f_title).pack(side="left")

        self._lbl_uptime = tk.Label(title_frame, text="00:00:00",
                                     bg=BG, fg=TEXT_DIM, font=f_label)
        self._lbl_uptime.pack(side="right")
        tk.Label(title_frame, text="Uptime : ", bg=BG, fg=TEXT_DIM,
                 font=f_label).pack(side="right")

        # ---- Carte Fix status ----
        fix_frame = tk.Frame(self.root, bg=BG_CARD, relief="flat", bd=0)
        fix_frame.pack(fill="x", padx=16, pady=(8, 4))

        self._canvas_fix = tk.Canvas(fix_frame, width=16, height=16,
                                      bg=BG_CARD, highlightthickness=0)
        self._canvas_fix.pack(side="left", padx=(10, 6), pady=8)
        self._dot = self._canvas_fix.create_oval(2, 2, 14, 14, fill=RED, outline="")

        self._lbl_fix = tk.Label(fix_frame, text="PAS DE FIX",
                                  bg=BG_CARD, fg=RED, font=f_status)
        self._lbl_fix.pack(side="left")

        self._lbl_precision = tk.Label(fix_frame, text="",
                                        bg=BG_CARD, fg=TEXT_DIM, font=f_label)
        self._lbl_precision.pack(side="right", padx=10)

        # ---- Coordonnées ----
        coords_frame = tk.Frame(self.root, bg=BG_CARD, relief="flat")
        coords_frame.pack(fill="x", padx=16, pady=4)

        for row_idx, (label, attr) in enumerate([
            ("Latitude  (°N)", "_lbl_lat"),
            ("Longitude (°E)", "_lbl_lon"),
            ("Altitude  (m)",  "_lbl_alt"),
            ("Géoïde N  (m)",  "_lbl_geoid"),
            ("VRS Lat   (°N)", "_lbl_vrs_lat"),
            ("VRS Lon   (°E)", "_lbl_vrs_lon"),
            ("VRS Alt   (m)",  "_lbl_vrs_alt"),
        ]):
            tk.Label(coords_frame, text=label, bg=BG_CARD, fg=TEXT_LABEL,
                     font=f_label, width=14, anchor="w").grid(
                row=row_idx, column=0, padx=(12, 4), pady=4, sticky="w")
            lbl = tk.Label(coords_frame, text="—", bg=BG_CARD,
                           fg=TEXT_MAIN, font=f_big, anchor="e", width=20)
            lbl.grid(row=row_idx, column=1, padx=(0, 12), pady=4, sticky="e")
            setattr(self, attr, lbl)

        # ---- Métriques ----
        metrics_frame = tk.Frame(self.root, bg=BG)
        metrics_frame.pack(fill="x", padx=16, pady=4)

        metric_defs = [
            ("Satellites", "_lbl_nsats"),
            ("Bases actives", "_lbl_nbases"),
            ("Âge corr.", "_lbl_age"),
            ("AR Ratio", "_lbl_ar"),
            ("PDOP", "_lbl_pdop"),
            ("Updates", "_lbl_nupdates"),
        ]
        for col, (label, attr) in enumerate(metric_defs):
            card = tk.Frame(metrics_frame, bg=BG_CARD2, relief="flat")
            card.grid(row=0, column=col, padx=3, pady=2, sticky="nsew")
            metrics_frame.columnconfigure(col, weight=1)
            tk.Label(card, text=label, bg=BG_CARD2, fg=TEXT_DIM,
                     font=f_label).pack(pady=(6, 0))
            lbl = tk.Label(card, text="—", bg=BG_CARD2, fg=TEXT_MAIN,
                           font=f_med)
            lbl.pack(pady=(2, 6))
            setattr(self, attr, lbl)

        # ---- Statut des 5 bases ----
        bases_outer = tk.Frame(self.root, bg=BG)
        bases_outer.pack(fill="x", padx=16, pady=(4, 2))
        tk.Label(bases_outer, text="Connexions NTRIP",
                 bg=BG, fg=TEXT_DIM, font=f_label).pack(anchor="w")

        bases_frame = tk.Frame(self.root, bg=BG)
        bases_frame.pack(fill="x", padx=16, pady=(0, 8))

        self._base_widgets: dict[str, dict] = {}
        for col, bid in enumerate(self._base_ids):
            card = tk.Frame(bases_frame, bg=BG_CARD, relief="flat", bd=0)
            card.grid(row=0, column=col, padx=3, sticky="nsew")
            bases_frame.columnconfigure(col, weight=1)

            # Indicateur LED
            cv = tk.Canvas(card, width=12, height=12, bg=BG_CARD,
                            highlightthickness=0)
            cv.pack(side="left", padx=(6, 2), pady=8)
            dot = cv.create_oval(1, 1, 11, 11, fill=RED, outline="")

            # Labels
            inner = tk.Frame(card, bg=BG_CARD)
            inner.pack(side="left", pady=4)
            display_name = self._base_dict.get(bid, bid)
            name_lbl = tk.Label(inner, text=display_name, bg=BG_CARD, fg=TEXT_MAIN,
                                 font=f_small, anchor="w")
            name_lbl.pack(anchor="w")
            info_lbl = tk.Label(inner, text="Déconnecté", bg=BG_CARD,
                                 fg=TEXT_DIM, font=f_label, anchor="w")
            info_lbl.pack(anchor="w")

            self._base_widgets[bid] = {
                "canvas": cv, "dot": dot,
                "name": name_lbl, "info": info_lbl,
            }

        # ---- Log console ----
        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(log_frame, text="Journal", bg=BG, fg=TEXT_DIM,
                 font=f_label).pack(anchor="w")
        self._log_text = tk.Text(
            log_frame, height=5, bg=BG_CARD, fg=TEXT_DIM,
            font=("Courier", 9), relief="flat", state="disabled",
            wrap="word", insertbackground=TEXT_MAIN,
        )
        self._log_text.pack(fill="x")
        self._log_text.tag_config("fix",    foreground=GREEN)
        self._log_text.tag_config("float",  foreground=ORANGE)
        self._log_text.tag_config("warn",   foreground=YELLOW)
        self._log_text.tag_config("error",  foreground=RED)
        self._log_text.tag_config("info",   foreground=TEXT_DIM)

        # Bouton quitter
        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(fill="x", padx=16, pady=(0, 12))
        tk.Button(btn_frame, text="⏹  Quitter", command=self.root.quit,
                  bg=BG_CARD2, fg=TEXT_MAIN, relief="flat",
                  activebackground=ACCENT, cursor="hand2").pack(side="right")

        self.root.geometry("760x760")

    # ------------------------------------------------------------------
    # Boucle de rafraîchissement
    # ------------------------------------------------------------------

    def _schedule_refresh(self):
        self._refresh()
        self.root.after(self._update_rate_ms, self._schedule_refresh)

    def _refresh(self):
        # Uptime
        elapsed = int(time.time() - self._start_time)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        self._lbl_uptime.config(text=f"{h:02d}:{m:02d}:{s:02d}")

        with self._lock:
            result = self._latest_result
            bases  = dict(self._base_statuses)
            n_upd  = self._n_updates

        # Position
        if result is not None:
            color = FIX_COLORS.get(result.fix_status, RED)
            self._canvas_fix.itemconfig(self._dot, fill=color)
            self._lbl_fix.config(
                text=FIX_LABELS.get(result.fix_status, "INCONNU"),
                fg=color
            )
            self._lbl_precision.config(text=result.precision_str, fg=TEXT_DIM)

            self._lbl_lat.config(text=f"{result.lat:+.8f}", fg=color)
            self._lbl_lon.config(text=f"{result.lon:+.8f}", fg=color)
            self._lbl_alt.config(text=f"{result.alt:+.3f}", fg=color)

            # Géoïde
            if result.geoid_undulation != 0.0:
                self._lbl_geoid.config(
                    text=f"{result.geoid_undulation:+.3f}", fg=TEXT_DIM
                )
            else:
                self._lbl_geoid.config(text="—", fg=TEXT_DIM)

            self._lbl_vrs_lat.config(text=f"{result.vrs_lat:+.8f}", fg=TEXT_DIM)
            self._lbl_vrs_lon.config(text=f"{result.vrs_lon:+.8f}", fg=TEXT_DIM)
            self._lbl_vrs_alt.config(text=f"{result.vrs_alt:+.3f}", fg=TEXT_DIM)

            self._lbl_nsats.config(text=str(result.n_sats_used))
            self._lbl_nbases.config(text=str(result.n_bases_used))
            age_str = f"{result.age_diff:.1f} s" if result.age_diff > 0 else "—"
            self._lbl_age.config(text=age_str)
            ar_str  = f"{result.ar_ratio:.1f}" if result.ar_ratio > 0 else "—"
            self._lbl_ar.config(text=ar_str,
                                  fg=GREEN if result.ar_ratio >= 3 else TEXT_MAIN)
            pdop_str = f"{result.pdop:.1f}" if result.pdop < 90 else "—"
            self._lbl_pdop.config(
                text=pdop_str,
                fg=GREEN if result.pdop < 2 else ORANGE if result.pdop < 4 else RED
            )
            self._lbl_nupdates.config(text=str(n_upd))

        # Bases
        for bid, status in bases.items():
            w = self._base_widgets.get(bid)
            if w is None:
                continue
            if status.connected:
                age = status.last_msg_age
                if age is not None and age < 5:
                    dot_color = GREEN
                    info_str  = f"{status.msg_count} msg  {age:.1f}s"
                else:
                    dot_color = ORANGE
                    info_str  = f"{status.msg_count} msg  vieux"
            else:
                dot_color = RED
                info_str  = status.last_error or "Déconnecté"

            w["canvas"].itemconfig(w["dot"], fill=dot_color)
            w["info"].config(text=info_str,
                              fg=TEXT_DIM if status.connected else RED)

    def log(self, message: str, level: str = "info"):
        """Ajoute une ligne dans le journal (peut être appelé depuis n'importe quel thread)."""
        import logging
        logging.getLogger("ui").info(message)
        ts  = time.strftime("%H:%M:%S")
        tag = level.lower()
        self.root.after(0, self._append_log, f"[{ts}] {message}\n", tag)

    def _append_log(self, text: str, tag: str):
        self._log_text.config(state="normal")
        self._log_text.insert("end", text, tag)
        self._log_text.see("end")
        # Garder 200 lignes max
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 200:
            self._log_text.delete("1.0", "20.0")
        self._log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Lancement
    # ------------------------------------------------------------------

    def run(self):
        """Démarre la boucle tkinter (bloquant)."""
        self._schedule_refresh()
        self.log("Interface démarrée — en attente de données…", "info")
        self.root.mainloop()
