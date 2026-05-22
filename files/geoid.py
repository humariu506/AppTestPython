"""
geoid.py
========
Chargement et interpolation du modèle de géoïde RAF20 (fichier .gtx).

Le fichier RAF20.gtx contient l'ondulation géoïdale N pour la France
métropolitaine : hauteur du quasi-géoïde IGN69 au-dessus de l'ellipsoïde
GRS80/WGS84.

Relation fondamentale :
    h_ellipsoïdal = H_orthométrique + N

Où :
    h = hauteur ellipsoïdale (utilisée pour les calculs ECEF)
    H = hauteur orthométrique (altitude « terrain », fournie par le NMEA GGA)
    N = ondulation géoïdale (lue dans RAF20.gtx)

Format GTX :
    Header (40 octets, big-endian) :
        - lat0    (float64) : latitude coin sud-ouest
        - lon0    (float64) : longitude coin sud-ouest
        - dlat    (float64) : pas en latitude
        - dlon    (float64) : pas en longitude
        - nrows   (int32)   : nombre de lignes
        - ncols   (int32)   : nombre de colonnes
    Données (nrows × ncols × float32, big-endian) :
        Ondulations N en mètres, rangées du sud vers le nord,
        d'ouest en est.
"""

import logging
import struct
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class GeoidModel:
    """
    Modèle de géoïde chargé depuis un fichier .gtx (RAF20).

    Fournit l'interpolation bilinéaire de l'ondulation géoïdale N
    en tout point (lat, lon) couvert par la grille.
    """

    def __init__(self):
        self._loaded = False
        self._lat0: float = 0.0
        self._lon0: float = 0.0
        self._dlat: float = 0.0
        self._dlon: float = 0.0
        self._nrows: int = 0
        self._ncols: int = 0
        self._grid: Optional[np.ndarray] = None  # shape (nrows, ncols)

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def lat_range(self) -> tuple[float, float]:
        """Retourne (lat_min, lat_max) de la grille."""
        return (self._lat0, self._lat0 + (self._nrows - 1) * self._dlat)

    @property
    def lon_range(self) -> tuple[float, float]:
        """Retourne (lon_min, lon_max) de la grille."""
        return (self._lon0, self._lon0 + (self._ncols - 1) * self._dlon)

    def load(self, filepath: str | Path) -> bool:
        """
        Charge un fichier .gtx (RAF20).

        Retourne True si le chargement a réussi, False sinon.
        """
        filepath = Path(filepath)
        if not filepath.exists():
            logger.error(f"Fichier géoïde introuvable : {filepath}")
            return False

        try:
            with open(filepath, "rb") as f:
                # Header : 4 × float64 + 2 × int32 = 40 octets (big-endian)
                header = f.read(40)
                if len(header) < 40:
                    logger.error("Fichier GTX trop court (header incomplet)")
                    return False

                self._lat0, self._lon0, self._dlat, self._dlon = \
                    struct.unpack(">dddd", header[:32])
                self._nrows, self._ncols = struct.unpack(">ii", header[32:40])

                # Validation basique
                if self._nrows <= 0 or self._ncols <= 0:
                    logger.error(f"Dimensions grille invalides : "
                                 f"{self._nrows}×{self._ncols}")
                    return False

                # Lecture de la grille (float32, big-endian)
                expected_bytes = self._nrows * self._ncols * 4
                grid_data = f.read(expected_bytes)
                if len(grid_data) < expected_bytes:
                    logger.error(
                        f"Données grille incomplètes : "
                        f"{len(grid_data)}/{expected_bytes} octets"
                    )
                    return False

                # Conversion en array numpy (big-endian float32 → natif)
                self._grid = np.frombuffer(grid_data, dtype=">f4").reshape(
                    (self._nrows, self._ncols)
                ).astype(np.float64)

            self._loaded = True
            lat_max = self._lat0 + (self._nrows - 1) * self._dlat
            lon_max = self._lon0 + (self._ncols - 1) * self._dlon
            logger.info(
                f"Géoïde RAF20 chargé : {self._nrows}×{self._ncols} points, "
                f"couverture [{self._lat0:.1f}°–{lat_max:.1f}°] lat, "
                f"[{self._lon0:.1f}°–{lon_max:.1f}°] lon"
            )
            return True

        except Exception as e:
            logger.error(f"Erreur de chargement du géoïde : {e}")
            return False

    def get_undulation(self, lat: float, lon: float) -> Optional[float]:
        """
        Retourne l'ondulation géoïdale N (mètres) au point (lat, lon)
        par interpolation bilinéaire.

        Retourne None si :
            - le modèle n'est pas chargé
            - les coordonnées sont hors de la grille
        """
        if not self._loaded or self._grid is None:
            return None

        # Position dans la grille (indices fractionnaires)
        row_f = (lat - self._lat0) / self._dlat
        col_f = (lon - self._lon0) / self._dlon

        # Vérification des bornes
        if row_f < 0 or col_f < 0:
            return None
        if row_f > self._nrows - 1 or col_f > self._ncols - 1:
            return None

        # Indices entiers et fractions
        row_i = int(row_f)
        col_i = int(col_f)

        # Clamper les indices pour les points exactement sur le bord supérieur
        if row_i >= self._nrows - 1:
            row_i = self._nrows - 2
        if col_i >= self._ncols - 1:
            col_i = self._ncols - 2

        dr = row_f - row_i
        dc = col_f - col_i

        # Interpolation bilinéaire
        n00 = self._grid[row_i,     col_i]
        n01 = self._grid[row_i,     col_i + 1]
        n10 = self._grid[row_i + 1, col_i]
        n11 = self._grid[row_i + 1, col_i + 1]

        n = (n00 * (1 - dr) * (1 - dc) +
             n01 * (1 - dr) * dc +
             n10 * dr * (1 - dc) +
             n11 * dr * dc)

        return float(n)

    def orthometric_to_ellipsoidal(
        self, lat: float, lon: float, h_ortho: float
    ) -> float:
        """
        Convertit une altitude orthométrique H en altitude ellipsoïdale h.

            h = H + N

        Si le géoïde n'est pas disponible pour ces coordonnées,
        retourne l'altitude inchangée.
        """
        n = self.get_undulation(lat, lon)
        if n is None:
            return h_ortho
        return h_ortho + n

    def ellipsoidal_to_orthometric(
        self, lat: float, lon: float, h_ellip: float
    ) -> float:
        """
        Convertit une altitude ellipsoïdale h en altitude orthométrique H.

            H = h - N

        Si le géoïde n'est pas disponible pour ces coordonnées,
        retourne l'altitude inchangée.
        """
        n = self.get_undulation(lat, lon)
        if n is None:
            return h_ellip
        return h_ellip - n


# ---------------------------------------------------------------------------
# Instance singleton — chargée une seule fois
# ---------------------------------------------------------------------------

_default_model: Optional[GeoidModel] = None


def load_geoid(filepath: str | Path) -> GeoidModel:
    """
    Charge le modèle de géoïde depuis le fichier spécifié.
    Retourne l'instance singleton.
    """
    global _default_model
    model = GeoidModel()
    if model.load(filepath):
        _default_model = model
    else:
        logger.warning("Géoïde non chargé — les altitudes ne seront pas corrigées")
        _default_model = model  # modèle non chargé, retournera None
    return _default_model


def get_geoid() -> Optional[GeoidModel]:
    """Retourne l'instance singleton du géoïde, ou None si non chargé."""
    return _default_model
