from __future__ import annotations

from pathlib import Path

import pandas as pd

from config_channel_v4 import DATA_FILE_15M, END_DATE, START_DATE


REQUIRED_COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def load_15m_data(csv_path: Path | None = None) -> pd.DataFrame:
    path = csv_path or DATA_FILE_15M
    df = pd.read_csv(path)
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    df = df[REQUIRED_COLUMNS].copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=False)
    df = df.sort_values("open_time").drop_duplicates(subset=["open_time"]).reset_index(drop=True)
    df = df[(df["open_time"] >= pd.Timestamp(START_DATE)) & (df["open_time"] <= pd.Timestamp(END_DATE))].reset_index(drop=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df["bar_index"] = range(len(df))
    return df


def validate_15m_data(df: pd.DataFrame) -> dict:
    if df.empty:
        raise ValueError("15m data is empty after filtering")

    spacing = df["open_time"].diff().dropna()
    expected = pd.Timedelta(minutes=15)
    bad_spacing = int((spacing != expected).sum())

    return {
        "rows": int(len(df)),
        "start": df["open_time"].iloc[0].strftime("%Y-%m-%d %H:%M:%S"),
        "end": df["open_time"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "bad_spacing_count": bad_spacing,
    }
