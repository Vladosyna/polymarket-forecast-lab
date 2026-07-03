"""`lab` CLI skeleton (Phase 0).

Commands are wired to their implementations phase by phase; until then each
prints a clear not-implemented notice and exits non-zero so cron jobs fail
loudly rather than silently succeeding.
"""

from __future__ import annotations

import asyncio

import typer

from lab.util import load_config, setup_logging, use_stable_event_loop

use_stable_event_loop()

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
    source: str = typer.Option(
        "hf",
        help="'hf' = quant.parquet + markets.parquet (brief-pinned; ~21-27 GB download). "
             "'clob' = markets.parquet + CLOB prices-history (light fallback).",
    ),
    sample_size: int = typer.Option(2000, help="[clob] Top-volume resolved markets to fetch."),
    min_volume: float = typer.Option(10000.0, help="[clob] Minimum lifetime volume (USD)."),
    max_per_market: int = typer.Option(0, help="[hf] Cap observations per market (0 = no cap)."),
    skip_fetch: bool = typer.Option(False, help="Reuse existing observations.parquet; refit only."),
) -> None:
    """Phase 2: historical bootstrap -- download resolved markets, fit M1/M2 artifacts."""
    from lab.learn import bootstrap as bs
    from lab.learn.plots import plot_m1_curves
    from lab.learn.refit import fit_m1_curves, fit_m2_baserates, save_artifact

    config = load_config()
    if not skip_fetch:
        asyncio.run(bs.run_bootstrap(
            config, source=source, sample_size=sample_size, min_volume=min_volume,
            max_per_market=(max_per_market or None)))
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
def learn(
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Persist refits and promote/rollback versions. Default is a dry-run diff "
             "that writes nothing (brief section 6).",
    ),
) -> None:
    """Monthly learning loop: batch refits, champion/challenger, post-mortems.

    Dry-run by default: computes every proposed change and prints a diff without
    touching model_versions or ACTIVE.json. Pass --apply to commit.
    """
    from lab.jobs import run_learn_job

    summary = run_learn_job(load_config(), apply=apply)
    mode = "APPLIED" if apply else "DRY-RUN (nothing written; pass --apply to commit)"
    typer.echo(f"learn [{mode}]: {summary}")


@app.command()
def rollback(
    model_id: str = typer.Argument(..., help="Model key, e.g. m1_curves, m3_params, m4_weights."),
    to: str = typer.Option(None, "--to", help="Target version_tag (default: previous promotable)."),
) -> None:
    """Revert a model's active version to a prior one (manual override)."""
    from lab.jobs import run_rollback_job

    result = run_rollback_job(load_config(), model_id, to_version_tag=to)
    if result["restored"] is None:
        typer.secho(f"rollback: nothing to restore for {model_id}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rollback: {model_id} -> {result['restored']} (active)")


@app.command()
def guard(
    retire_outdated: bool = typer.Option(
        False,
        "--retire-outdated",
        help="Also stop a lone outdated instance (use before restarting after a code update).",
    ),
) -> None:
    """Stop redundant or unmanaged lab instances (orchestrator, collector, dashboard)."""
    from lab import process_guard

    result = process_guard.cleanup(load_config(), retire_sole_outdated=retire_outdated)
    stopped = result.get("stopped") or []
    if stopped:
        typer.echo(f"guard: stopped pids {stopped}")
    else:
        typer.echo("guard: nothing to stop")


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
            flags = []
            if e.get("pid") in snap["flagged_pids"]:
                flags.append("REDUNDANT/OUTDATED")
            tag = f"  [{' '.join(flags)}]" if flags else ""
            typer.echo(f"  {e.get('role'):12} pid={e.get('pid'):<7} age={age_min:.0f}min{tag}")


map_app = typer.Typer(help="Cross-venue question matching (M7, Phase 9): propose-then-confirm.")
app.add_typer(map_app, name="map")


@map_app.command("propose")
def map_propose() -> None:
    """LLM proposes candidate Kalshi matches for top liquid priority-category markets.

    Metaculus isn't reachable without an account (see api/metaculus.py) --
    use `lab map confirm --venue metaculus` for a hand-curated pair instead.
    """
    import asyncio as _asyncio

    from lab.api.http import TokenBucket
    from lab.api.kalshi import KalshiClient
    from lab.models.m7_crossvenue import propose_matches
    from lab.news.extract import create_llm_client
    from lab.store import db

    config = load_config()
    conn = db.connect(config["storage"]["db_path"])
    llm = create_llm_client(conn, config)
    if llm is None:
        typer.secho("map propose: no LLM configured (see llm.api_key_env in config.yaml)",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    async def _run():
        bucket = TokenBucket(rate=config["collect"]["rate_limit"]["requests_per_second"],
                             burst=config["collect"]["rate_limit"]["burst"])
        kalshi = KalshiClient(bucket)
        try:
            candidates = await kalshi.open_markets(limit=200)
            return propose_matches(conn, config, candidates, llm)
        finally:
            await kalshi.aclose()

    try:
        proposals = _asyncio.run(_run())
    finally:
        conn.close()
    typer.echo(f"map propose: {len(proposals)} new candidate(s) added to data/markets_map.yaml")
    for p in proposals:
        typer.echo(f"  {p['condition_id']} <-> kalshi:{p['external_id']} "
                   f"(confidence={p['confidence']:.2f}) {p['rationale']}")


@map_app.command("confirm")
def map_confirm(
    condition_id: str = typer.Argument(..., help="Polymarket condition_id."),
    venue: str = typer.Option(..., help="kalshi | metaculus"),
    external_id: str = typer.Option(
        None, help="Required if not already in `proposed` (e.g. a hand-found Metaculus pair)."),
) -> None:
    """Move a proposed pair into `confirmed` -- or confirm a hand-curated one directly."""
    from lab.models.m7_crossvenue import confirm_match, load_markets_map, save_markets_map

    data = load_markets_map()
    ok = confirm_match(data, condition_id, venue, external_id=external_id)
    if not ok:
        typer.secho(
            f"map confirm: no proposed ({condition_id}, {venue}) entry -- pass --external-id "
            "to confirm a hand-curated pair directly",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)
    save_markets_map(data)
    typer.echo(f"map confirm: {condition_id} <-> {venue} is now live for M7")


@map_app.command("list")
def map_list() -> None:
    """Show confirmed and proposed (awaiting human review) pairs."""
    from lab.models.m7_crossvenue import load_markets_map

    data = load_markets_map()
    typer.echo(f"confirmed ({len(data['confirmed'])}):")
    for e in data["confirmed"]:
        typer.echo(f"  {e['condition_id']} <-> {e['venue']}:{e['external_id']}")
    typer.echo(f"proposed, awaiting confirmation ({len(data['proposed'])}):")
    for e in data["proposed"]:
        typer.echo(f"  {e['condition_id']} <-> {e['venue']}:{e['external_id']} "
                   f"(confidence={e.get('confidence', 0):.2f})")


if __name__ == "__main__":
    app()
