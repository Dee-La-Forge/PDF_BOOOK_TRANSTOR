"""Étape 1 — extraction : PDF → blocs de texte positionnés.

Chaque bloc conserve sa géométrie exacte (bbox), son style dominant et le
détail des polices qui le composent.

Les fragments non traduisibles rencontrés au fil de la prose — identifiants de
code, symboles et variables mathématiques, indices et exposants — sont
remplacés par des marqueurs ⟦n⟧ et mis de côté sous forme de fragments HTML
stylés. Ils sont ainsi soustraits au traducteur puis réinjectés au rendu avec
leur italique, leur indice ou leur exposant d'origine.
"""

from __future__ import annotations

import html as _html
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz

from .config import placeholder
from .models import Block, Kind

# --- familles de polices -----------------------------------------------------

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_CM_MATH = re.compile(r"^cm(sy|mi|ex|bx|r|ti)")

#: Une variable mathématique composée en italique : « X », « Yj », « k », « T ».
#: Volontairement restrictif, pour ne pas confisquer l'italique d'emphase.
_VARIABLE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,2}$")

#: Préfixes qui restent attachés par un trait d'union en fin de ligne :
#: « meta-\nlabeling » doit redonner « meta-labeling », pas « metalabeling ».
_KEEP_HYPHEN = (
    "meta", "non", "sub", "pre", "post", "co", "self", "multi", "cross",
    "out", "in", "semi", "quasi", "anti", "re", "de", "intra", "inter", "over", "under",
)


def font_family(font: str) -> str:
    """Regroupe un nom de police PDF en famille fonctionnelle."""
    f = _SUBSET_PREFIX.sub("", font).lower()
    if "courier" in f or "mono" in f or "consol" in f:
        return "code"
    if "stix" in f or "symbol" in f or "lcircle" in f or "mathematicalpi" in f or _CM_MATH.match(f):
        return "math"
    if "helvetica" in f or "arial" in f or "frutiger" in f:
        return "sans"
    return "serif"


#: Polices symbole à encodage propriétaire : le texte que PyMuPDF en extrait
#: est celui du codet, pas du glyphe. LCIRCLE10 ne sert qu'aux puces de liste
#: dans cet ouvrage, d'où la correspondance directe.
_GLYPH_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("lcircle", "•"),  # •
)


def normalize_glyph(font: str, text: str) -> str:
    name = _SUBSET_PREFIX.sub("", font).lower()
    for marker, replacement in _GLYPH_OVERRIDES:
        if marker in name:
            return replacement * len(text.strip()) or replacement
    return text


def _is_italic(span: dict) -> bool:
    name = span["font"].lower()
    return bool(span["flags"] & 2) or "italic" in name or "oblique" in name


def _is_bold(span: dict) -> bool:
    name = span["font"].lower()
    return bool(span["flags"] & 16) or "bold" in name or "black" in name


# --- fragments protégés ------------------------------------------------------


@dataclass
class _Protected:
    """Fragment soustrait à la traduction, conservé en HTML stylé."""

    html: str
    plain: str
    lead_space: bool
    trail_space: bool


def _span_html(span: dict, family: str, script: str) -> str:
    """Rend un span protégé en HTML, en conservant son style d'origine."""
    text = _html.escape(normalize_glyph(span["font"], span["text"]).strip())
    if not text:
        return ""
    if family == "code":
        text = f'<span style="font-family:bookmono">{text}</span>'
    if _is_italic(span):
        text = f"<i>{text}</i>"
    if _is_bold(span):
        text = f"<b>{text}</b>"
    if script == "sup":
        text = f"<sup>{text}</sup>"
    elif script == "sub":
        text = f"<sub>{text}</sub>"
    return text


def _script_position(span: dict, baseline: float, body_size: float) -> str:
    """Détecte indice / exposant.

    Le décalage de ligne de base ne suffit pas : une puce ou un grand opérateur
    mathématique s'écarte lui aussi de la ligne de base sans être un indice.
    On exige donc en plus un corps réduit, ce qui caractérise tout indice et
    tout exposant — et rien d'autre.
    """
    if span["size"] >= 0.86 * body_size:
        return ""
    delta = span["origin"][1] - baseline
    if span["flags"] & 1 or delta < -0.12 * body_size:
        return "sup"
    if delta > 0.06 * body_size:
        return "sub"
    return ""


def _line_items(line: dict) -> list[Any]:
    """Convertit une ligne en suite de str et de _Protected."""
    spans = [s for s in line["spans"] if s["text"]]
    if not spans:
        return []

    # Référence de la ligne : le span le plus long donne à la fois la ligne de
    # base et le corps courant.
    dominant = max(spans, key=lambda s: len(s["text"]))
    baseline = dominant["origin"][1]
    body = max(dominant["size"], 1.0)

    items: list[Any] = []
    for span in spans:
        text = span["text"]
        family = font_family(span["font"])
        script = _script_position(span, baseline, body)
        stripped = text.strip()

        protected = (
            family in ("code", "math")
            or bool(script)
            or (family == "serif" and _is_italic(span) and bool(_VARIABLE.match(stripped)))
        )

        if not protected:
            if items and isinstance(items[-1], str):
                items[-1] += text
            else:
                items.append(text)
            continue

        html = _span_html(span, family, script)
        if not html:
            continue
        fragment = _Protected(
            html=html,
            plain=normalize_glyph(span["font"], text).strip(),
            lead_space=text[:1].isspace(),
            trail_space=text[-1:].isspace(),
        )
        if items and isinstance(items[-1], _Protected):
            # Fragments contigus (« X » + indice « t ») : un seul marqueur.
            previous = items[-1]
            previous.html += fragment.html
            previous.plain += fragment.plain
            previous.trail_space = fragment.trail_space
        else:
            items.append(fragment)
    return items


def _flatten(items: Sequence[Any], protected: dict[str, str], counter: int) -> tuple[str, int]:
    out: list[str] = []
    for item in items:
        if isinstance(item, _Protected):
            if not item.plain:
                continue
            counter += 1
            protected[str(counter)] = item.html
            lead = " " if item.lead_space else ""
            trail = " " if item.trail_space else ""
            out.append(f"{lead}{placeholder(counter)}{trail}")
        else:
            out.append(item)
    return "".join(out), counter


# --- assemblage du texte d'un bloc -------------------------------------------


def _join_lines(lines: Iterable[str]) -> str:
    """Recolle les lignes d'un paragraphe en résolvant les césures."""
    text = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not text:
            text = line
            continue
        if text.endswith("-") and line[:1].islower():
            stem = re.split(r"[\s⟦]", text[:-1])[-1].lower()
            if stem in _KEEP_HYPHEN:
                text = text + line          # trait d'union légitime, on le garde
            else:
                text = text[:-1] + line     # césure typographique, on la retire
        else:
            text = f"{text} {line}"
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# --- mesures de style --------------------------------------------------------


def _style(block: dict) -> dict[str, Any]:
    sizes: Counter[float] = Counter()
    serif_sizes: Counter[float] = Counter()
    fams: Counter[str] = Counter()
    bold = italic = serif_chars = 0

    for line in block["lines"]:
        for span in line["spans"]:
            n = len(span["text"].strip())
            if not n:
                continue
            size = round(span["size"], 1)
            sizes[size] += n
            family = font_family(span["font"])
            fams[family] += n
            if family == "serif":
                serif_sizes[size] += n
                serif_chars += n
                if _is_bold(span):
                    bold += n
                if _is_italic(span):
                    italic += n

    total = sum(fams.values()) or 1
    size = (serif_sizes or sizes).most_common(1)[0][0] if (serif_sizes or sizes) else 10.0

    tops = [line["bbox"][1] for line in block["lines"]]
    gaps = [b - a for a, b in zip(tops, tops[1:]) if 0 < b - a < 5 * size]
    line_height = statistics.median(gaps) if gaps else size * 1.16

    return {
        "size": size,
        # Mesuré sur le seul texte courant : un grand opérateur mathématique
        # ne doit pas faire passer un paragraphe pour un titre.
        "max_size": max(serif_sizes) if serif_sizes else size,
        "line_height": line_height,
        "n_lines": len(block["lines"]),
        "bold": serif_chars > 0 and bold / serif_chars > 0.6,
        "italic": serif_chars > 0 and italic / serif_chars > 0.6,
        "fam": {k: v / total for k, v in fams.items()},
        "chars": total,
    }


def _geometry(block: dict, page_rect: fitz.Rect, column: tuple[float, float]) -> dict[str, Any]:
    x0, y0, x1, y1 = block["bbox"]
    left, right = column
    width = max(right - left, 1.0)

    first_x0 = block["lines"][0]["bbox"][0] if block["lines"] else x0
    indent = max(0.0, first_x0 - x0)

    inset_l, inset_r = x0 - left, right - x1
    centered = (x1 - x0) < 0.85 * width and abs(inset_l - inset_r) < 8.0 and inset_l > 12.0

    if centered:
        align = "center"
    elif len(block["lines"]) > 1:
        align = "justify"
    else:
        align = "left"

    h = page_rect.height
    return {
        "align": align,
        "indent": indent,
        "top_ratio": (y0 - page_rect.y0) / h if h else 0.0,
        "bottom_ratio": (page_rect.y1 - y1) / h if h else 0.0,
    }


def _text_column(blocks: Sequence[dict]) -> tuple[float, float]:
    """Bornes gauche/droite du bloc de texte de la page."""
    xs0 = [b["bbox"][0] for b in blocks]
    xs1 = [b["bbox"][2] for b in blocks]
    if not xs0:
        return 0.0, 1.0
    return statistics.median(xs0), max(xs1)


def _table_rects(page: fitz.Page) -> list[fitz.Rect]:
    try:
        finder = page.find_tables()
    except Exception:
        return []
    rects = []
    for table in getattr(finder, "tables", []):
        try:
            if table.row_count >= 2 and table.col_count >= 2:
                rects.append(fitz.Rect(table.bbox))
        except Exception:
            continue
    return rects


# --- extraction --------------------------------------------------------------


def body_size(doc: fitz.Document, pages: Sequence[int]) -> float:
    """Corps dominant du texte courant, servant de référence aux titres."""
    sizes: Counter[float] = Counter()
    for pno in pages:
        for block in doc[pno].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if font_family(span["font"]) == "serif":
                        sizes[round(span["size"], 1)] += len(span["text"].strip())
    return sizes.most_common(1)[0][0] if sizes else 10.0


def dominant_serif(doc: fitz.Document, pages: Sequence[int]) -> str:
    """Police de labeur de l'ouvrage, qui guide le choix de la substitution."""
    names: Counter[str] = Counter()
    for pno in pages:
        for block in doc[pno].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if font_family(span["font"]) == "serif":
                        names[_SUBSET_PREFIX.sub("", span["font"])] += len(span["text"].strip())
    return names.most_common(1)[0][0] if names else ""


def extract_page(page: fitz.Page, pno: int) -> list[Block]:
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_LIGATURES
    raw = page.get_text("dict", flags=flags, sort=True)["blocks"]
    text_blocks = [b for b in raw if b.get("type") == 0 and b.get("lines")]
    column = _text_column(text_blocks)
    tables = _table_rects(page)

    out: list[Block] = []
    for order, raw_block in enumerate(text_blocks):
        protected: dict[str, str] = {}
        counter = 0
        lines: list[str] = []
        for line in raw_block["lines"]:
            flat, counter = _flatten(_line_items(line), protected, counter)
            lines.append(flat)
        text = _join_lines(lines)
        if not text:
            continue

        style = _style(raw_block)
        geom = _geometry(raw_block, page.rect, column)
        bbox = tuple(round(v, 2) for v in raw_block["bbox"])
        rect = fitz.Rect(bbox)
        in_table = any(
            r.contains(rect) or (r & rect).get_area() > 0.6 * rect.get_area() for r in tables
        )

        out.append(
            Block(
                id=f"p{pno:04d}-b{order:03d}",
                page=pno,
                order=order,
                bbox=bbox,  # type: ignore[arg-type]
                kind=Kind.UNKNOWN,
                text=text,
                protected=protected,
                size=style["size"],
                line_height=round(style["line_height"], 2),
                n_lines=style["n_lines"],
                bold=style["bold"],
                italic=style["italic"],
                align=geom["align"],
                indent=round(geom["indent"], 2),
                features={
                    "fam": {k: round(v, 3) for k, v in style["fam"].items()},
                    "max_size": style["max_size"],
                    "chars": style["chars"],
                    "top_ratio": round(geom["top_ratio"], 4),
                    "bottom_ratio": round(geom["bottom_ratio"], 4),
                    "in_table": in_table,
                },
            )
        )
    return out


@dataclass
class Extraction:
    """Blocs extraits, accompagnés de ce que le PDF dit de lui-même."""

    blocks: list[Block]
    base_size: float
    serif_font: str
    title: str
    author: str


def extract(pdf_path: Path, pages: Sequence[int]) -> Extraction:
    """Extrait les blocs des pages demandées (index 0-based)."""
    with fitz.open(pdf_path) as doc:
        invalid = [p for p in pages if not 0 <= p < doc.page_count]
        if invalid:
            raise ValueError(
                f"Pages hors document (1-{doc.page_count}) : "
                + ", ".join(str(p + 1) for p in invalid)
            )
        meta = doc.metadata or {}
        blocks: list[Block] = []
        for pno in pages:
            blocks.extend(extract_page(doc[pno], pno))
        return Extraction(
            blocks=blocks,
            base_size=body_size(doc, pages),
            serif_font=dominant_serif(doc, pages),
            title=(meta.get("title") or "").strip(),
            author=(meta.get("author") or "").strip(),
        )
