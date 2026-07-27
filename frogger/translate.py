"""Étape 3 — traduction des blocs de prose.

Deux particularités par rapport à un appel de traduction ordinaire :

* **budget de caractères** — chaque bloc doit tenir dans le rectangle qu'il
  occupait en anglais. Le budget est transmis au modèle, qui reformule plus
  court plutôt que de laisser déborder la mise en page ;
* **fragments protégés** — les identifiants de code et symboles mathématiques
  rencontrés au fil du texte sont masqués par des marqueurs ⟦n⟧ que le modèle
  doit restituer tels quels.

Une passe de vérification rejette toute réponse qui perd un marqueur ou
dépasse largement son budget, et renvoie les seuls blocs fautifs au modèle.
C'est ce contrôle qui rend le pipeline tolérant à un moteur moins docile : un
modèle plus faible dégrade en reprises supplémentaires, pas en sortie
corrompue silencieusement.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .config import DEFAULT_EFFORT, DEFAULT_MODEL, PH_CLOSE, PH_OPEN
from .glossary import Glossary
from .models import Block
from .store import Store, cache_key

_PLACEHOLDER_RE = re.compile(f"{PH_OPEN}(\\d+){PH_CLOSE}")


@dataclass(frozen=True)
class Pricing:
    """Tarifs en dollars par million de tokens."""

    input: float        # entrée non mise en cache
    output: float
    cache_read: float   # token servi par le cache
    cache_write: float  # token écrit en cache


#: Anthropic facture la lecture de cache à 0,1x et l'écriture à 1,25x l'entrée.
#: DeepSeek ne surfacture pas l'écriture : un cache manqué coûte le prix normal.
PRICES: dict[str, Pricing] = {
    "claude-opus-5": Pricing(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": Pricing(5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": Pricing(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": Pricing(1.0, 5.0, 0.1, 1.25),
    "deepseek-v4-pro": Pricing(0.435, 0.87, 0.003625, 0.435),
    "deepseek-v4-flash": Pricing(0.14, 0.28, 0.0028, 0.14),
}

def describe_book(title: str = "", author: str = "") -> str:
    if title and author:
        return f"« {title} », de {author}"
    if title:
        return f"« {title} »"
    return "un ouvrage technique de finance quantitative"


SYSTEM_RULES = """\
Tu traduis un ouvrage technique d'anglais en français : {ouvrage}. Le lectorat
est composé de quants et d'ingénieurs financiers.

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

_RESPONSE_EXAMPLE = (
    'Format de réponse, en json strict et rien d\'autre :\n'
    '{"translations": [{"id": "p0130-b002", "fr": "…"}, {"id": "p0130-b003", "fr": "…"}]}'
)

#: Schéma strict, exploité par les moteurs qui savent le contraindre.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "fr": {"type": "string"}},
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
        price = PRICES.get(model)
        if price is None:
            return 0.0
        total = (
            self.input_tokens * price.input
            + self.cache_read * price.cache_read
            + self.cache_write * price.cache_write
            + self.output_tokens * price.output
        )
        return total / 1_000_000


class Translator(Protocol):
    name: str
    model_id: str
    profile: str
    usage: Usage

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]: ...


# --- socle commun ------------------------------------------------------------


class LLMTranslator:
    """Composition des lots, vérification et reprise, communes à tous les moteurs."""

    name = "llm"

    #: Nombre maximal de blocs par appel. `None` = toute la page d'un coup.
    #: Un modèle modeste tient mal un long JSON : le découpage lui évite de
    #: perdre des blocs en cours de route.
    batch_size: int | None = None

    #: Dépassement de budget toléré avant de renvoyer le bloc au modèle.
    #: Le français s'allonge naturellement d'environ 17 % : à 1.10, un modèle
    #: qui se contente de traduire fidèlement sans resserrer repart à sa copie.
    DEFAULT_LENGTH_TOLERANCE = 1.10

    def __init__(
        self,
        glossary: Glossary,
        model: str,
        max_tokens: int = 16000,
        length_tolerance: float | None = None,
        book: str = "",
    ):
        self.glossary = glossary
        self.model_id = model
        self.max_tokens = max_tokens
        self.length_tolerance = (
            self.DEFAULT_LENGTH_TOLERANCE if length_tolerance is None else length_tolerance
        )
        self.usage = Usage()
        rules = SYSTEM_RULES.format(ouvrage=book or describe_book())
        self.system = f"{rules}\n\n{glossary.as_prompt()}"
        #: Blocs dont la reprise a échoué : acceptés faute de mieux, mais
        #: signalés — une traduction fautive acceptée en silence est pire
        #: qu'une traduction manquante.
        self.unresolved: list[str] = []

    @property
    def profile(self) -> str:
        return ""

    def _complete(self, user: str) -> str:
        """Renvoie la réponse brute du modèle. À spécialiser."""
        raise NotImplementedError

    # --- composition ---------------------------------------------------------

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
            "Contexte amont déjà traduit (pour la cohérence terminologique, "
            f"ne pas retraduire) :\n{context or '(début de la sélection)'}\n\n"
        )
        tail = f"\n\n{note}" if note else ""
        return (
            head
            + "Traduis chaque bloc ci-dessous, un couple {id, fr} par bloc, dans le même ordre.\n"
            + _RESPONSE_EXAMPLE
            + "\n\n"
            + json.dumps(items, ensure_ascii=False, indent=1)
            + tail
        )

    @staticmethod
    def _parse(raw: str) -> dict[str, str]:
        text = raw.strip()
        if not text:
            raise RuntimeError("réponse vide du modèle")
        # Certains moteurs encadrent le JSON d'une clôture Markdown.
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
        data = json.loads(text)
        return {item["id"]: item["fr"] for item in data["translations"]}

    # --- contrôle ------------------------------------------------------------

    def _check(self, blocks: Sequence[Block], result: dict[str, str]) -> dict[str, str]:
        """Repère les traductions manquantes, trop longues ou ayant perdu un marqueur."""
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
            elif len(fr) > block.char_budget * self.length_tolerance:
                problems[block.id] = (
                    f"[{block.id}] {len(fr)} caractères pour un budget de "
                    f"{block.char_budget} : resserre la formulation, sans rien retrancher au sens."
                )
            elif len(block.text) > 60 and len(fr) < 0.55 * len(block.text):
                # Le français n'est jamais plus court que l'anglais d'un tiers :
                # un tel écart trahit un fragment omis, ou un décalage entre les
                # identifiants et les textes de la réponse.
                problems[block.id] = (
                    f"[{block.id}] {len(fr)} caractères pour {len(block.text)} en anglais : "
                    "traduction anormalement courte, vérifie qu'aucun fragment ne manque "
                    "et que le texte correspond bien à cet identifiant."
                )
        return problems

    def _complete_and_parse(self, user: str) -> dict[str, str]:
        """Un modèle en mode `json_object` produit parfois un JSON invalide.
        Une seconde tentative suffit presque toujours."""
        try:
            return self._parse(self._complete(user))
        except (json.JSONDecodeError, KeyError, TypeError):
            return self._parse(self._complete(user))

    def _translate_chunk(self, blocks: Sequence[Block], context: str) -> dict[str, str]:
        result = self._complete_and_parse(self._payload(blocks, context))
        faulty = self._check(blocks, result)
        if faulty:
            self.usage.repairs += 1
            note = (
                "Reprise : les blocs suivants sont à corriger. "
                + " ".join(faulty.values())
                + " Renvoie uniquement ces blocs."
            )
            subset = [b for b in blocks if b.id in faulty]
            try:
                result.update(self._complete_and_parse(self._payload(subset, context, note)))
            except Exception:  # noqa: BLE001 — on garde le premier jet
                pass
            still_faulty = self._check(subset, result)
            self.unresolved.extend(still_faulty.values())
        return result

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]:
        if not self.batch_size or len(blocks) <= self.batch_size:
            return self._translate_chunk(blocks, context)

        result: dict[str, str] = {}
        for start in range(0, len(blocks), self.batch_size):
            chunk = blocks[start : start + self.batch_size]
            result.update(self._translate_chunk(chunk, context))
            tail = " ".join(result[b.id] for b in chunk if b.id in result)
            context = tail[-400:] or context
        return result


# --- moteur factice ----------------------------------------------------------

_ACCENTS = str.maketrans({"e": "é", "a": "à", "u": "ù", "i": "î", "o": "ô", "c": "ç"})
_FILLER = "afin de mesurer précisément cette grandeur dans le cadre considéré "


class FakeTranslator:
    """Moteur d'essai : produit un texte accentué ~18 % plus long que la source.

    Sert à éprouver le rendu — couverture des accents, débordements, réduction
    d'échelle — sans clé API ni dépense.
    """

    name = "fake"
    model_id = "fake"

    def __init__(self, expansion: float = 1.18):
        self.expansion = expansion
        self.usage = Usage()

    @property
    def profile(self) -> str:
        return f"x{self.expansion}"

    def translate_batch(self, blocks: Sequence[Block], context: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for block in blocks:
            target = int(len(block.text) * self.expansion)
            # On n'accentue que hors marqueurs, pour les restituer intacts.
            parts = _PLACEHOLDER_RE.split(block.text)
            rebuilt = [
                f"{PH_OPEN}{part}{PH_CLOSE}" if index % 2 else part.translate(_ACCENTS)
                for index, part in enumerate(parts)
            ]
            text = "".join(rebuilt)
            while len(text) < target:
                text += " " + _FILLER[: target - len(text)]
            out[block.id] = text.strip()
        return out


# --- moteur Claude -----------------------------------------------------------


class ClaudeTranslator(LLMTranslator):
    """API Anthropic : schéma de sortie contraint et mise en cache du système."""

    name = "claude"

    def __init__(
        self,
        glossary: Glossary,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = 16000,
        length_tolerance: float | None = None,
        book: str = "",
    ):
        import anthropic  # importé tardivement : inutile pour les autres moteurs

        super().__init__(glossary, model, max_tokens, length_tolerance, book)
        self.client = anthropic.Anthropic()
        self.effort = effort

    @property
    def profile(self) -> str:
        return f"{self.effort}/L{self.length_tolerance:g}"

    def _complete(self, user: str) -> str:
        response = self.client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            output_config={
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA},
                "effort": self.effort,
            },
            messages=[{"role": "user", "content": user}],
        )

        u = response.usage
        self.usage.requests += 1
        self.usage.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.usage.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.usage.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"requête refusée ({getattr(details, 'category', '?')})")

        return next((b.text for b in response.content if b.type == "text"), "")


# --- moteur DeepSeek ---------------------------------------------------------


class OpenAICompatTranslator(LLMTranslator):
    """Socle pour tout service exposant l'API chat/completions d'OpenAI.

    Couvre DeepSeek et Ollama. Aucun de ces services ne contraint la sortie par
    un schéma strict — au mieux un mode `json_object` — d'où l'analyse
    défensive de la réponse. Le contrôle des marqueurs du socle commun sert de
    garde-fou : un modèle moins docile dégrade en reprises, pas en silence.
    """

    name = "openai-compat"

    default_base_url = ""
    default_model = ""
    api_key_env = ""          # vide = service local, sans authentification
    timeout = 120.0
    DEFAULT_TEMPERATURE = 1.0

    def __init__(
        self,
        glossary: Glossary,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int = 8000,
        base_url: str | None = None,
        api_key: str | None = None,
        length_tolerance: float | None = None,
        book: str = "",
    ):
        from openai import OpenAI  # importé tardivement

        super().__init__(
            glossary, model or self.default_model, max_tokens, length_tolerance, book
        )
        key = api_key or (os.environ.get(self.api_key_env) if self.api_key_env else None)
        if self.api_key_env and not key:
            raise RuntimeError(
                f"{self.api_key_env} absente de l'environnement (ou du fichier .env)."
            )
        self.client = OpenAI(
            api_key=key or "sans-objet",
            base_url=base_url or self.default_base_url,
            timeout=self.timeout,
        )
        self.temperature = self.DEFAULT_TEMPERATURE if temperature is None else temperature

    @property
    def profile(self) -> str:
        return f"t{self.temperature}/L{self.length_tolerance:g}"

    def _record(self, usage: Any) -> None:
        self.usage.requests += 1
        if usage is None:
            return
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        if hit is None or miss is None:
            self.usage.input_tokens += getattr(usage, "prompt_tokens", 0) or 0
        else:
            self.usage.input_tokens += miss or 0
            self.usage.cache_read += hit or 0
        self.usage.output_tokens += getattr(usage, "completion_tokens", 0) or 0

    def _complete(self, user: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": self.system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        self._record(response.usage)
        return response.choices[0].message.content or ""


class DeepSeekTranslator(OpenAICompatTranslator):
    """API DeepSeek."""

    name = "deepseek"
    default_base_url = "https://api.deepseek.com"
    default_model = "deepseek-v4-pro"
    api_key_env = "DEEPSEEK_API_KEY"

    #: Barème DeepSeek : 0.0 pour le code, 1.0 pour l'extraction de données,
    #: 1.3 pour la traduction. Notre tâche est une traduction sous contrainte
    #: de format strict, d'où la valeur intermédiaire.
    DEFAULT_TEMPERATURE = 1.0


class OllamaTranslator(OpenAICompatTranslator):
    """Modèle servi localement par Ollama : gratuit, et rien ne sort de la machine.

    Les lots sont volontairement courts et la température basse : un modèle de
    7 à 12 milliards de paramètres tient mal un long JSON et perd facilement un
    marqueur. La génération locale étant lente, le délai d'attente est large.
    """

    name = "ollama"
    default_base_url = "http://localhost:11434/v1"
    default_model = "gemma4:latest"
    api_key_env = ""
    timeout = 900.0
    batch_size = 5
    DEFAULT_TEMPERATURE = 0.3

    #: Mesuré sur gemma4:latest, pages 130-136 : passer le seuil de 1.30 à 1.10
    #: n'a rien changé aux dépassements (32 → 31 blocs sur 50) ni au rendu
    #: (17 blocs compressés dans les deux cas), pour deux requêtes de plus.
    #: Un modèle de cette taille reformule sans jamais gagner de place ;
    #: le renvoyer à sa copie ne fait que consommer du temps machine.
    DEFAULT_LENGTH_TOLERANCE = 1.30


# --- orchestration -----------------------------------------------------------


@dataclass
class TranslationRun:
    usage: Usage = field(default_factory=Usage)
    errors: list[str] = field(default_factory=list)
    aborted: str = ""
    #: Blocs qui gardent une traduction produite sous un réglage antérieur,
    #: faute d'avoir pu être retraduits. Les compter pour traduits masquerait
    #: un échec derrière un ancien succès.
    stale: int = 0


#: Échecs sans espoir de reprise : inutile de parcourir tout l'ouvrage pour
#: collectionner la même erreur d'authentification ou de facturation.
_FATAL = (
    "insufficient balance", "invalid api key", "authentication",
    "unauthorized", "invalid_api_key", "402", "401", "403",
)


def _is_fatal(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _FATAL)


def translate_blocks(
    blocks: Sequence[Block],
    translator: Translator,
    store: Store,
    glossary_digest: str,
    context_chars: int = 400,
    progress=None,
) -> TranslationRun:
    """Traduit les blocs traduisibles, page par page, avec cache et contexte.

    Les blocs déjà présents en cache ne repartent pas à l'API : une reprise
    après interruption ne repaie que ce qui manque.
    """
    run = TranslationRun(usage=translator.usage)
    model_id, profile = translator.model_id, translator.profile

    def key_for(block: Block) -> str:
        return cache_key(model_id, profile, glossary_digest, block.char_budget, block.text)

    by_page: dict[int, list[Block]] = {}
    for block in blocks:
        if block.translatable and block.text.strip():
            by_page.setdefault(block.page, []).append(block)

    context = ""
    for page in sorted(by_page):
        page_blocks = sorted(by_page[page], key=lambda b: b.order)

        pending: list[Block] = []
        for block in page_blocks:
            hit = store.cached(key_for(block))
            if hit is not None:
                block.fr = hit
                translator.usage.cached_blocks += 1
            else:
                pending.append(block)

        if pending:
            page_failed = False
            try:
                result = translator.translate_batch(pending, context)
            except Exception as exc:  # noqa: BLE001 — on poursuit sur les autres pages
                result, page_failed = {}, True
                run.errors.append(f"page {page + 1} : {exc}")
                if _is_fatal(exc):
                    run.aborted = (
                        "arrêt : l'erreur porte sur les identifiants ou le solde du "
                        "compte, la réessayer page après page ne changerait rien."
                    )
                    store.put_blocks(page_blocks)
                    return run

            for block in pending:
                fr = result.get(block.id)
                if not fr:
                    if block.fr:
                        run.stale += 1
                    # Une page entièrement en échec s'est déjà signalée : inutile
                    # de répéter l'erreur pour chacun de ses blocs.
                    elif not page_failed:
                        run.errors.append(f"{block.id} : aucune traduction renvoyée")
                    continue
                block.fr = fr
                translator.usage.translated_blocks += 1
                store.cache_put(key_for(block), fr, model_id)

        store.put_blocks(page_blocks)
        tail = " ".join(b.fr for b in page_blocks if b.fr)
        context = tail[-context_chars:]
        if progress:
            progress(page, len(page_blocks))

    run.errors.extend(f"non corrigé — {problem}" for problem in getattr(translator, "unresolved", []))
    return run
