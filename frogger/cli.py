"""Interface en ligne de commande du pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import report as reporting
from .classify import classify, summary
from .config import DEFAULT_EFFORT, DEFAULT_MODEL, MIN_SCALE, Workspace
from .extract import extract
from .glossary import Glossary, load_glossary
from .models import Kind
from .render import render as render_pdf
from .store import Store
from .translate import ClaudeTranslator, FakeTranslator, translate_blocks

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

        blocks, base_size = extract(pdf, selection)
        blocks = classify(blocks, base_size)

        store.clear_blocks(selection)
        store.put_blocks(blocks)
        store.set_meta("pdf", str(pdf.resolve()))
        store.set_meta("base_size", base_size)

        console.print(
            f"[green]{len(blocks)}[/green] blocs extraits sur "
            f"[green]{len(selection)}[/green] pages (corps de texte : {base_size} pt)"
        )
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
    engine: str = typer.Option("claude", "--engine", help="claude | fake"),
    model: str = typer.Option(DEFAULT_MODEL, "--model", help="Identifiant du modèle Claude."),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort", help="low | medium | high | xhigh | max"),
    glossary_path: Optional[Path] = typer.Option(None, "--glossary", help="Glossaire JSON alternatif."),
):
    """Traduit les blocs de prose (cache : rien n'est repayé deux fois)."""
    selection = parse_pages(pages)
    glossary = Glossary.load(glossary_path) if glossary_path else load_glossary()

    with open_store(work) as store:
        blocks = store.blocks(selection)
        todo = [b for b in blocks if b.translatable and b.text.strip()]
        if not todo:
            console.print("[yellow]Aucun bloc traduisible — lancez d'abord `extract`.[/yellow]")
            raise typer.Exit(1)

        if engine == "fake":
            translator = FakeTranslator()
            model_id = "fake"
        elif engine == "claude":
            translator = ClaudeTranslator(glossary, model=model, effort=effort)
            model_id = model
        else:
            raise typer.BadParameter("--engine attend « claude » ou « fake ».")

        console.print(
            f"{len(todo)} blocs à traduire · moteur [cyan]{engine}[/cyan]"
            + (f" · modèle [cyan]{model}[/cyan] · effort {effort}" if engine == "claude" else "")
        )

        with console.status("Traduction en cours…") as status:
            def progress(page: int, n: int) -> None:
                status.update(f"Page {page + 1} — {n} blocs")

            run = translate_blocks(
                blocks, translator, store, glossary.digest, model_id, effort, progress=progress
            )

    done = sum(1 for b in blocks if b.translatable and b.fr)
    console.print(f"[green]{done}/{len(todo)}[/green] blocs traduits")
    reporting.print_usage(console, run.usage, model_id)
    for err in run.errors:
        console.print(f"[red]![/red] {err}")


def render_cmd(
    out: Path = typer.Option(..., "--out", "-o", help="PDF de sortie."),
    work: Path = WORK_OPT,
    pages: Optional[str] = PAGES_OPT,
    pdf: Optional[Path] = typer.Option(None, "--pdf", help="PDF source (sinon celui de `extract`)."),
    subset: bool = typer.Option(False, "--subset", help="N'exporter que les pages traitées."),
    min_scale: float = typer.Option(MIN_SCALE, "--min-scale", help="Réduction d'échelle maximale."),
):
    """Supprime le texte anglais et réinsère le français dans le PDF."""
    selection = parse_pages(pages)
    workspace = Workspace(work).prepare()
    with open_store(work) as store:
        source = resolve_pdf(store, pdf)
        blocks = store.blocks(selection)
        ready = [b for b in blocks if b.translatable and b.fr]
        if not ready:
            console.print("[yellow]Aucun bloc traduit — lancez d'abord `translate`.[/yellow]")
            raise typer.Exit(1)

        stats = render_pdf(source, out, blocks, workspace, min_scale=min_scale, subset=subset)
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
    engine: str = typer.Option("claude", "--engine", help="claude | fake"),
    model: str = typer.Option(DEFAULT_MODEL, "--model"),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort"),
    subset: bool = typer.Option(False, "--subset"),
):
    """Enchaîne extract → translate → render."""
    extract_cmd(pdf=pdf, pages=pages, work=work)
    translate_cmd(work=work, pages=pages, engine=engine, model=model, effort=effort, glossary_path=None)
    render_cmd(out=out, work=work, pages=pages, pdf=pdf, subset=subset, min_scale=MIN_SCALE)


# Noms de commandes exposés à l'utilisateur.
app.command("extract")(extract_cmd)
app.command("classify")(classify_cmd)
app.command("translate")(translate_cmd)
app.command("render")(render_cmd)
app.command("report")(report_cmd)


def main() -> None:
    app()
