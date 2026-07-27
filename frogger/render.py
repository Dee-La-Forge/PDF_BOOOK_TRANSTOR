"""Étape 4 — rendu : suppression du texte anglais, réinsertion du français.

Le texte source est retiré par annotations de rédaction, en préservant images
et tracés vectoriels. Le français est ensuite réinséré dans le rectangle
d'origine, éventuellement agrandi vers le bas jusqu'à la gouttière disponible,
et réduit d'échelle si nécessaire. Ce qui déborde malgré tout n'est pas
tronqué : c'est signalé dans le rapport.
"""

from __future__ import annotations

import html as _html
import re
from pathlib import Path
from typing import Iterable, Sequence

import fitz

from .config import (
    DEFAULT_SERIF,
    FONT_FAMILY,
    MIN_SCALE,
    MONO_FAMILY,
    SYMBOL_FAMILY,
    Workspace,
    ensure_fonts,
)
from .models import Block, Kind, RenderStat

#: Interlignes de réserve accordés en plus de la hauteur strictement nécessaire,
#: pour absorber l'allongement du français.
EXTRA_LINES = 1.0

#: Marge de sécurité conservée entre deux blocs voisins, en points.
GUTTER_MARGIN = 1.5

#: Marge haute et basse de la page, en fraction de sa hauteur.
PAGE_MARGIN = 0.045

_TAG_SPLIT = re.compile(r"(<[^>]+>)")

_REDACT_KWARGS = {
    "images": getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
    "graphics": getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
    "text": getattr(fitz, "PDF_REDACT_TEXT_REMOVE", 0),
}


def build_css(fonts: dict[str, str]) -> str:
    return "\n".join(
        [
            f"@font-face {{ font-family: {FONT_FAMILY}; src: url({fonts['regular']}); }}",
            f"@font-face {{ font-family: {FONT_FAMILY}; src: url({fonts['bold']}); font-weight: bold; }}",
            f"@font-face {{ font-family: {FONT_FAMILY}; src: url({fonts['italic']}); font-style: italic; }}",
            f"@font-face {{ font-family: {FONT_FAMILY}; src: url({fonts['bolditalic']}); "
            f"font-weight: bold; font-style: italic; }}",
            f"@font-face {{ font-family: {MONO_FAMILY}; src: url({fonts['mono']}); }}",
            f"@font-face {{ font-family: {SYMBOL_FAMILY}; src: url({fonts['symbol']}); }}",
            f"* {{ font-family: {FONT_FAMILY}; margin: 0; padding: 0; }}",
            "sub, sup { font-size: 0.72em; }",
        ]
    )


# --- repli sur une police symbole --------------------------------------------


def _wrap_missing(text: str, font: fitz.Font) -> str:
    """Bascule sur la police symbole les caractères absents du serif.

    Le Times couvre le latin étendu mais pas tout le répertoire mathématique
    de STIX ; sans ce repli, un « ⊥ » ou un « ≃ » se rendrait en case vide.
    """
    out: list[str] = []
    buffer: list[str] = []
    missing = False

    def flush() -> None:
        if not buffer:
            return
        chunk = "".join(buffer)
        out.append(f'<span style="font-family:{SYMBOL_FAMILY}">{chunk}</span>' if missing else chunk)
        buffer.clear()

    for char in text:
        char_missing = ord(char) > 127 and not font.has_glyph(ord(char))
        if char_missing != missing and buffer:
            flush()
        missing = char_missing
        buffer.append(char)
    flush()
    return "".join(out)


def _apply_fallback(html: str, font: fitz.Font) -> str:
    """Applique `_wrap_missing` aux seuls nœuds texte, jamais aux balises."""
    parts = _TAG_SPLIT.split(html)
    return "".join(
        part if index % 2 else _wrap_missing(part, font) for index, part in enumerate(parts)
    )


# --- composition -------------------------------------------------------------


def block_html(block: Block, font: fitz.Font | None = None) -> str:
    """Compose le fragment HTML réinséré pour un bloc traduit."""
    # On échappe d'abord le français — les marqueurs ⟦n⟧ y survivent — puis on
    # substitue les fragments protégés, qui sont déjà du HTML valide.
    body = _html.escape(block.fr or "")
    body = block.restored(body, html=True).replace("\n", "<br/>")
    if font is not None:
        body = _apply_fallback(body, font)
    if block.bold:
        body = f"<b>{body}</b>"
    if block.italic:
        body = f"<i>{body}</i>"

    size = max(block.size, 1.0)
    leading = min(max(block.line_height / size, 1.0), 1.6)
    # Un titre justifié s'étire disgracieusement dès qu'il passe à la ligne :
    # la justification n'a de sens que sur du texte courant.
    align = block.align if block.kind is Kind.PROSE else ("center" if block.align == "center" else "left")
    style = f"font-size:{size:.2f}pt;line-height:{leading:.3f};text-align:{align};"
    if block.indent > 1.0:
        style += f"text-indent:{block.indent:.1f}pt;"
    return f'<p style="{style}">{body}</p>'


# --- géométrie ---------------------------------------------------------------


def _obstacles(page: fitz.Page) -> list[fitz.Rect]:
    """Rectangles occupés par des images, à ne pas recouvrir."""
    try:
        return [fitz.Rect(info["bbox"]) for info in page.get_image_info()]
    except Exception:
        return []


def insert_rect(
    block: Block,
    page_blocks: Sequence[Block],
    obstacles: Sequence[fitz.Rect],
    page_rect: fitz.Rect,
    claimed: Sequence[fitz.Rect] = (),
) -> fitz.Rect:
    """Rectangle de réinsertion : la boîte d'origine, remise à la bonne hauteur.

    La bbox renvoyée par PyMuPDF épouse les glyphes : elle ignore les jambages
    et l'interligne complet, et se révèle donc trop basse pour y recomposer le
    même texte. On la ramène à la hauteur réellement nécessaire, puis on lui
    accorde un interligne de réserve — en descendant d'abord dans la gouttière,
    en remontant ensuite si besoin, sans jamais empiéter sur un voisin.
    """
    rect = fitz.Rect(block.bbox)
    size = max(block.size, 1.0)
    leading = max(block.line_height, size * 1.05)

    # Sur un bloc tourné, hauteur et largeur échangent leurs rôles : le calcul
    # d'agrandissement qui suit n'a plus de sens. On s'en tient à la boîte
    # d'origine, la rotation étant transmise au moteur de rendu.
    if int(block.features.get("rotation", 0)) % 180:
        return rect

    neighbours = [fitz.Rect(o.bbox) for o in page_blocks if o.id != block.id]
    neighbours.extend(obstacles)
    # Les blocs déjà posés valent par l'espace qu'ils occupent réellement, et
    # non par leur boîte d'origine : sans cela, un paragraphe descendant dans
    # la gouttière et le titre suivant y remontant revendiquent le même vide.
    neighbours.extend(claimed)

    def overlaps_horizontally(other: fitz.Rect) -> bool:
        return not (other.x1 <= rect.x0 + 2.0 or other.x0 >= rect.x1 - 2.0)

    below = [o.y0 for o in neighbours if overlaps_horizontally(o) and o.y0 >= rect.y1 - 1.0]
    above = [o.y1 for o in neighbours if overlaps_horizontally(o) and o.y1 <= rect.y0 + 1.0]
    limit_down = min(below) if below else page_rect.y1 - page_rect.height * PAGE_MARGIN
    limit_up = max(above) if above else page_rect.y0 + page_rect.height * PAGE_MARGIN

    available_down = max(0.0, limit_down - GUTTER_MARGIN - rect.y1)
    available_up = max(0.0, rect.y0 - (limit_up + GUTTER_MARGIN))

    # Le moteur HTML pose la première ligne au sommet du rectangle alors que la
    # bbox part de la hauteur de capitale : on remonte d'autant pour retrouver
    # la ligne de base d'origine.
    lift = min(available_up, 0.22 * size)
    rect.y0 -= lift
    available_up -= lift

    needed = block.n_lines * leading + 0.35 * size
    target = max(rect.height, needed) + EXTRA_LINES * leading
    deficit = max(0.0, target - rect.height)

    grow_down = min(available_down, deficit)
    rect.y1 += grow_down
    rect.y0 -= min(available_up, deficit - grow_down)

    rect.x0 = max(page_rect.x0, rect.x0 - 0.5)
    rect.x1 = min(page_rect.x1, rect.x1 + 0.5)
    return rect


# --- insertion ---------------------------------------------------------------


def _insert(
    page: fitz.Page,
    rect: fitz.Rect,
    html: str,
    css: str,
    archive: fitz.Archive,
    min_scale: float,
    rotate: int = 0,
) -> tuple[float, float, str]:
    """Insère le HTML dans le rectangle, sans jamais perdre de texte.

    `insert_htmlbox` renvoie une hauteur restante négative quand le texte ne
    tient pas — et dans ce cas il n'écrit *rien*. On repasse alors en réduction
    d'échelle libre : mieux vaut un paragraphe visiblement tassé, signalé dans
    le rapport, qu'un paragraphe disparu sans bruit.
    """
    # Le plancher d'échelle est tenté en premier, puis la réduction libre. Une
    # exception vaut échec de la tentative, pas de l'insertion : PyMuPDF refuse
    # une échelle calculée à 0.8799999999999999 pour un plancher à 0.88, simple
    # artefact de représentation flottante. Renoncer là-dessus perdrait le bloc.
    failure = ""
    for scale_low, libre in ((min_scale, False), (0, True)):
        try:
            spare, scale = page.insert_htmlbox(
                rect, html, css=css, archive=archive, scale_low=scale_low, rotate=rotate
            )
        except Exception as exc:  # noqa: BLE001
            failure = str(exc)
            continue
        if spare >= 0:
            return float(spare), float(scale), f"reduction libre a {scale:.0%}" if libre else ""
        failure = "ne tient pas dans le rectangle"

    return -1.0, 0.0, f"TEXTE NON INSERE - a reprendre a la main ({failure})"


# --- rendu -------------------------------------------------------------------


def render(
    pdf_path: Path,
    out_path: Path,
    blocks: Iterable[Block],
    workspace: Workspace,
    min_scale: float = MIN_SCALE,
    subset: bool = False,
    serif: str = DEFAULT_SERIF,
) -> list[RenderStat]:
    fonts = ensure_fonts(workspace.fonts, serif)
    css = build_css(fonts)
    archive = fitz.Archive(workspace.fonts)
    try:
        serif = fitz.Font(fontfile=str(workspace.fonts / fonts["regular"]))
    except Exception:
        serif = None

    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        by_page.setdefault(block.page, []).append(block)

    stats: list[RenderStat] = []
    doc = fitz.open(pdf_path)
    try:
        for pno in sorted(by_page):
            page = doc[pno]
            page_blocks = by_page[pno]
            targets = [b for b in page_blocks if b.translatable and b.fr]
            if not targets:
                continue

            for block in targets:
                page.add_redact_annot(fitz.Rect(block.bbox))
            page.apply_redactions(**_REDACT_KWARGS)

            obstacles = _obstacles(page)
            claimed: list[fitz.Rect] = []
            # De haut en bas : chaque bloc voit l'emprise définitive de ceux
            # qui le précèdent.
            for block in sorted(targets, key=lambda b: (b.bbox[1], b.order)):
                rect = insert_rect(block, page_blocks, obstacles, page.rect, claimed)
                claimed.append(fitz.Rect(rect))
                spare, scale, note = _insert(
                    page, rect, block_html(block, serif), css, archive, min_scale,
                    rotate=int(block.features.get("rotation", 0)),
                )

                stats.append(
                    RenderStat(
                        block_id=block.id,
                        page=pno,
                        kind=block.kind.value,
                        src_chars=len(block.restored(block.text, html=False)),
                        fr_chars=len(block.restored(block.fr or "", html=False)),
                        scale=round(float(scale), 3),
                        spare_height=round(float(spare), 2),
                        note=note,
                    )
                )

        if subset:
            doc.select(sorted(by_page))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        doc.save(out_path, garbage=4, deflate=True)
    finally:
        doc.close()

    return stats
