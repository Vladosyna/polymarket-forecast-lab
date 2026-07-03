"""Phase 2 historical bootstrap.

Primary path (brief v1.6/v1.7 section 3): download two files from the HF dataset
`SII-WANGZJ/Polymarket_data` -- `quant.parquet` (~21-27 GB; cleaned trades
unified to the YES token) and `markets.parquet` (~85-165 MB; metadata + final
outcomes) -- and lazy-filter `quant.parquet` with `polars.scan_parquet(...)` down
to resolved binary markets, joined to `markets.parquet`. Each (daily) price point
becomes one (p_market, outcome, days_to_resolution) observation. The trade file is
NEVER loaded fully into memory: the pipeline is lazy + streaming.

Column-name assumption (guardrail 1): the exact quant.parquet schema is not
pinned by the brief, so column roles (market key / price / timestamp) are
detected from a candidate list at run time and the pipeline raises a clear error
if it cannot map them -- it fails loudly rather than silently producing garbage.

Fallback path (`source="clob"`): download only `markets.parquet` and fetch daily
price paths for a top-volume sample from the public CLOB `prices-history`
endpoint. Retained because the quant file is large; useful for a quick bootstrap.

Categories come from `markets.parquet` where present; otherwise 'unknown' (M1
fits are horizon-bucketed, not category-bucketed, so this only limits M2).
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

# Candidate column names for detecting roles in quant.parquet (assumption per
# guardrail 1 -- resolved against the actual schema at run time).
_MARKET_KEY_CANDIDATES = ("condition_id", "conditionId", "market", "market_id",
                          "marketId", "fpmm", "fpmmAddress", "market_address", "id")
_PRICE_CANDIDATES = ("price", "p", "p_yes", "yes_price", "priceYes", "mid",
                     "outcome_price", "yesPrice")
_TS_CANDIDATES = ("timestamp", "t", "ts", "time", "datetime", "date",
                  "block_timestamp", "createdAt", "blockTimestamp")


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


def download_quant_parquet(config: dict[str, Any]) -> Path:
    """Download the ~21-27 GB cleaned-trades file (brief section 3 fetch code)."""
    from huggingface_hub import hf_hub_download

    log.info("bootstrap: downloading quant.parquet (~21-27 GB) -- this is large")
    path = hf_hub_download(
        repo_id=HF_REPO,
        filename="quant.parquet",
        repo_type="dataset",
        local_dir=bootstrap_dir(config),
    )
    return Path(path)


def _detect_column(schema_names: list[str], candidates: tuple[str, ...], role: str) -> str:
    """First candidate present in the schema (case-insensitive), else raise."""
    lower = {n.lower(): n for n in schema_names}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    raise ValueError(
        f"bootstrap: could not find a {role} column in quant.parquet; "
        f"looked for {candidates} among {schema_names}"
    )


def resolved_markets_metadata(markets_path: Path) -> pl.DataFrame:
    """Small per-market frame: condition_id, payout_yes, end_date, category.

    Only resolved binary (YES/NO) markets with an unambiguous 0/1 payout.
    """
    df = pl.read_parquet(markets_path)
    df = df.filter(
        (pl.col("closed") == 1)
        & (pl.col("answer1").str.to_lowercase() == "yes")
        & (pl.col("answer2").str.to_lowercase() == "no")
        & pl.col("end_date").is_not_null()
    )

    def payout(prices: str) -> float | None:
        try:
            p = [float(x) for x in ast.literal_eval(prices)]
        except (ValueError, TypeError, SyntaxError):
            return None
        return p[0] if len(p) == 2 and sorted(p) == [0.0, 1.0] else None

    df = df.with_columns(
        pl.col("outcome_prices").map_elements(payout, return_dtype=pl.Float64).alias("payout_yes")
    ).filter(pl.col("payout_yes").is_not_null())
    cols = {"condition_id", "payout_yes", "end_date"}
    if "category" in df.columns:
        cols.add("category")
    out = df.select(list(cols))
    if "category" not in out.columns:
        out = out.with_columns(pl.lit("unknown").alias("category"))
    return out.with_columns(pl.col("category").fill_null("unknown"))


def build_observations_from_quant(quant_path: Path, markets_path: Path,
                                  max_per_market: int | None = None) -> pl.DataFrame:
    """Lazy-scan quant.parquet -> daily (p_market, outcome, days_to_resolution).

    The trade file is never fully materialized: we scan lazily, filter prices to
    (0,1), join to the small resolved-markets frame (an inner join drops every
    unresolved/non-binary market), reduce to one price per market per day, and
    stream-collect. `max_per_market` optionally caps rows per market.
    """
    schema_names = pl.scan_parquet(quant_path).collect_schema().names()
    key_col = _detect_column(schema_names, _MARKET_KEY_CANDIDATES, "market-key")
    price_col = _detect_column(schema_names, _PRICE_CANDIDATES, "price")
    ts_col = _detect_column(schema_names, _TS_CANDIDATES, "timestamp")
    log.info("bootstrap: quant columns detected",
             extra={"ctx": {"key": key_col, "price": price_col, "ts": ts_col}})

    meta = resolved_markets_metadata(markets_path)
    if key_col != "condition_id" and "condition_id" in meta.columns:
        # The trade key is not literally 'condition_id'; join on matching values.
        meta = meta.rename({"condition_id": key_col}) if key_col not in meta.columns else meta

    lf = pl.scan_parquet(quant_path).select([
        pl.col(key_col).cast(pl.String).alias("market_key"),
        pl.col(price_col).cast(pl.Float64).alias("p_market"),
        pl.col(ts_col).alias("ts_raw"),
    ]).filter((pl.col("p_market") > 0.0) & (pl.col("p_market") < 1.0))

    meta_lf = meta.rename({key_col: "market_key"} if key_col in meta.columns else
                          {"condition_id": "market_key"}).lazy()
    meta_lf = meta_lf.with_columns([
        pl.col("market_key").cast(pl.String),
        pl.col("end_date").cast(pl.Datetime, strict=False).alias("end_dt"),
    ])

    joined = lf.join(meta_lf, on="market_key", how="inner")

    # Timestamps may be epoch seconds/ms or an ISO/datetime column.
    joined = joined.with_columns(_ts_to_datetime(pl.col("ts_raw")).alias("obs_dt"))
    joined = joined.with_columns(
        ((pl.col("end_dt") - pl.col("obs_dt")).dt.total_seconds() / 86400.0)
        .alias("days_to_resolution")
    ).filter(pl.col("days_to_resolution") > 0)

    daily = (joined
             .with_columns(pl.col("obs_dt").dt.date().alias("obs_date"))
             .group_by(["market_key", "obs_date"])
             .agg([
                 pl.col("p_market").last(),
                 pl.col("payout_yes").first().alias("outcome"),
                 pl.col("category").first(),
                 pl.col("days_to_resolution").last(),
             ]))

    df = daily.collect(streaming=True)
    out = df.select([
        pl.col("market_key").alias("condition_id"),
        pl.col("category"),
        pl.col("p_market"),
        pl.col("outcome"),
        pl.col("days_to_resolution"),
    ])
    if max_per_market is not None:
        out = (out.sort("days_to_resolution")
               .group_by("condition_id").head(max_per_market))
    return out.select(list(OBS_SCHEMA)).cast(OBS_SCHEMA)


def _ts_to_datetime(col: pl.Expr) -> pl.Expr:
    """Best-effort conversion of a timestamp column to Datetime.

    Handles epoch seconds and milliseconds (integer/float) and already-typed or
    string datetimes. Ambiguity is resolved by magnitude (ms values are ~1e12).
    """
    as_int = col.cast(pl.Int64, strict=False)
    epoch_s = (as_int * 1_000_000).cast(pl.Datetime("us"))          # seconds -> us
    epoch_ms = (as_int * 1_000).cast(pl.Datetime("us"))             # millis  -> us
    from_str = col.cast(pl.Datetime, strict=False)
    return (pl.when(as_int.is_not_null() & (as_int.abs() > 1_000_000_000_000))
            .then(epoch_ms)
            .when(as_int.is_not_null() & (as_int.abs() > 1_000_000_000))
            .then(epoch_s)
            .otherwise(from_str))


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


def run_bootstrap_hf(config: dict[str, Any], max_per_market: int | None = None) -> Path:
    """Primary path: quant.parquet + markets.parquet, lazy-filtered (brief section 3)."""
    out_path = bootstrap_dir(config) / "observations.parquet"
    markets_path = download_markets_parquet(config)
    quant_path = download_quant_parquet(config)
    obs = build_observations_from_quant(quant_path, markets_path, max_per_market=max_per_market)
    obs.write_parquet(out_path)
    log.info("bootstrap observations written (hf/quant)",
             extra={"ctx": {"path": str(out_path), "rows": len(obs)}})
    return out_path


async def run_bootstrap_clob(config: dict[str, Any], sample_size: int = 2000,
                             min_volume: float = 10000.0) -> Path:
    """Fallback path: markets.parquet + CLOB prices-history for a top-volume sample."""
    out_path = bootstrap_dir(config) / "observations.parquet"
    markets_path = download_markets_parquet(config)
    sample = resolved_binary_sample(markets_path, sample_size, min_volume)
    log.info("bootstrap sample selected", extra={"ctx": {"markets": len(sample)}})
    obs = await fetch_observations(sample, config)
    obs.write_parquet(out_path)
    log.info("bootstrap observations written (clob)",
             extra={"ctx": {"path": str(out_path), "rows": len(obs)}})
    return out_path


async def run_bootstrap(config: dict[str, Any], source: str = "hf",
                        sample_size: int = 2000, min_volume: float = 10000.0,
                        max_per_market: int | None = None) -> Path:
    """End-to-end bootstrap. `source="hf"` (default) uses quant.parquet; the brief
    pins this method. `source="clob"` keeps the lighter CLOB-live fallback (no
    21-27 GB download) for a quick start.
    """
    if source == "hf":
        # Sync HF path; run off the event loop so the signature stays awaitable.
        import asyncio
        return await asyncio.to_thread(run_bootstrap_hf, config, max_per_market)
    if source == "clob":
        return await run_bootstrap_clob(config, sample_size, min_volume)
    raise ValueError(f"unknown bootstrap source: {source!r} (expected 'hf' or 'clob')")


def load_observations(config: dict[str, Any]) -> pl.DataFrame:
    return pl.read_parquet(bootstrap_dir(config) / "observations.parquet")
