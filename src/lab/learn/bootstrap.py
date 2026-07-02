"""Phase 2 historical bootstrap.

Slice strategy (assumption, stated per guardrail 1): the brief's HF dataset
ships trades as a 28 GB file; the lab only needs (price at horizon, outcome)
pairs. We download just `markets.parquet` (~85 MB) for resolved binary
markets + final outcomes, then fetch daily price paths for a top-volume
sample from the public CLOB `prices-history` endpoint. Each daily price point
becomes one (p_market, outcome, days_to_resolution) observation.

Categories are backfilled from Gamma metadata where available; markets whose
category Gamma no longer serves are labeled 'unknown' (M1 fits are horizon-
bucketed, not category-bucketed, so this only limits M2 coverage).
"""

from __future__ import annotations

import ast
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from lab.api.clob import ClobClient
from lab.api.gamma import GammaClient
from lab.api.http import TokenBucket
from lab.util import PROJECT_ROOT

log = logging.getLogger(__name__)

HF_REPO = "SII-WANGZJ/Polymarket_data"
OBS_SCHEMA = {
    "condition_id": pl.String,
    "category": pl.String,
    "p_market": pl.Float64,
    "outcome": pl.Float64,
    "days_to_resolution": pl.Float64,
}


def bootstrap_dir(config: dict[str, Any]) -> Path:
    d = PROJECT_ROOT / "data" / "bootstrap"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_markets_parquet(config: dict[str, Any]) -> Path:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        repo_id=HF_REPO,
        filename="markets.parquet",
        repo_type="dataset",
        local_dir=bootstrap_dir(config),
    )
    return Path(path)


def resolved_binary_sample(markets_path: Path, sample_size: int,
                           min_volume: float) -> pl.DataFrame:
    """Top-volume resolved binary markets with an unambiguous final payout."""
    df = pl.read_parquet(markets_path)
    df = df.filter(
        (pl.col("closed") == 1)
        & (pl.col("answer1").str.to_lowercase() == "yes")
        & (pl.col("answer2").str.to_lowercase() == "no")
        & pl.col("end_date").is_not_null()
        & (pl.col("volume") >= min_volume)
    )

    def payout(prices: str) -> float | None:
        # Dataset stores Python-repr lists ("['0', '1']"), not JSON.
        try:
            p = [float(x) for x in ast.literal_eval(prices)]
        except (ValueError, TypeError, SyntaxError):
            return None
        if len(p) == 2 and sorted(p) == [0.0, 1.0]:
            return p[0]
        return None

    df = df.with_columns(
        pl.col("outcome_prices").map_elements(payout, return_dtype=pl.Float64).alias("payout_yes")
    ).filter(pl.col("payout_yes").is_not_null())
    return df.sort("volume", descending=True).head(sample_size)


async def fetch_observations(sample: pl.DataFrame, config: dict[str, Any]) -> pl.DataFrame:
    """Daily price paths from CLOB for each sampled market -> observations."""
    bucket = TokenBucket(
        rate=config["collect"]["rate_limit"]["requests_per_second"],
        burst=config["collect"]["rate_limit"]["burst"],
    )
    clob = ClobClient(bucket)
    gamma = GammaClient(bucket)
    rows: list[dict] = []
    categories: dict[str, str] = {}
    try:
        for m in sample.iter_rows(named=True):
            cid = m["condition_id"]
            end = m["end_date"]
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            try:
                history = await clob.prices_history(m["token1"], interval="max", fidelity=1440)
            except Exception:
                log.warning("bootstrap: prices-history failed", extra={"ctx": {"condition_id": cid}})
                continue
            if cid not in categories:
                try:
                    gm = await gamma.market_by_condition(cid)
                    categories[cid] = (gm.category or "unknown").lower() if gm else "unknown"
                except Exception:
                    categories[cid] = "unknown"
            for point in history:
                ts = datetime.fromtimestamp(point["t"], tz=timezone.utc)
                days = (end - ts).total_seconds() / 86400
                p = float(point["p"])
                if days <= 0 or not (0.0 < p < 1.0):
                    continue
                rows.append({
                    "condition_id": cid,
                    "category": categories[cid],
                    "p_market": p,
                    "outcome": float(m["payout_yes"]),
                    "days_to_resolution": days,
                })
    finally:
        await clob.aclose()
        await gamma.aclose()
    return pl.DataFrame(rows, schema=OBS_SCHEMA)


async def run_bootstrap(config: dict[str, Any], sample_size: int = 2000,
                        min_volume: float = 10000.0) -> Path:
    """End-to-end: download, sample, fetch paths, persist observations."""
    out_path = bootstrap_dir(config) / "observations.parquet"
    markets_path = download_markets_parquet(config)
    sample = resolved_binary_sample(markets_path, sample_size, min_volume)
    log.info("bootstrap sample selected", extra={"ctx": {"markets": len(sample)}})
    obs = await fetch_observations(sample, config)
    obs.write_parquet(out_path)
    log.info("bootstrap observations written",
             extra={"ctx": {"path": str(out_path), "rows": len(obs)}})
    return out_path


def load_observations(config: dict[str, Any]) -> pl.DataFrame:
    return pl.read_parquet(bootstrap_dir(config) / "observations.parquet")
