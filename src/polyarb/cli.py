"""polyarb CLI entry point.

Phase 0: a stub so the console script and `python -m polyarb.cli` resolve. The real
commands (`scan --dry-run`, `record`, `backtest`, `replay`) land in later phases.
The default `scan` path is read-only and must never touch a signing client.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="polyarb",
    help="Read-only Polymarket structural-arbitrage scanner (detection is the product).",
    no_args_is_help=True,
)


@app.callback()
def main() -> None:
    """Root callback — keeps subcommand structure even with one command (Phase 0)."""


@app.command()
def version() -> None:
    """Print the installed polyarb version."""
    from polyarb import __version__

    typer.echo(__version__)


@app.command()
def record(
    output_dir: str = typer.Option(
        "tests/fixtures/recorded",
        "--out",
        "-o",
        help="Directory to write captured live samples into.",
    ),
) -> None:
    """Capture live (read-only) Gamma/CLOB samples to disk for fixtures/inspection."""
    from polyarb.recording import main as record_main

    record_main(output_dir)


if __name__ == "__main__":
    app()
