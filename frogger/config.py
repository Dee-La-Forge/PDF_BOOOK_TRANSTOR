"""Configuration globale du pipeline."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"

# Budget de longueur accordé à la traduction, en multiple de la source anglaise.
# Le français est naturellement ~15-20 % plus long ; on impose 1.15 au traducteur
# et on rattrape le reste par réduction d'échelle au rendu.
LENGTH_BUDGET = 1.15

# Réduction d'échelle maximale tolérée à la réinsertion (0.88 = -12 %).
MIN_SCALE = 0.88

# Marqueurs des fragments protégés (code / maths inline) au sein de la prose.
PH_OPEN, PH_CLOSE = "\u27e6", "\u27e7"  # ⟦ ⟧


def placeholder(n: int) -> str:
    return f"{PH_OPEN}{n}{PH_CLOSE}"


# --- Polices -----------------------------------------------------------------
# Le corps de texte du livre est composé en Times. Les sous-ensembles embarqués
# dans le PDF ne contiennent pas les glyphes accentués (l'original est anglais),
# il faut donc substituer une police complète, visuellement équivalente.

FONT_VARIANTS = ("regular", "bold", "italic", "bolditalic", "mono", "symbol")

#: Familles de substitution disponibles. Chaque ouvrage a sa police de labeur ;
#: en choisir une proche préserve la couleur de la page et, à largeur de
#: caractère plus étroite, ménage de la place pour l'allongement du français.
SERIF_FAMILIES: dict[str, dict[str, tuple[str, ...]]] = {
    "times": {
        "regular": ("times.ttf", "Times New Roman.ttf", "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf"),
        "bold": ("timesbd.ttf", "Times New Roman Bold.ttf", "LiberationSerif-Bold.ttf", "DejaVuSerif-Bold.ttf"),
        "italic": ("timesi.ttf", "Times New Roman Italic.ttf", "LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf"),
        "bolditalic": ("timesbi.ttf", "Times New Roman Bold Italic.ttf", "LiberationSerif-BoldItalic.ttf", "DejaVuSerif-BoldItalic.ttf"),
    },
    "constantia": {
        "regular": ("constan.ttf", "Constantia.ttf"),
        "bold": ("constanb.ttf", "Constantia Bold.ttf"),
        "italic": ("constani.ttf", "Constantia Italic.ttf"),
        "bolditalic": ("constanz.ttf", "Constantia Bold Italic.ttf"),
    },
    "palatino": {
        "regular": ("PALA.TTF", "Palatino.ttc", "pala.ttf"),
        "bold": ("PALAB.TTF", "palab.ttf"),
        "italic": ("PALAI.TTF", "palai.ttf"),
        "bolditalic": ("PALABI.TTF", "palabi.ttf"),
    },
    "georgia": {
        "regular": ("georgia.ttf",),
        "bold": ("georgiab.ttf",),
        "italic": ("georgiai.ttf",),
        "bolditalic": ("georgiaz.ttf",),
    },
}

DEFAULT_SERIF = "times"

#: Correspondance entre la police de labeur du PDF source et la substitution la
#: plus proche parmi celles disponibles. Le premier fragment trouvé l'emporte.
#:
#: Deux critères s'opposent : la ressemblance et l'encombrement. Mesuré sur le
#: chapitre 1 d'« Algorithmic Trading », composé en Perpetua — blocs rendus au
#: corps d'origine puis tassés sous 80 % : Times 29/97, Constantia 22/120,
#: Palatino 22/127, Georgia 21/126. Times est la plus étroite des quatre, donc
#: celle qui laisse le plus de texte à sa taille d'origine. Les faces claires et
#: étroites lui sont renvoyées ; celles qui ont leur équivalent exact le gardent.
SERIF_MATCH: tuple[tuple[str, str], ...] = (
    ("perpetua", "times"),
    ("garamond", "times"),
    ("minion", "times"),
    ("constantia", "constantia"),
    ("palatino", "palatino"),
    ("book antiqua", "palatino"),
    ("georgia", "georgia"),
    ("times", "times"),
    ("serif", "times"),
)


def match_serif(source_font: str | None) -> str:
    """Choisit la famille de substitution la plus proche de la police source."""
    if not source_font:
        return DEFAULT_SERIF
    name = source_font.lower()
    for marker, family in SERIF_MATCH:
        if marker in name:
            return family
    return DEFAULT_SERIF


_SHARED_CANDIDATES: dict[str, tuple[str, ...]] = {
    # Identifiants de code cités au fil du texte.
    "mono": ("cour.ttf", "Courier New.ttf", "LiberationMono-Regular.ttf", "DejaVuSansMono.ttf"),
    # Filet de sécurité pour les symboles mathématiques absents de la serif.
    "symbol": ("seguisym.ttf", "cambria.ttc", "DejaVuSans.ttf", "Arial Unicode.ttf"),
}

_SYSTEM_FONT_DIRS = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
)

# Noms de familles exposés au moteur de rendu HTML.
FONT_FAMILY = "bookserif"
MONO_FAMILY = "bookmono"
SYMBOL_FAMILY = "booksym"

#: Variantes sans lesquelles le rendu reste possible.
OPTIONAL_VARIANTS = ("mono", "symbol")


def load_env(path: Path | None = None) -> None:
    """Charge un fichier .env s'il existe, sans écraser l'environnement en place.

    Les clés API vivent là plutôt que dans le code : `.env` est exclu du dépôt.
    """
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    # utf-8-sig : PowerShell écrit volontiers un BOM, qui sinon se retrouve
    # collé au nom de la première clé et la rend introuvable.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def ensure_fonts(dest: Path, serif: str = DEFAULT_SERIF) -> dict[str, str]:
    """Copie les variantes de la police de substitution dans `dest`.

    Renvoie {variante: nom de fichier}. Lève RuntimeError si la variante
    « regular » est introuvable — sans elle aucun rendu accentué n'est possible.
    """
    dest.mkdir(parents=True, exist_ok=True)
    family = SERIF_FAMILIES.get(serif, SERIF_FAMILIES[DEFAULT_SERIF])
    candidates = {**family, **_SHARED_CANDIDATES}
    # La famille demandée peut manquer sur la machine : le Times sert de recours.
    fallback = SERIF_FAMILIES[DEFAULT_SERIF]
    resolved: dict[str, str] = {}

    for variant in FONT_VARIANTS:
        for name in candidates[variant] + fallback.get(variant, ()):
            target = dest / name
            if target.exists():
                resolved[variant] = name
                break
            src = next((d / name for d in _SYSTEM_FONT_DIRS if (d / name).is_file()), None)
            if src is not None:
                shutil.copy2(src, target)
                resolved[variant] = name
                break

    if "regular" not in resolved:
        dirs = "\n  ".join(str(d) for d in _SYSTEM_FONT_DIRS)
        raise RuntimeError(
            "Aucune police serif Unicode trouvée (Times New Roman, Liberation Serif "
            f"ou DejaVu Serif). Répertoires explorés :\n  {dirs}"
        )

    # Les variantes manquantes retombent sur la romaine.
    for variant in FONT_VARIANTS:
        resolved.setdefault(variant, resolved["regular"])
    return resolved


def font_path(dest: Path, resolved: dict[str, str], variant: str) -> Path:
    return dest / resolved[variant]


# --- Arborescence de travail -------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    """Répertoire de travail d'un ouvrage : état, polices, sorties."""

    root: Path

    @property
    def db(self) -> Path:
        return self.root / "frogger.db"

    @property
    def fonts(self) -> Path:
        return self.root / "fonts"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    def prepare(self) -> Workspace:
        self.root.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)
        return self
