"""Modèle de données : blocs de texte positionnés extraits du PDF."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

_TAG = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    return _TAG.sub("", text)


class Kind(str, Enum):
    """Nature d'un bloc, qui décide de son traitement."""

    PROSE = "prose"       # corps de texte      -> traduit
    HEADING = "heading"   # titre / intertitre  -> traduit
    CAPTION = "caption"   # légende, titre de snippet ou de tableau -> traduit
    CODE = "code"         # extrait Python      -> intact
    MATH = "math"         # équation hors ligne -> intact
    TABLE = "table"       # cellule de tableau  -> intact
    CHROME = "chrome"     # en-tête, pied, folio -> intact
    LABEL = "label"       # libellé de figure   -> intact
    UNKNOWN = "unknown"


#: Blocs que le pipeline réécrit. Tout le reste est laissé tel quel.
TRANSLATABLE: frozenset[Kind] = frozenset({Kind.PROSE, Kind.HEADING, Kind.CAPTION})

BBox = tuple[float, float, float, float]


@dataclass
class Block:
    """Un bloc de texte, avec sa géométrie et son style dominant."""

    id: str                       # ex. "p0130-b04"
    page: int                     # index 0-based dans le PDF
    order: int                    # rang dans l'ordre de lecture de la page
    bbox: BBox
    kind: Kind
    text: str                     # source anglais, fragments protégés remplacés par ⟦n⟧
    protected: dict[str, str] = field(default_factory=dict)
    size: float = 10.0            # corps dominant, en points
    line_height: float = 12.0     # interligne mesuré, en points
    n_lines: int = 1
    bold: bool = False
    italic: bool = False
    align: str = "justify"        # justify | left | center
    indent: float = 0.0           # retrait de première ligne, en points
    fr: str | None = None         # traduction

    #: Indices mesurés à l'extraction, consommés par la classification.
    #: Garder ces mesures ici permet de rejouer `classify` sans relire le PDF.
    features: dict[str, Any] = field(default_factory=dict)

    # --- sérialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["bbox"] = list(self.bbox)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Block:
        d = dict(d)
        d["kind"] = Kind(d["kind"])
        d["bbox"] = tuple(d["bbox"])
        return cls(**d)

    # --- helpers -------------------------------------------------------------

    @property
    def translatable(self) -> bool:
        return self.kind in TRANSLATABLE

    @property
    def char_budget(self) -> int:
        """Nombre de caractères visé pour la traduction de ce bloc."""
        from .config import LENGTH_BUDGET

        return max(40, int(len(self.text) * LENGTH_BUDGET))

    def restored(self, text: str, *, html: bool = True) -> str:
        """Réinjecte les fragments protégés (code, maths inline) dans `text`.

        Les fragments sont stockés en HTML stylé — italique, indice, exposant,
        chasse fixe — tel qu'il figurait dans le PDF d'origine. `html=False`
        rend la version textuelle nue, pour les décomptes et les rapports.
        """
        from .config import placeholder

        for key, value in self.protected.items():
            fragment = value if html else strip_tags(value)
            text = text.replace(placeholder(int(key)), fragment)
        return text


@dataclass
class RenderStat:
    """Résultat de la réinsertion d'un bloc dans le PDF."""

    block_id: str
    page: int
    kind: str
    src_chars: int
    fr_chars: int
    scale: float          # 1.0 = taille d'origine préservée
    spare_height: float   # < 0 => le texte déborde du rectangle
    note: str = ""

    @property
    def ratio(self) -> float:
        return self.fr_chars / self.src_chars if self.src_chars else 0.0

    @property
    def status(self) -> str:
        if self.spare_height < 0:
            return "perdu"        # rien n'a pu être écrit : reprise manuelle
        if self.scale < 0.80:
            return "tasse"        # inséré, mais nettement plus petit
        if self.scale < 0.95:
            return "compresse"
        return "ok"
