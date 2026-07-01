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
    """Print the installed polyarb version (alpha)."""
    from polyarb import __version__

    typer.echo(f"polyarb {__version__} (alpha)")


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
    """Summarize the stored opportunity history: would-be stats AND realized ledger P&L."""
    from polyarb.config import load_settings
    from polyarb.engine.backtest import (
        format_ledger_summary,
        format_shadow_summary,
        format_summary,
        summarize,
        summarize_ledger,
        summarize_shadow_arrivals,
    )
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()
    store = SqliteStore(settings.sqlite_path)
    try:
        opps = store.recent(limit)
        events = store.events()
        shadow = store.shadow_events()
    finally:
        store.close()
    typer.echo(format_summary(summarize(opps)))
    typer.echo("")
    typer.echo(format_ledger_summary(summarize_ledger(events)))
    typer.echo("")
    typer.echo(format_shadow_summary(summarize_shadow_arrivals(shadow)))


@app.command()
def settle(
    batch: int = typer.Option(500, help="Max pending ledger events to check this run."),
) -> None:
    """Poll Gamma (read-only) for the resolution of pending ledger events; record realized P&L."""
    import asyncio

    from polyarb.clients.gamma import GammaClient
    from polyarb.config import load_settings
    from polyarb.engine.settlement import SettlementRun, poll_settlements
    from polyarb.sinks.notify import build_notifier
    from polyarb.sinks.store import SqliteStore

    settings = load_settings()

    async def _run() -> SettlementRun:
        store = SqliteStore(settings.sqlite_path)
        notifier = build_notifier(settings.notifier, settings.notifier_url)
        try:
            async with GammaClient() as gamma:
                return await poll_settlements(store, gamma, notifier=notifier, batch_limit=batch)
        finally:
            await notifier.aclose()
            store.close()

    result = asyncio.run(_run())
    typer.echo(
        f"settle: checked={result.checked} settled={result.settled} void={result.void} "
        f"pending={result.still_pending} alerted={result.alerted}"
    )


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
    """Exit 0 if the scanner is alive; exit 1 if stale/missing (Docker HEALTHCHECK).

    Checks the **scan heartbeat** (HEARTBEAT_PATH, D7) — the loop pulsed within
    ``max(2 * SCAN_INTERVAL_SECONDS, 120)`` s — and, when streaming is the active path
    (STREAMING_ENABLED with WS_HEARTBEAT_PATH set), also the **WS heartbeat** (R8): the book
    cache was refreshed by a WS message or a successful resync within ``max(2 *
    WS_RESYNC_INTERVAL_S, 120)`` s. A frozen cache (both WS and resync wedged) therefore fails the
    check even while the scan loop keeps pulsing off stale books. Missing / unreadable / stale
    file → non-zero exit with a diagnostic on stderr. Side-effect-free (read-only).
    """
    from pathlib import Path

    from polyarb.config import load_settings
    from polyarb.engine import heartbeat

    settings = load_settings()

    def _check(path: Path | None, label: str, window: float) -> str:
        """Return an OK summary, or echo a diagnostic and exit non-zero."""
        if path is None:
            typer.echo(f"{label} path is not configured — set it in the environment", err=True)
            raise typer.Exit(code=1)
        try:
            age = heartbeat.age(path)
        except FileNotFoundError:
            typer.echo(f"{label} file not found: {path}", err=True)
            raise typer.Exit(code=1) from None
        except Exception as exc:
            typer.echo(f"{label} file unreadable ({path}): {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if age > window:
            typer.echo(
                f"{label} stale: {age:.1f}s old (window={window:.0f}s)",
                err=True,
            )
            raise typer.Exit(code=1)
        return f"{label} {age:.1f}s old"

    parts = [
        _check(
            settings.heartbeat_path,
            "scan heartbeat",
            max(2.0 * settings.scan_interval_seconds, 120.0),
        )
    ]
    # Streaming is the default path, so its liveness is required: when enabled, the WS heartbeat
    # must be configured AND fresh. _check fails loud on an unset path (a misconfiguration — a
    # frozen cache would otherwise read healthy), matching the docstring.
    if settings.streaming_enabled:
        parts.append(
            _check(
                settings.ws_heartbeat_path,
                "ws heartbeat",
                max(2.0 * settings.ws_resync_interval_s, 120.0),
            )
        )
    typer.echo("ok: " + ", ".join(parts))


if __name__ == "__main__":
    app()
