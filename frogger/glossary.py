"""Glossaire quant imposé au traducteur.

Le fichier `glossary.json` à la racine du projet est la source de vérité et
peut être édité librement. Son empreinte entre dans la clé de cache : modifier
un terme force la retraduction des seuls blocs qui le contiennent.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "glossary.json"


class Glossary:
    def __init__(self, terms: dict[str, str], keep: list[str], version: int = 1):
        self.terms = terms
        self.keep = keep
        self.version = version
        payload = json.dumps({"t": terms, "k": keep, "v": version}, sort_keys=True, ensure_ascii=False)
        self.digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def load(cls, path: Path | None = None) -> "Glossary":
        path = path or DEFAULT_PATH
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("terms", {}), data.get("keep", []), data.get("version", 1))

    def relevant(self, text: str) -> dict[str, str]:
        """Sous-ensemble du glossaire réellement présent dans `text`."""
        low = text.lower()
        return {en: fr for en, fr in self.terms.items() if en.lower() in low}

    def as_prompt(self) -> str:
        lines = [f"- {en} → {fr}" for en, fr in sorted(self.terms.items())]
        keep = ", ".join(self.keep)
        return (
            "Traductions obligatoires :\n"
            + "\n".join(lines)
            + "\n\nÀ ne jamais traduire ni altérer (noms propres, bibliothèques, sigles) :\n"
            + keep
        )


@lru_cache(maxsize=1)
def load_glossary(path: str | None = None) -> Glossary:
    return Glossary.load(Path(path) if path else None)
