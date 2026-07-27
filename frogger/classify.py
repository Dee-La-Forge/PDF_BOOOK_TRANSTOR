"""Étape 2 — classification : quel bloc part au traducteur, quel bloc reste intact.

Le typage repose d'abord sur les polices, ce qui est fiable ici : dans
« Advances in Financial Machine Learning », le corps de texte est en Times,
les extraits Python en Courier et les formules en STIX. Le nom de police
suffit donc à isoler ce qui ne doit jamais être traduit.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import Block, Kind

# Légendes et titres numérotés du livre : « FIGURE 7.1 », « SNIPPET 7.2 », « TABLE 3.1 ».
_CAPTION = re.compile(
    r"^\s*(figure|fig\.|table|tableau|snippet|exhibit|listing|algorithm)\s*[\dA-Z]",
    re.IGNORECASE,
)

# Titres de chapitre / section : « CHAPTER 7 », « 7.2 The Problem », « PART 2 ».
_NUMBERED_HEADING = re.compile(r"^\s*(chapter|part|appendix)\b|^\s*\d+(\.\d+)*\s+\S", re.IGNORECASE)

#: Bandes haute et basse de la page, exprimées en fraction de sa hauteur.
_EDGE_BAND = 0.09

#: Une légende est nécessairement courte : sans ce garde-fou, un paragraphe
#: ouvrant par « Snippet 7.1 implements… » serait pris pour un titre de snippet.
_CAPTION_MAX_CHARS = 250

#: Seuils de domination d'une famille de polices dans un bloc.
_CODE_RATIO = 0.45
_MATH_RATIO = 0.50
_SANS_RATIO = 0.60

#: Un titre est plus gros que le corps de texte, et court.
_HEADING_SIZE_RATIO = 1.12
_HEADING_MAX_CHARS = 220
_RUNNING_HEAD_MAX_CHARS = 120

#: Longueur au-delà de laquelle un bloc cerné d'un filet n'est plus une cellule.
#: La détection de tableaux repère les traits, donc aussi les encadrés : chez
#: Chan, « BOX 1.1 » et ses 1 100 caractères de prose passaient pour un tableau
#: et échappaient à la traduction.
_TABLE_MAX_CHARS = 200


def _near_edge(block: Block) -> bool:
    f = block.features
    return f.get("top_ratio", 1.0) < _EDGE_BAND or f.get("bottom_ratio", 1.0) < _EDGE_BAND


def _is_folio(block: Block) -> bool:
    """Numéro de page nu : rien à traduire."""
    return _near_edge(block) and not re.search(r"[A-Za-z]", block.text)


def _is_running_head(block: Block, base_size: float) -> bool:
    """Titre courant en haut ou en bas de page.

    Il fait partie du texte du livre — un lecteur francophone attend
    « VALIDATION CROISÉE EN FINANCE », pas l'intitulé anglais — donc il part
    au traducteur, contrairement au folio.
    """
    return (
        _near_edge(block)
        and block.size < base_size
        and block.n_lines <= 2
        and len(block.text) <= _RUNNING_HEAD_MAX_CHARS
    )


def _is_caption(text: str) -> bool:
    return len(text) <= _CAPTION_MAX_CHARS and bool(_CAPTION.match(text))


def _is_heading(block: Block, base_size: float) -> bool:
    if len(block.text) > _HEADING_MAX_CHARS:
        return False
    if block.features.get("max_size", block.size) >= base_size * _HEADING_SIZE_RATIO:
        return True
    if _NUMBERED_HEADING.match(block.text) and block.n_lines <= 3:
        return True
    # Intertitres en capitales grasses, fréquents dans l'ouvrage.
    return block.bold and block.n_lines <= 2 and block.text.isupper()


def classify_block(block: Block, base_size: float) -> Kind:
    fam = block.features.get("fam", {})
    text = block.text.strip()

    if block.features.get("in_table") and len(text) <= _TABLE_MAX_CHARS:
        return Kind.TABLE
    if _is_folio(block):
        return Kind.CHROME
    if fam.get("code", 0.0) >= _CODE_RATIO:
        return Kind.CODE
    if fam.get("math", 0.0) >= _MATH_RATIO:
        return Kind.MATH

    # Un bloc sans mot à traduire (numéros seuls, symboles) reste intact.
    if not re.search(r"[A-Za-z]{2}", text):
        return Kind.LABEL

    if _is_running_head(block, base_size):
        return Kind.HEADING

    if fam.get("sans", 0.0) >= _SANS_RATIO:
        # L'Helvetica du livre porte les titres de snippets et les libellés de
        # figures : les premiers se traduisent, les seconds appartiennent au
        # graphique et doivent rester en place.
        if _is_caption(text):
            return Kind.CAPTION
        return Kind.HEADING if len(text) <= _HEADING_MAX_CHARS else Kind.PROSE

    if _is_caption(text):
        return Kind.CAPTION
    if _is_heading(block, base_size):
        return Kind.HEADING
    return Kind.PROSE


def classify(blocks: Iterable[Block], base_size: float) -> list[Block]:
    out = []
    for block in blocks:
        block.kind = classify_block(block, base_size)
        if not block.translatable and block.protected:
            # Un bloc laissé intact n'a plus besoin de ses marqueurs : on lui
            # rend son texte lisible, ce qui remet les décomptes d'aplomb.
            block.text = block.restored(block.text, html=False)
            block.protected = {}
        out.append(block)
    return out


def summary(blocks: Iterable[Block]) -> dict[str, dict[str, int]]:
    """Répartition blocs / caractères par nature, pour le rapport."""
    stats: dict[str, dict[str, int]] = {}
    for block in blocks:
        entry = stats.setdefault(block.kind.value, {"blocs": 0, "caracteres": 0})
        entry["blocs"] += 1
        entry["caracteres"] += len(block.text)
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]["caracteres"]))
