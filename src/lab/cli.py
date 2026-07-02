"""`lab` CLI skeleton (Phase 0).

Commands are wired to their implementations phase by phase; until then each
prints a clear not-implemented notice and exits non-zero so cron jobs fail
loudly rather than silently succeeding.
"""

from __future__ import annotations

import asyncio

import typer

from lab.util import load_config, setup_logging

app = typer.Typer(
    name="lab",
    help="Polymarket Forecast Lab -- read-only forecasting research instrument.",
    no_args_is_help=True,
)


def _not_implemented(command: str, phase: str) -> None:
    typer.secho(
        f"`lab {command}` is not implemented yet (arrives in {phase}).",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=2)


@app.callback()
def main() -> None:
    """Initialize config and logging for every command."""
    setup_logging(load_config())


@app.command()
def sync() -> None:
    """Discover markets from Gamma and update the universe with tiering."""
    from lab.api.gamma import GammaClient
    from lab.api.http import TokenBucket
    from lab.collect.universe import sync_universe
    from lab.store import db

    config = load_config()

    async def _run() -> dict:
        bucket = TokenBucket(
            rate=config["collect"]["rate_limit"]["requests_per_second"],
            burst=config["collect"]["rate_limit"]["burst"],
        )
        gamma = GammaClient(bucket)
        conn = db.connect(config["storage"]["db_path"])
        try:
            return await sync_universe(gamma, conn, config)
        finally:
            await gamma.aclose()
            conn.close()

    counts = asyncio.run(_run())
    typer.echo(f"universe sync: {counts}")


@app.command()
def collect() -> None:
    """Run the long-lived collection process (snapshots + resolution watcher)."""
    from lab.collect.runner import run_collect

    asyncio.run(run_collect(load_config()))


@app.command()
def forecast() -> None:
    """Generate forecasts for the eligible universe and freeze them in the ledger."""
    _not_implemented("forecast", "Phase 3")


@app.command()
def eval() -> None:
    """Score resolved forecasts: paired Brier/log-loss, skill with bootstrap CIs."""
    _not_implemented("eval", "Phase 3")


@app.command()
def report() -> None:
    """Render the static HTML report from evaluation results."""
    _not_implemented("report", "Phase 3")


@app.command()
def shadow() -> None:
    """Run the simulated shadow portfolio (SIMULATION only, no real money)."""
    _not_implemented("shadow", "Phase 6")


@app.command()
def export() -> None:
    """Emit latest forecast per (market, model) as JSONL -- the downstream integration point."""
    _not_implemented("export", "Phase 3")


@app.command()
def status() -> None:
    """Data health: snapshot freshness, gaps, watcher lag, ledger counts, LLM spend."""
    from lab.collect.status import format_status, gather_status

    typer.echo(format_status(gather_status(load_config())))


@app.command()
def learn() -> None:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems."""
    _not_implemented("learn", "Phase 7")


if __name__ == "__main__":
    app()
