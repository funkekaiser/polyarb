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


@app.command()
def scan(
    dry_run: bool = typer.Option(True, "--dry-run/--no-dry-run", help="Read-only by default."),
    passes: int = typer.Option(0, help="Number of scan passes (0 = loop until interrupted)."),
    max_seconds: float = typer.Option(0.0, help="Stop after N seconds (0 = no time limit)."),
) -> None:
    """Run the read-only structural-arbitrage scanner (the default detection path)."""
    if not dry_run:
        raise typer.BadParameter(
            "execution is not available (Phase 5, gated). Only --dry-run is supported."
        )

    import asyncio
    import logging

    from polyarb.clients.clob import ClobClient
    from polyarb.clients.gamma import GammaClient
    from polyarb.config import load_settings
    from polyarb.engine.scanner import Scanner
    from polyarb.sinks.notify import build_notifier
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper())

    async def _run() -> None:
        async with GammaClient() as gamma, ClobClient() as clob:
            store = SqliteStore(settings.sqlite_path)
            notifier = build_notifier(settings.notifier, settings.notifier_url)
            scanner = Scanner(settings, gamma=gamma, clob=clob, store=store, notifier=notifier)
            try:
                await scanner.run(passes=passes, max_seconds=max_seconds or None)
            finally:
                store.close()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
