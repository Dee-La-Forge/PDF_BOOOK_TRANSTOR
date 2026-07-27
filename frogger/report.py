"""Rapports : ce qui a été compressé, ce qui déborde, ce que ça a coûté."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

from rich.console import Console
from rich.table import Table

from .models import Block, RenderStat

_CSV_FIELDS = [
    "block_id", "page", "kind", "statut", "src_chars", "fr_chars",
    "ratio", "scale", "spare_height", "note",
]


def write_render_csv(stats: Sequence[RenderStat], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, delimiter=";")
        writer.writeheader()
        for stat in sorted(stats, key=lambda s: (s.spare_height, s.scale)):
            writer.writerow(
                {
                    "block_id": stat.block_id,
                    "page": stat.page + 1,
                    "kind": stat.kind,
                    "statut": stat.status,
                    "src_chars": stat.src_chars,
                    "fr_chars": stat.fr_chars,
                    "ratio": f"{stat.ratio:.2f}",
                    "scale": f"{stat.scale:.3f}",
                    "spare_height": f"{stat.spare_height:.1f}",
                    "note": stat.note,
                }
            )
    return path


def print_classification(console: Console, summary: dict[str, dict[str, int]]) -> None:
    table = Table(title="Répartition des blocs", header_style="bold")
    table.add_column("Nature")
    table.add_column("Blocs", justify="right")
    table.add_column("Caractères", justify="right")
    table.add_column("Traité")
    from .models import Kind, TRANSLATABLE

    for kind, row in summary.items():
        traite = "traduit" if Kind(kind) in TRANSLATABLE else "intact"
        table.add_row(kind, str(row["blocs"]), f"{row['caracteres']:,}".replace(",", " "), traite)
    console.print(table)


def print_render(console: Console, stats: Sequence[RenderStat], csv_path: Path | None = None) -> None:
    counts = {"ok": 0, "compresse": 0, "tasse": 0, "perdu": 0}
    for stat in stats:
        counts[stat.status] += 1

    table = Table(title="Réinsertion", header_style="bold")
    table.add_column("Statut")
    table.add_column("Blocs", justify="right")
    table.add_row("[green]corps d'origine[/green]", str(counts["ok"]))
    table.add_row("[yellow]compressé (95-80 %)[/yellow]", str(counts["compresse"]))
    table.add_row("[dark_orange]tassé (< 80 %)[/dark_orange]", str(counts["tasse"]))
    table.add_row("[red]non inséré[/red]", str(counts["perdu"]))
    console.print(table)

    worst = sorted(stats, key=lambda s: (s.spare_height, s.scale))[:8]
    flagged = [s for s in worst if s.status != "ok"]
    if flagged:
        detail = Table(title="Blocs à relire en priorité", header_style="bold")
        for col in ("Bloc", "Page", "Statut", "Ratio FR/EN", "Échelle", "Note"):
            detail.add_column(col)
        for stat in flagged:
            detail.add_row(
                stat.block_id,
                str(stat.page + 1),
                stat.status,
                f"{stat.ratio:.2f}",
                f"{stat.scale:.2f}",
                stat.note or "—",
            )
        console.print(detail)

    if csv_path:
        console.print(f"Détail complet : [cyan]{csv_path}[/cyan]")


def print_usage(console: Console, usage, model: str) -> None:
    table = Table(title="Consommation API", header_style="bold")
    table.add_column("Poste")
    table.add_column("Valeur", justify="right")
    table.add_row("Requêtes", str(usage.requests))
    table.add_row("Blocs traduits", str(usage.translated_blocks))
    table.add_row("Blocs servis par le cache", str(usage.cached_blocks))
    table.add_row("Reprises (marqueur ou longueur)", str(usage.repairs))
    table.add_row("Tokens entrée", f"{usage.input_tokens:,}".replace(",", " "))
    table.add_row("Tokens sortie", f"{usage.output_tokens:,}".replace(",", " "))
    table.add_row("Cache lu / écrit", f"{usage.cache_read:,} / {usage.cache_write:,}".replace(",", " "))
    table.add_row("Coût estimé", f"{usage.cost_usd(model):.2f} $")
    console.print(table)
