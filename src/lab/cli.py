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
    from dotenv import load_dotenv

    load_dotenv()
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
def run() -> None:
    """One-button orchestrator: collector + scheduled forecast/eval/report/shadow/learn."""
    from lab.collect.runner import run_orchestrator

    typer.echo("Forecast Lab orchestrator starting (Ctrl+C to stop)...")
    asyncio.run(run_orchestrator(load_config()))


@app.command()
def forecast() -> None:
    """Generate forecasts for the eligible universe and freeze them in the ledger."""
    from lab.jobs import run_forecast_job

    counts = run_forecast_job(load_config())
    typer.echo(f"forecast run: {counts}")


@app.command()
def eval() -> None:
    """Score resolved forecasts: paired Brier/log-loss, skill with bootstrap CIs."""
    from lab.jobs import run_eval_job

    summaries = run_eval_job(load_config())
    if not summaries:
        typer.echo("eval: no resolved paired forecasts yet (INSUFFICIENT DATA)")
    for s in summaries:
        r = s["result"]
        typer.echo(
            f"  {s['model_id']} [{s['window']}] n={r.n} skill={r.skill:+.4f} "
            f"CI=[{r.skill_ci_lo:+.4f},{r.skill_ci_hi:+.4f}] mde={r.mde:.4f}"
        )


@app.command()
def report() -> None:
    """Render the static HTML report from evaluation results."""
    from lab.jobs import run_report_job

    path = run_report_job(load_config())
    typer.echo(f"report: {path}")


@app.command()
def shadow() -> None:
    """Run the simulated shadow portfolio (SIMULATION only, no real money)."""
    from lab.jobs import run_shadow_job

    result = run_shadow_job(load_config())
    typer.echo(f"shadow (SIMULATION): opened={result['opened']} settled={result['settled']}")
    typer.echo(f"  {result['summary']}")


@app.command()
def export(
    out: str = typer.Option(None, help="Output file; stdout when omitted."),
) -> None:
    """Emit latest forecast per (market, model) as JSONL -- the downstream integration point."""
    from lab.export import export_jsonl
    from lab.store import db

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    try:
        lines = list(export_jsonl(conn))
    finally:
        conn.close()
    if out:
        from pathlib import Path

        Path(out).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        typer.echo(f"export: {len(lines)} rows -> {out}")
    else:
        for line in lines:
            typer.echo(line)


@app.command()
def status() -> None:
    """Data health: snapshot freshness, gaps, watcher lag, ledger counts, LLM spend."""
    from lab.collect.status import format_status, gather_status

    typer.echo(format_status(gather_status(load_config())))


@app.command()
def bootstrap(
    sample_size: int = typer.Option(2000, help="Top-volume resolved markets to fetch price paths for."),
    min_volume: float = typer.Option(10000.0, help="Minimum lifetime volume (USD) for the sample."),
    skip_fetch: bool = typer.Option(False, help="Reuse existing observations.parquet; refit only."),
) -> None:
    """Phase 2: historical bootstrap -- download resolved markets, fit M1/M2 artifacts."""
    from lab.learn import bootstrap as bs
    from lab.learn.plots import plot_m1_curves
    from lab.learn.refit import fit_m1_curves, fit_m2_baserates, save_artifact

    config = load_config()
    if not skip_fetch:
        asyncio.run(bs.run_bootstrap(config, sample_size=sample_size, min_volume=min_volume))
    obs = bs.load_observations(config)
    typer.echo(f"observations: {len(obs)} rows / {obs['condition_id'].n_unique()} markets")

    m1 = fit_m1_curves(obs.to_dicts())
    save_artifact(config, "m1_curves", m1)
    for name, fit in m1["buckets"].items():
        typer.echo(f"  m1 {name}: alpha={fit['alpha']:.3f} beta={fit['beta']:.3f} n={fit['n']}")

    per_market = obs.group_by("condition_id").first().select("category", "outcome")
    m2 = fit_m2_baserates(per_market.to_dicts())
    save_artifact(config, "m2_baserates", m2)
    typer.echo(f"  m2 base rates: {len(m2['categories'])} categories")

    for path in plot_m1_curves(m1, config):
        typer.echo(f"  plot: {path}")


@app.command()
def learn() -> None:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems."""
    from lab.jobs import run_learn_job

    summary = run_learn_job(load_config())
    typer.echo(f"learn: {summary}")


@app.command()
def ps() -> None:
    """List our own running instances; flag outdated code versions and duplicates."""
    import time

    from lab import process_guard

    config = load_config()
    snap = process_guard.report(config)
    typer.echo(f"current code version: {snap['current_version']}")
    typer.echo("managed instances:")
    if not snap["managed"]:
        typer.echo("  (none registered)")
    for e in snap["managed"]:
        age_min = (time.time() - (e.get("start_ts") or time.time())) / 60
        flags = []
        if e.get("pid") in snap["flagged_pids"]:
            flags.append("REDUNDANT/OUTDATED")
        if e.get("code_version") != snap["current_version"]:
            flags.append("stale-version")
        tag = f"  [{' '.join(flags)}]" if flags else ""
        typer.echo(
            f"  {e.get('role'):12} pid={e.get('pid'):<7} ver={e.get('code_version')} "
            f"age={age_min:.0f}min{tag}"
        )
    if snap["unmanaged"]:
        typer.echo("unmanaged lab-looking processes (not registered; consider stopping):")
        for e in snap["unmanaged"]:
            age_min = (time.time() - (e.get("start_ts") or time.time())) / 60
            typer.echo(f"  {e.get('role'):12} pid={e.get('pid'):<7} age={age_min:.0f}min")


if __name__ == "__main__":
    app()
