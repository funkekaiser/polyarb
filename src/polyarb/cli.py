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

    from polyarb.clients.clob import ClobClient
    from polyarb.clients.gamma import GammaClient
    from polyarb.config import load_settings
    from polyarb.engine.scanner import Scanner
    from polyarb.logging_setup import configure_logging
    from polyarb.sinks.notify import build_notifier
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()
    configure_logging(settings.log_level)

    if settings.metrics_enabled:
        from polyarb.engine.metrics import start_metrics_server

        start_metrics_server(settings.metrics_port)

    async def _run() -> None:
        async with GammaClient() as gamma, ClobClient() as clob:
            # Notifier (which eagerly opens an httpx client for webhooks) and store are both
            # created inside the try so the finally releases whichever got created — even if the
            # other's construction raises (a bad notifier config, or a store-open failure).
            notifier = build_notifier(settings.notifier, settings.notifier_url)
            store: SqliteStore | None = None
            try:
                store = SqliteStore(settings.sqlite_path)
                scanner = Scanner(settings, gamma=gamma, clob=clob, store=store, notifier=notifier)
                await scanner.run(passes=passes, max_seconds=max_seconds or None)
            finally:
                if store is not None:
                    store.close()
                await notifier.aclose()

    asyncio.run(_run())


@app.command()
def backtest(
    limit: int = typer.Option(10000, help="Max stored opportunities to analyze."),
) -> None:
    """Summarize the stored opportunity history (counts, bps stats, would-be P&L)."""
    from polyarb.config import load_settings
    from polyarb.engine.backtest import format_summary, summarize
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()
    store = SqliteStore(settings.sqlite_path)
    try:
        opps = store.recent(limit)
    finally:
        store.close()
    typer.echo(format_summary(summarize(opps)))


@app.command()
def replay(
    limit: int = typer.Option(50, help="Most recent stored opportunities to replay."),
) -> None:
    """Print stored opportunities oldest-first (a re-emit of the persisted feed)."""
    from polyarb.config import load_settings
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()
    store = SqliteStore(settings.sqlite_path)
    try:
        opps = store.recent(limit)
    finally:
        store.close()
    if not opps:
        typer.echo("no stored opportunities")
        return
    for opp in reversed(opps):  # store.recent is newest-first; replay oldest-first
        typer.echo(
            f"[{opp.detector}] {opp.net_profit_bps:.1f}bps "
            f"size={opp.executable_size} risk={opp.resolution_risk} :: {opp.description}"
        )


@app.command()
def healthcheck() -> None:
    """Exit 0 if the scan loop has pulsed recently; exit 1 if stale/missing (Docker HEALTHCHECK).

    Reads HEARTBEAT_PATH from Settings and checks that the recorded timestamp is within
    ``max(2 * SCAN_INTERVAL_SECONDS, 120)`` seconds.  Missing / unreadable / stale file
    → non-zero exit with a diagnostic on stderr.  Side-effect-free (read-only).
    """
    from datetime import UTC, datetime

    from polyarb.config import load_settings

    settings = load_settings()
    path = settings.heartbeat_path
    if path is None:
        typer.echo("HEARTBEAT_PATH is not configured — set it in the environment or .env", err=True)
        raise typer.Exit(code=1)

    freshness_window = max(2.0 * settings.scan_interval_seconds, 120.0)

    try:
        raw = path.read_text().strip()
        recorded_ts = float(raw)
    except FileNotFoundError:
        typer.echo(f"heartbeat file not found: {path}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        typer.echo(f"heartbeat file unreadable ({path}): {exc}", err=True)
        raise typer.Exit(code=1) from exc

    now_ts = datetime.now(UTC).timestamp()
    age = now_ts - recorded_ts
    if age > freshness_window:
        typer.echo(
            f"heartbeat stale: {age:.1f}s since last pass (window={freshness_window:.0f}s)",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"ok: heartbeat {age:.1f}s old (window={freshness_window:.0f}s)")


if __name__ == "__main__":
    app()
