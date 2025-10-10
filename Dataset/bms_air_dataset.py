"""BMS (Beijing Multi-Site) air quality dataset utilities.

This module mirrors the public APIs exposed by :mod:`fin_dataset` so the rest of
the training pipeline can treat financial and air-quality data
interchangeably. In particular it produces the same compact cache layout
consisting of per-entity feature/target arrays accompanied by a global window
index. The resulting cache can therefore be consumed by the existing
ratio-split dataloader from :mod:`fin_dataset`, which also yields the familiar
``entity_mask`` metadata.

Targets ``Y`` correspond to future values of the ``PM2.5`` pollutant while the
context tensors (``V`` levels and ``T`` temporal first-differences) include the
full set of historical features *including* past ``PM2.5`` readings.
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

from ._normalization import NormalizationStatsAccumulator
from ._types import PathLike
from .fin_dataset import (
    CachePaths,
    load_dataloaders_with_ratio_split as _load_fin_ratio_split,
    rebuild_window_index_only as _rebuild_window_index_only,
)

# ---------------------------------------------------------------------------
# Public constants & configuration helpers

DATA_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/00501/"
    "PRSA2017_Data_20130301-20170228.zip"
)
ARCHIVE_ROOT = "PRSA_Data_20130301-20170228"
TARGET_COLUMN = "PM2.5"

# Default configuration parameters required by the training pipeline.
DEFAULT_SAMPLE_FREQ = "H"  # hourly resolution
MAX_LOOKBACK = 336  # maximum context window (K)
MAX_HORIZON = 168  # maximum forecasting horizon (H)

@dataclass
class BMSCacheConfig:
    """Configuration used when building the compact cache."""

    window: int = MAX_LOOKBACK
    horizon: int = MAX_HORIZON
    data_dir: PathLike = "./bms_air_cache"
    raw_data_dir: Optional[PathLike] = "./bms_air_quality_data"
    normalize_per_station: bool = True
    clamp_sigma: float = 5.0
    keep_time_meta: str = "end"  # "full" | "end" | "none"
    freq: str = DEFAULT_SAMPLE_FREQ
    stations: Optional[Sequence[str]] = None
    overwrite: bool = False


# ---------------------------------------------------------------------------
# Downloading & ingestion utilities

def download_bms_air_dataset(
    dest_path: PathLike = "./bms_air_quality_data",
    url: str = DATA_URL,
) -> Path:
    """Download and extract the raw dataset if it is not already present."""

    dest = Path(dest_path).expanduser().resolve()
    extracted = dest / ARCHIVE_ROOT
    if extracted.exists():
        return extracted

    dest.mkdir(parents=True, exist_ok=True)

    response = requests.get(url, stream=True, timeout=60)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        zf.extractall(dest)
    return extracted


def load_raw_bms_frames(data_dir: PathLike) -> pd.DataFrame:
    """Load every monitoring station CSV into a single dataframe."""

    root = Path(data_dir)
    csv_files = sorted(root.glob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(
            f"No CSV files found in '{root}'. Did you download the dataset?"
        )

    frames = []
    for csv in csv_files:
        df = pd.read_csv(csv)
        if "station" not in df.columns:
            raise ValueError(f"File '{csv}' does not contain the 'station' column.")
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _encode_wind_direction(series: pd.Series) -> pd.Series:
    """Convert cardinal wind direction strings into stable float codes."""

    codes = series.astype("category").cat.codes
    codes = codes.replace(-1, np.nan)
    return codes.astype(np.float32)


def clean_bms_data(df: pd.DataFrame) -> pd.DataFrame:
    """Perform light cleaning and station-wise forward-filling of the dataset."""

    df = df.copy()
    # Build a timestamp using the provided components.
    df["datetime"] = pd.to_datetime(df[["year", "month", "day", "hour"]])

    # Drop redundant index columns.
    df = df.drop(columns=["No", "year", "month", "day", "hour"], errors="ignore")

    numeric_cols = [
        "PM2.5",
        "PM10",
        "SO2",
        "NO2",
        "CO",
        "O3",
        "TEMP",
        "PRES",
        "DEWP",
        "RAIN",
        "WSPM",
    ]

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "wd" in df.columns:
        df["WD_CODE"] = _encode_wind_direction(df["wd"])
        df = df.drop(columns="wd")

    df = df.sort_values(["station", "datetime"])  # ensure chronological order

    fill_cols = [c for c in df.columns if c not in {"station", "datetime"}]
    df[fill_cols] = df.groupby("station", sort=False)[fill_cols].ffill()

    return df


def _build_station_frames(
    df: pd.DataFrame,
    freq: str = DEFAULT_SAMPLE_FREQ,
    stations: Optional[Sequence[str]] = None,
) -> Dict[str, pd.DataFrame]:
    """Transform the long dataframe into per-station panels aligned on time."""

    if stations is not None:
        df = df[df["station"].isin(set(stations))].copy()

    if df.empty:
        raise ValueError("No data available after applying station filters.")

    feature_cols = [c for c in df.columns if c not in {"station", "datetime"}]
    global_index = pd.date_range(df["datetime"].min(), df["datetime"].max(), freq=freq)

    panels: Dict[str, pd.DataFrame] = {}
    for station, group in df.groupby("station"):
        station_df = group.set_index("datetime")[feature_cols]
        station_df = station_df.reindex(global_index)
        station_df = station_df.ffill()
        station_df = station_df.dropna()
        if not station_df.empty:
            panels[str(station)] = station_df.astype(np.float32)

    if not panels:
        raise ValueError("All station panels became empty after reindexing.")

    return panels




# ---------------------------------------------------------------------------
# Cache builder

def prepare_bms_air_cache(cfg: BMSCacheConfig) -> Mapping[str, List[str]]:
    """Prepare the compact cache with context features and PM2.5 targets.

    The resulting directory layout matches :func:`fin_dataset.prepare_features_and_index_cache`.

    Returns
    -------
    Mapping[str, List[str]]
        Metadata describing the assets and feature columns that were persisted.
    """

    if cfg.window <= 0:
        raise ValueError("window must be a positive integer")
    if cfg.horizon < 0:
        raise ValueError("horizon must be non-negative")
    if cfg.window > MAX_LOOKBACK:
        raise ValueError(
            f"window must be <= {MAX_LOOKBACK} for the BMS air-quality dataset"
        )
    if cfg.horizon > MAX_HORIZON:
        raise ValueError(
            f"horizon must be <= {MAX_HORIZON} for the BMS air-quality dataset"
        )
    keep_time_meta = cfg.keep_time_meta.lower()
    if keep_time_meta not in {"full", "end", "none"}:
        raise ValueError("keep_time_meta must be one of {'full', 'end', 'none'}")

    raw_root = cfg.raw_data_dir or "./bms_air_quality_data"
    raw_path = download_bms_air_dataset(raw_root)
    raw_df = load_raw_bms_frames(raw_path)
    clean_df = clean_bms_data(raw_df)

    panels = _build_station_frames(clean_df, freq=cfg.freq, stations=cfg.stations)

    assets = sorted(panels.keys())
    asset_to_id = {asset: idx for idx, asset in enumerate(assets)}

    feature_cols = list(next(iter(panels.values())).columns)
    if TARGET_COLUMN not in feature_cols:
        raise ValueError(f"Target column '{TARGET_COLUMN}' missing from features.")
    # Ensure the target is the first column to make downstream inspection easier.
    feature_cols = [TARGET_COLUMN] + [c for c in feature_cols if c != TARGET_COLUMN]

    data_dir = Path(cfg.data_dir).expanduser().resolve()
    paths = CachePaths.from_dir(data_dir)
    if cfg.overwrite and paths.cache_root.exists():
        shutil.rmtree(paths.cache_root)
    paths.ensure()

    pairs: List[np.ndarray] = []
    end_times: List[np.ndarray] = []
    start_times: List[np.datetime64] = []
    stop_times: List[np.datetime64] = []

    # Normalization statistics accumulator.
    feature_dim = len(feature_cols)
    norm_acc = NormalizationStatsAccumulator(
        num_assets=len(assets),
        feature_dim=feature_dim,
        per_asset=cfg.normalize_per_station,
    )

    for asset in assets:
        aid = asset_to_id[asset]
        panel = panels[asset][feature_cols]
        total_rows = panel.shape[0]
        if total_rows < cfg.window + cfg.horizon:
            raise ValueError(
                f"Station '{asset}' has insufficient history for the requested window/horizon."
            )

        features = panel.to_numpy(dtype=np.float32, copy=True)
        targets = panel[TARGET_COLUMN].to_numpy(dtype=np.float32, copy=True)
        times = panel.index.to_numpy(dtype="datetime64[ns]")

        np.save(paths.features / f"{aid}.npy", features.astype(np.float16, copy=False))
        np.save(paths.targets / f"{aid}.npy", targets.astype(np.float16, copy=False))
        np.save(paths.times / f"{aid}.npy", times)

        norm_acc.update(aid, features, targets)

        max_start = total_rows - (cfg.window + cfg.horizon) + 1
        if max_start <= 0:
            continue
        starts = np.arange(0, max_start, dtype=np.int32)
        window_end_times = times[starts + cfg.window - 1].astype("datetime64[ns]")
        pairs.append(np.stack([np.full_like(starts, aid), starts], axis=1))
        end_times.append(window_end_times)
        start_times.append(times[0])
        stop_times.append(times[-1])

    if not pairs:
        raise RuntimeError("No valid context windows available across stations.")

    global_pairs = np.concatenate(pairs, axis=0).astype(np.int32)
    global_end_times = np.concatenate(end_times, axis=0).astype("datetime64[ns]")
    np.save(paths.windows / "global_pairs.npy", global_pairs)
    np.save(paths.windows / "end_times.npy", global_end_times)

    norm_stats = norm_acc.finalize(assets)

    dataset_start = min(start_times).astype("datetime64[s]") if start_times else None
    dataset_end = max(stop_times).astype("datetime64[s]") if stop_times else None

    meta = {
        "dataset": "bms_air_quality",
        "format": "indexcache_v1",
        "assets": assets,
        "asset2id": asset_to_id,
        "feature_cols": feature_cols,
        "target_col": TARGET_COLUMN,
        "window": int(cfg.window),
        "horizon": int(cfg.horizon),
        "max_window": MAX_LOOKBACK,
        "max_horizon": MAX_HORIZON,
        "keep_time_meta": keep_time_meta,
        "normalize_per_ticker": cfg.normalize_per_station,
        "clamp_sigma": cfg.clamp_sigma,
        "freq": cfg.freq or DEFAULT_SAMPLE_FREQ,
        "start": str(dataset_start) if dataset_start is not None else None,
        "end": str(dataset_end) if dataset_end is not None else None,
    }

    with paths.meta.open("w") as f:
        json.dump(meta, f, indent=2)

    with paths.norm_stats.open("w") as f:
        json.dump(norm_stats, f, indent=2)

    return {"assets": assets, "feature_cols": feature_cols}


# ---------------------------------------------------------------------------
# Loader wrappers

def load_bms_dataloaders_with_ratio_split(
    data_dir: PathLike,
    **loader_kwargs,
):
    """Wrapper around the financial ratio-split loader for BMS datasets."""

    return _load_fin_ratio_split(data_dir=data_dir, **loader_kwargs)


def _validate_bms_cache(paths: CachePaths) -> Tuple[Dict[str, object], bool]:
    """Read and validate the cache metadata, returning the parsed JSON.

    Returns
    -------
    Tuple[Dict[str, object], bool]
        The parsed metadata and a flag indicating whether additional keys were
        added during validation (meaning the metadata file should be rewritten).
    """

    if not paths.meta.exists():
        raise FileNotFoundError(
            f"Cache meta file not found at '{paths.meta}'. Did you run prepare_bms_air_cache()?"
        )

    with paths.meta.open("r") as f:
        meta: Dict[str, object] = json.load(f)

    dataset = meta.get("dataset")
    if dataset not in {"bms_air_quality", "bms_air_dataset"}:
        raise ValueError(
            f"The cache located at '{paths.cache_root}' does not correspond to the BMS "
            "air-quality dataset."
        )

    target_col = meta.get("target_col")
    needs_update = False
    if target_col is None:
        meta["target_col"] = TARGET_COLUMN
        needs_update = True
    elif target_col != TARGET_COLUMN:
        raise ValueError(
            "The prepared cache does not use PM2.5 as the prediction target."
        )

    freq = meta.get("freq", DEFAULT_SAMPLE_FREQ)
    if freq != DEFAULT_SAMPLE_FREQ:
        raise ValueError(
            "The BMS air-quality cache must use hourly sampling intervals."
        )
    if "freq" not in meta:
        meta["freq"] = DEFAULT_SAMPLE_FREQ
        needs_update = True

    max_window = int(meta.get("max_window", MAX_LOOKBACK))
    max_horizon = int(meta.get("max_horizon", MAX_HORIZON))
    if max_window > MAX_LOOKBACK or max_horizon > MAX_HORIZON:
        raise ValueError(
            "Cached maximum window/horizon exceed the supported (336, 168) hour limit."
        )
    if meta.get("max_window") != MAX_LOOKBACK:
        meta["max_window"] = MAX_LOOKBACK
        needs_update = True
    if meta.get("max_horizon") != MAX_HORIZON:
        meta["max_horizon"] = MAX_HORIZON
        needs_update = True

    return meta, needs_update


def run_experiment(
    data_dir: PathLike,
    K: Optional[int] = None,
    H: Optional[int] = None,
    *,
    ratios=(0.7, 0.1, 0.2),
    per_asset: bool = True,
    date_batching: bool = True,
    coverage: float = 0.85,
    dates_per_batch: int = 30,
    batch_size: int = 64,
    norm: str = "train_only",
    reindex: bool = True,
    shuffle_train: bool = True,
    num_workers: int = 0,
    pin_memory: Optional[bool] = None,
):
    """Build train/val/test loaders for the prepared BMS cache.

    This mirrors :func:`Dataset.fin_dataset.run_experiment` so the training
    pipeline can swap between the financial and BMS datasets transparently.
    """

    paths = CachePaths.from_dir(data_dir)
    meta, meta_needs_update = _validate_bms_cache(paths)
    max_window = int(meta.get("max_window", MAX_LOOKBACK))
    max_horizon = int(meta.get("max_horizon", MAX_HORIZON))
    cached_window = int(meta.get("window", max_window))
    cached_horizon = int(meta.get("horizon", max_horizon))

    if K is None:
        K = cached_window
    if H is None:
        H = cached_horizon

    if K > max_window or H > max_horizon:
        raise ValueError(
            "Requested (window, horizon) exceed the cached configuration. "
            "Re-run prepare_bms_air_cache with larger values first."
        )

    K = int(K)
    H = int(H)

    needs_update = K != cached_window or H != cached_horizon
    if reindex or needs_update:
        _rebuild_window_index_only(
            data_dir,
            window=K,
            horizon=H,
            update_meta=False,
            backup_old=False,
        )

    if needs_update:
        meta["window"] = K
        meta["horizon"] = H
        meta_needs_update = True

    if meta_needs_update:
        with paths.meta.open("w") as f:
            json.dump(meta, f, indent=2)

    train_dl, val_dl, test_dl, lengths = load_bms_dataloaders_with_ratio_split(
        data_dir=data_dir,
        train_ratio=ratios[0],
        val_ratio=ratios[1],
        test_ratio=ratios[2],
        batch_size=batch_size,
        regression=True,
        per_asset=per_asset,
        norm_scope=norm,
        date_batching=date_batching,
        coverage_per_window=coverage,
        dates_per_batch=dates_per_batch,
        window=K,
        horizon=H,
        shuffle_train=shuffle_train,
        num_workers=num_workers,
        pin_memory=pin_memory,
    )

    return train_dl, val_dl, test_dl, lengths


__all__ = [
    "BMSCacheConfig",
    "download_bms_air_dataset",
    "load_raw_bms_frames",
    "clean_bms_data",
    "prepare_bms_air_cache",
    "load_bms_dataloaders_with_ratio_split",
    "run_experiment",
]

