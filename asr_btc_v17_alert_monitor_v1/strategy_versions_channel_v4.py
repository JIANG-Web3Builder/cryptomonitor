from __future__ import annotations

from config_channel_v4 import FEATURES_BY_VERSION


def get_version_features(version: str):
    if version not in FEATURES_BY_VERSION:
        raise KeyError(f"Unsupported version: {version}")
    return FEATURES_BY_VERSION[version]
