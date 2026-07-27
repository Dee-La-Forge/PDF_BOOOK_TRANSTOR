"""Étape 3 — traduction des blocs de prose via l'API Claude.

Deux particularités par rapport à un appel de traduction ordinaire :

* **budget de caractères** — chaque bloc doit tenir dans le rectangle qu'il
  occupait en anglais. Le budget est transmis au modèle, qui reformule plus
  court plutôt que de laisser déborder la mise en page ;
* **fragments protégés** — les identifiants de code et symboles mathématiques
  rencontrés au fil du texte sont masqués par des marqueurs ⟦n⟧ que le modèle
  doit restituer tels quels. Une passe de vérification rejette toute réponse
  qui en perd un.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence

from .config import DEFAULT_EFFORT, DEFAULT_MODEL, PH_CLOSE, PH_OPEN
from .glossary import Glossary
from .models import Block
from .store import Store, cache_key

_PLACEHOLDER_RE = re.compile(f"{PH_OPEN}(\\d+){PH_CLOSE}")

#: Tarifs API en dollars par million de tokens (entrée, sortie).
_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

SYSTEM_RULES = """\
Tu traduis un ouvrage technique d'anglais en français : « Advances in Financial
Machine Learning » de Marcos López de Prado. Le lectorat est composé de
quants et d'ingénieurs financiers.

Registre : français technique et académique, précis, sans lourdeur. Emploie
les conventions typographiques françaises (guillemets « », espace insécable
avant : ; ? !, insécable dans les nombres).

Règles impératives :
1. Rends uniquement la traduction. Aucun commentaire, aucune note, aucune
   reformulation de la consigne.
2. Les marqueurs de la forme ⟦1⟧, ⟦2⟧ … encadrent du code ou des symboles
   mathématiques masqués. Reproduis-les à l'identique, tous, sans en ajouter.
   Tu peux les déplacer dans la phrase si la syntaxe française l'exige.
3. N'invente rien et ne supprime aucun contenu technique. Une formule, une
   référence bibliographique, un renvoi de figure ou d'équation doivent
   subsister.
4. Respecte le glossaire ci-dessous mot pour mot.
5. Contrainte de longueur : chaque bloc indique un budget en caractères. Le
   texte français doit tenir dedans, car il sera réinséré dans l'emplacement
   exact qu'occupait l'anglais dans le PDF. Si ta première formulation dépasse,
   resserre-la : supprime les chevilles, préfère le terme court, évite les
   périphrases. Ne tronque jamais le sens pour gagner de la place.
"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "fr": {"type": "string"},
                },
                "required": ["id", "fr"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def placeholders(text: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(text)


@dataclass
class Usage:
    """Consommation cumulée, pour le rapport de coût."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cached_blocks: int = 0
    translated_blocks: int = 0
    repairs: int = 0

    def cost_usd(self, model: str) -> float:
        price_in, price_out = _PRICES.get(model, (0.0, 0.0))
        billed_in = self.input_tokens + 1.25 * self.cache_write + 0.1 * self.cache_read
        return (billed_in * price_in + self.output_tokens * price_out) / 1_000_000


class Translator(Protocol):
    name: str

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]: ...


# --- moteur factice ----------------------------------------------------------

_ACCENTS = str.maketrans({"e": "é", "a": "à", "u": "ù", "i": "î", "o": "ô", "c": "ç"})
_FILLER = "afin de mesurer précisément cette grandeur dans le cadre considéré "


class FakeTranslator:
    """Moteur d'essai : produit un texte accentué ~18 % plus long que la source.

    Sert à éprouver le rendu — couverture des accents, débordements, réduction
    d'échelle — sans clé API ni dépense.
    """

    name = "fake"

    def __init__(self, expansion: float = 1.18):
        self.expansion = expansion

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for block in blocks:
            target = int(len(block.text) * self.expansion)
            # On n'accentue que hors marqueurs, pour les restituer intacts.
            parts = _PLACEHOLDER_RE.split(block.text)
            rebuilt: list[str] = []
            for i, part in enumerate(parts):
                rebuilt.append(f"{PH_OPEN}{part}{PH_CLOSE}" if i % 2 else part.translate(_ACCENTS))
            text = "".join(rebuilt)
            while len(text) < target:
                text += " " + _FILLER[: target - len(text)]
            out[block.id] = text.strip()
        return out


# --- moteur Claude -----------------------------------------------------------


class ClaudeTranslator:
    """Traduction par lots : un appel API par page, contexte glissant."""

    name = "claude"

    def __init__(
        self,
        glossary: Glossary,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = 16000,
    ):
        import anthropic  # importé tardivement : inutile pour le moteur factice

        self.client = anthropic.Anthropic()
        self.glossary = glossary
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        self._system = f"{SYSTEM_RULES}\n\n{glossary.as_prompt()}"

    def _payload(self, blocks: Sequence[Block], context: str, note: str = "") -> str:
        items = [
            {
                "id": b.id,
                "nature": b.kind.value,
                "budget_caracteres": b.char_budget,
                "texte": b.text,
            }
            for b in blocks
        ]
        head = (
            f"Contexte amont déjà traduit (pour la cohérence terminologique, ne pas retraduire) :\n"
            f"{context or '(début de la sélection)'}\n\n"
        )
        tail = f"\n\n{note}" if note else ""
        return (
            head
            + "Traduis chaque bloc ci-dessous. Renvoie un objet JSON contenant un "
            + "tableau `translations` avec un couple {id, fr} par bloc, dans le même ordre.\n\n"
            + json.dumps(items, ensure_ascii=False, indent=1)
            + tail
        )

    def _call(self, blocks: Sequence[Block], context: str, note: str = "") -> dict[str, str]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}, "effort": self.effort},
            messages=[{"role": "user", "content": self._payload(blocks, context, note)}],
        )

        u = response.usage
        self.usage.requests += 1
        self.usage.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.usage.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.usage.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"Requête refusée par le modèle ({getattr(details, 'category', '?')}).")

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise RuntimeError("Réponse vide du modèle.")
        data = json.loads(text)
        return {item["id"]: item["fr"] for item in data["translations"]}

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]:
        result = self._call(blocks, context)
        faulty = self._check(blocks, result)
        if faulty:
            self.usage.repairs += 1
            note = (
                "Reprise : les blocs suivants sont à corriger. "
                + " ".join(faulty.values())
                + " Renvoie uniquement ces blocs."
            )
            subset = [b for b in blocks if b.id in faulty]
            result.update(self._call(subset, context, note))
        return result

    @staticmethod
    def _check(blocks: Sequence[Block], result: dict[str, str]) -> dict[str, str]:
        """Repère les traductions manquantes, tronquées ou ayant perdu un marqueur."""
        problems: dict[str, str] = {}
        for block in blocks:
            fr = result.get(block.id)
            if not fr:
                problems[block.id] = f"[{block.id}] absent de la réponse."
                continue
            expected, got = placeholders(block.text), placeholders(fr)
            if sorted(expected) != sorted(got):
                problems[block.id] = (
                    f"[{block.id}] marqueurs attendus {expected}, reçus {got} : "
                    "reproduis-les tous, à l'identique."
                )
            elif len(fr) > block.char_budget * 1.30:
                problems[block.id] = (
                    f"[{block.id}] {len(fr)} caractères pour un budget de "
                    f"{block.char_budget} : resserre la formulation."
                )
        return problems


# --- orchestration -----------------------------------------------------------


@dataclass
class TranslationRun:
    usage: Usage = field(default_factory=Usage)
    errors: list[str] = field(default_factory=list)


def translate_blocks(
    blocks: Sequence[Block],
    translator: Translator,
    store: Store,
    glossary_digest: str,
    model_id: str,
    effort: str,
    context_chars: int = 400,
    progress=None,
) -> TranslationRun:
    """Traduit les blocs traduisibles, page par page, avec cache et contexte.

    Les blocs déjà présents en cache ne repartent pas à l'API : une reprise
    après interruption ne repaie que ce qui manque.
    """
    run = TranslationRun()
    usage = getattr(translator, "usage", run.usage)
    run.usage = usage

    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        if block.translatable and block.text.strip():
            by_page.setdefault(block.page, []).append(block)

    context = ""
    for page in sorted(by_page):
        page_blocks = sorted(by_page[page], key=lambda b: b.order)

        pending: list[Block] = []
        for block in page_blocks:
            key = cache_key(model_id, effort, glossary_digest, block.char_budget, block.text)
            hit = store.cached(key)
            if hit is not None:
                block.fr = hit
                usage.cached_blocks += 1
            else:
                pending.append(block)

        if pending:
            try:
                result = translator.translate_batch(pending, context)
            except Exception as exc:  # noqa: BLE001 — on continue sur les autres pages
                run.errors.append(f"page {page + 1} : {exc}")
                result = {}
            for block in pending:
                fr = result.get(block.id)
                if not fr:
                    run.errors.append(f"{block.id} : aucune traduction renvoyée")
                    continue
                block.fr = fr
                usage.translated_blocks += 1
                key = cache_key(model_id, effort, glossary_digest, block.char_budget, block.text)
                store.cache_put(key, fr, model_id)

        store.put_blocks(page_blocks)
        tail = " ".join(b.fr for b in page_blocks if b.fr)
        context = tail[-context_chars:]
        if progress:
            progress(page, len(page_blocks))

    return run
