"""`lab` CLI skeleton (Phase 0).

Commands are wired to their implementations phase by phase; until then each
prints a clear not-implemented notice and exits non-zero so cron jobs fail
loudly rather than silently succeeding.
"""

from __future__ import annotations

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
    _not_implemented("sync", "Phase 1")


@app.command()
def collect() -> None:
    """Run the long-lived collection process (snapshots + resolution watcher)."""
    _not_implemented("collect", "Phase 1")


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
    _not_implemented("status", "Phase 1")


@app.command()
def learn() -> None:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems."""
    _not_implemented("learn", "Phase 7")


if __name__ == "__main__":
    app()
