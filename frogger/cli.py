"""Interface en ligne de commande du pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import report as reporting
from .classify import classify, summary
from .config import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    MIN_SCALE,
    SERIF_FAMILIES,
    Workspace,
    load_env,
    match_serif,
)
from .extract import extract
from .glossary import Glossary, load_glossary
from .render import render as render_pdf
from .store import Store, cache_key
from .translate import (
    ClaudeTranslator,
    DeepSeekTranslator,
    FakeTranslator,
    OllamaTranslator,
    describe_book,
    translate_blocks,
)

console = Console()
app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Traduction de PDF techniques en français, mise en page d'origine préservée.",
)

WORK_OPT = typer.Option(Path("data/work"), "--work", "-w", help="Répertoire de travail.")
PAGES_OPT = typer.Option(None, "--pages", "-p", help="Pages 1-based, ex. « 130-140 » ou « 5,9,12-14 ».")


def parse_pages(spec: Optional[str]) -> Optional[list[int]]:
    """« 130-140,150 » → indices 0-based triés."""
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) for x in part.split("-", 1))
            if end < start:
                raise typer.BadParameter(f"Intervalle inversé : « {part} »")
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    if any(p < 1 for p in pages):
        raise typer.BadParameter("Les pages sont numérotées à partir de 1.")
    return sorted(p - 1 for p in pages)


def open_store(work: Path) -> Store:
    return Store(Workspace(work).prepare().db)


def resolve_pdf(store: Store, pdf: Optional[Path]) -> Path:
    path = pdf or (Path(p) if (p := store.get_meta("pdf")) else None)
    if path is None:
        raise typer.BadParameter("Aucun PDF connu : passez --pdf au moins une fois.")
    if not path.is_file():
        raise typer.BadParameter(f"Fichier introuvable : {path}")
    return path


ENGINES = ("claude", "deepseek", "ollama", "fake")


def build_translator(
    engine: str,
    glossary: Glossary,
    model: str,
    effort: str,
    temperature: Optional[float],
    base_url: Optional[str],
    length_tolerance: Optional[float] = None,
    book: str = "",
):
    """`model` vaut le défaut Claude tant que l'utilisateur ne l'a pas changé :
    on ne le transmet donc qu'aux moteurs auxquels il correspond."""
    custom_model = model if model != DEFAULT_MODEL else None

    if engine == "fake":
        return FakeTranslator()
    if engine == "claude":
        return ClaudeTranslator(
            glossary, model=model, effort=effort,
            length_tolerance=length_tolerance, book=book,
        )
    if engine == "deepseek":
        return DeepSeekTranslator(
            glossary, model=custom_model, temperature=temperature,
            base_url=base_url, length_tolerance=length_tolerance, book=book,
        )
    if engine == "ollama":
        return OllamaTranslator(
            glossary, model=custom_model, temperature=temperature,
            base_url=base_url, length_tolerance=length_tolerance, book=book,
        )
    raise typer.BadParameter(f"--engine attend l'un de : {', '.join(ENGINES)}.")


# --- commandes ---------------------------------------------------------------


@app.command()
def toc(pdf: Path = typer.Option(..., "--pdf", help="PDF source.")):
    """Affiche la table des matières, pour choisir les pages à traiter."""
    import fitz

    with fitz.open(pdf) as doc:
        entries = doc.get_toc()
        console.print(f"[bold]{doc.metadata.get('title') or pdf.name}[/bold] — {doc.page_count} pages")
    table = Table(header_style="bold")
    table.add_column("Niv.", justify="right")
    table.add_column("Titre")
    table.add_column("Page", justify="right")
    for level, title, page in entries:
        table.add_row(str(level), f"{'  ' * (level - 1)}{title}", str(page))
    console.print(table)


def extract_cmd(
    pdf: Path = typer.Option(..., "--pdf", help="PDF source."),
    pages: Optional[str] = PAGES_OPT,
    work: Path = WORK_OPT,
):
    """Extrait les blocs de texte positionnés et les classe."""
    selection = parse_pages(pages)
    with open_store(work) as store:
        import fitz

        if selection is None:
            with fitz.open(pdf) as doc:
                selection = list(range(doc.page_count))

        result = extract(pdf, selection)
        blocks = classify(result.blocks, result.base_size)

        store.clear_blocks(selection)
        store.put_blocks(blocks)
        store.set_meta("pdf", str(pdf.resolve()))
        store.set_meta("base_size", result.base_size)
        # Renseignés une seule fois : une extraction partielle ne doit pas
        # écraser ce qu'une extraction plus large avait établi.
        for key, value in (
            ("serif_font", result.serif_font),
            ("title", result.title),
            ("author", result.author),
        ):
            if value and not store.get_meta(key):
                store.set_meta(key, value)

        serif = match_serif(store.get_meta("serif_font"))
        console.print(
            f"[green]{len(blocks)}[/green] blocs extraits sur "
            f"[green]{len(selection)}[/green] pages (corps : {result.base_size} pt · "
            f"police {store.get_meta('serif_font') or '?'} → substitution [cyan]{serif}[/cyan])"
        )
        if store.get_meta("title"):
            console.print(f"Ouvrage : [cyan]{describe_book(store.get_meta('title'), store.get_meta('author'))}[/cyan]")
        reporting.print_classification(console, summary(blocks))


def classify_cmd(work: Path = WORK_OPT, pages: Optional[str] = PAGES_OPT):
    """Rejoue la classification sur les blocs déjà extraits."""
    selection = parse_pages(pages)
    with open_store(work) as store:
        base_size = store.get_meta("base_size", 10.0)
        blocks = classify(store.blocks(selection), base_size)
        store.put_blocks(blocks)
        console.print(f"[green]{len(blocks)}[/green] blocs reclassés")
        reporting.print_classification(console, summary(blocks))


def translate_cmd(
    work: Path = WORK_OPT,
    pages: Optional[str] = PAGES_OPT,
    engine: str = typer.Option("claude", "--engine", help="claude | deepseek | ollama | fake"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Identifiant du modèle."),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort", help="Claude : low | medium | high | xhigh | max"),
    temperature: Optional[float] = typer.Option(None, "--temperature", help="DeepSeek / Ollama."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Point d'entrée compatible OpenAI."),
    length_tolerance: Optional[float] = typer.Option(
        None, "--length-tolerance",
        help="Dépassement de budget toléré avant reprise (défaut 1.10).",
    ),
    glossary_path: Optional[Path] = typer.Option(None, "--glossary", help="Glossaire JSON alternatif."),
):
    """Traduit les blocs de prose (cache : rien n'est repayé deux fois)."""
    load_env()
    selection = parse_pages(pages)
    glossary = Glossary.load(glossary_path) if glossary_path else load_glossary()

    with open_store(work) as store:
        blocks = store.blocks(selection)
        todo = [b for b in blocks if b.translatable and b.text.strip()]
        if not todo:
            console.print("[yellow]Aucun bloc traduisible — lancez d'abord `extract`.[/yellow]")
            raise typer.Exit(1)

        book = describe_book(store.get_meta("title", ""), store.get_meta("author", ""))
        translator = build_translator(
            engine, glossary, model, effort, temperature, base_url, length_tolerance, book
        )
        console.print(f"Ouvrage : [cyan]{book}[/cyan]")
        console.print(
            f"{len(todo)} blocs à traduire · moteur [cyan]{engine}[/cyan] · "
            f"modèle [cyan]{translator.model_id}[/cyan]"
            + (f" · {translator.profile}" if translator.profile else "")
        )

        with console.status("Traduction en cours…") as status:
            def progress(page: int, n: int) -> None:
                status.update(f"Page {page + 1} — {n} blocs")

            run = translate_blocks(blocks, translator, store, glossary.digest, progress=progress)

    fresh = run.usage.translated_blocks + run.usage.cached_blocks
    console.print(f"[green]{fresh}/{len(todo)}[/green] blocs à jour")
    if run.stale:
        console.print(
            f"[yellow]{run.stale}[/yellow] blocs conservent une traduction "
            "produite sous un réglage antérieur"
        )
    reporting.print_usage(console, run.usage, translator.model_id)
    for err in run.errors[:10]:
        console.print(f"[red]![/red] {err}")
    if len(run.errors) > 10:
        console.print(f"[red]![/red] … et {len(run.errors) - 10} autres")
    if run.aborted:
        console.print(f"[bold red]{run.aborted}[/bold red]")
        raise typer.Exit(1)


def export_cmd(
    out: Path = typer.Option(..., "--out", "-o", help="Fichier JSON à produire."),
    work: Path = WORK_OPT,
    pages: Optional[str] = PAGES_OPT,
    only_missing: bool = typer.Option(True, "--only-missing/--all", help="N'exporter que le non traduit."),
):
    """Exporte les blocs à traduire, pour une traduction hors pipeline.

    Permet de faire traduire le lot par n'importe quel moyen — y compris une
    session Claude Code — puis de réinjecter le résultat avec `import-fr`.
    """
    selection = parse_pages(pages)
    with open_store(work) as store:
        blocks = [b for b in store.blocks(selection) if b.translatable and b.text.strip()]
        if only_missing:
            blocks = [b for b in blocks if not b.fr]
        payload = [
            {
                "id": b.id,
                "page": b.page + 1,
                "nature": b.kind.value,
                "budget_caracteres": b.char_budget,
                "en": b.text,
                "fr": b.fr or "",
            }
            for b in blocks
        ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    console.print(f"[green]{len(payload)}[/green] blocs exportés vers [cyan]{out}[/cyan]")


def import_cmd(
    source: Path = typer.Option(..., "--in", "-i", help="Fichier JSON traduit."),
    work: Path = WORK_OPT,
    label: str = typer.Option("manuel", "--label", help="Étiquette du moteur, pour le cache."),
):
    """Réinjecte des traductions produites hors pipeline."""
    payload = json.loads(source.read_text(encoding="utf-8"))
    glossary = load_glossary()

    with open_store(work) as store:
        blocks = {b.id: b for b in store.blocks()}
        updated = []
        missing = []
        for item in payload:
            block = blocks.get(item["id"])
            if block is None:
                missing.append(item["id"])
                continue
            fr = (item.get("fr") or "").strip()
            if not fr:
                continue
            block.fr = fr
            updated.append(block)
            store.cache_put(
                cache_key(label, "", glossary.digest, block.char_budget, block.text), fr, label
            )
        store.put_blocks(updated)

    console.print(f"[green]{len(updated)}[/green] blocs mis à jour")
    if missing:
        console.print(f"[yellow]{len(missing)} identifiants inconnus[/yellow] : {missing[:5]}")


def render_cmd(
    out: Path = typer.Option(..., "--out", "-o", help="PDF de sortie."),
    work: Path = WORK_OPT,
    pages: Optional[str] = PAGES_OPT,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="PDF source (sinon celui de `extract`)."),
    subset: bool = typer.Option(False, "--subset", help="N'exporter que les pages traitées."),
    min_scale: float = typer.Option(MIN_SCALE, "--min-scale", help="Réduction d'échelle maximale."),
    serif: Optional[str] = typer.Option(
        None, "--serif", help=f"Police de substitution : {' | '.join(SERIF_FAMILIES)}."
    ),
):
    """Supprime le texte anglais et réinsère le français dans le PDF."""
    selection = parse_pages(pages)
    workspace = Workspace(work).prepare()
    with open_store(work) as store:
        source = resolve_pdf(store, pdf)
        blocks = store.blocks(selection)
        if not any(b.translatable and b.fr for b in blocks):
            console.print("[yellow]Aucun bloc traduit — lancez d'abord `translate`.[/yellow]")
            raise typer.Exit(1)

        serif = serif or match_serif(store.get_meta("serif_font"))
        if serif not in SERIF_FAMILIES:
            raise typer.BadParameter(f"--serif attend l'un de : {', '.join(SERIF_FAMILIES)}")
        console.print(f"Police de substitution : [cyan]{serif}[/cyan]")
        stats = render_pdf(
            source, out, blocks, workspace, min_scale=min_scale, subset=subset, serif=serif
        )
        csv_path = reporting.write_render_csv(stats, workspace.reports / "rendu.csv")

    console.print(f"PDF écrit : [cyan]{out}[/cyan]")
    reporting.print_render(console, stats, csv_path)


def report_cmd(work: Path = WORK_OPT, pages: Optional[str] = PAGES_OPT):
    """Récapitule l'état du travail en cours."""
    selection = parse_pages(pages)
    with open_store(work) as store:
        blocks = store.blocks(selection)
        if not blocks:
            console.print("[yellow]Aucun bloc en base.[/yellow]")
            raise typer.Exit(1)
        reporting.print_classification(console, summary(blocks))
        todo = [b for b in blocks if b.translatable]
        done = [b for b in todo if b.fr]
        console.print(
            f"Traduction : [green]{len(done)}[/green]/{len(todo)} blocs · "
            f"cache : {store.cache_size()} entrées · pages : {len(store.pages())}"
        )


@app.command()
def run(
    pdf: Path = typer.Option(..., "--pdf", help="PDF source."),
    out: Path = typer.Option(..., "--out", "-o", help="PDF de sortie."),
    pages: Optional[str] = PAGES_OPT,
    work: Path = WORK_OPT,
    engine: str = typer.Option("claude", "--engine", help="claude | deepseek | ollama | fake"),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort"),
    temperature: Optional[float] = typer.Option(None, "--temperature"),
    base_url: Optional[str] = typer.Option(None, "--base-url"),
    subset: bool = typer.Option(False, "--subset"),
):
    """Enchaîne extract → translate → render."""
    extract_cmd(pdf=pdf, pages=pages, work=work)
    translate_cmd(
        work=work, pages=pages, engine=engine, model=model, effort=effort,
        temperature=temperature, base_url=base_url, glossary_path=None,
    )
    render_cmd(out=out, work=work, pages=pages, pdf=pdf, subset=subset, min_scale=MIN_SCALE)


# Noms de commandes exposés à l'utilisateur.
app.command("extract")(extract_cmd)
app.command("classify")(classify_cmd)
app.command("translate")(translate_cmd)
app.command("export")(export_cmd)
app.command("import-fr")(import_cmd)
app.command("render")(render_cmd)
app.command("report")(report_cmd)


def main() -> None:
    app()
