"""Display-only imagery post-processing for NATO fallback twins.

The vegetation engine reads ``imagery/naip_rgb.png`` and
``imagery/false_color.png`` on the same linear byte scale while analysis runs.
This module is intentionally pack-side and should only be invoked after
``scripts/analyze_vegetation.py`` has finished.  It rewrites the visible RGB
display files and leaves ``false_color.png`` untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

NODATA = 255
DISPLAY_LOW = 5.0
DISPLAY_HIGH = 225.0
LOW_PERCENTILE = 2.0
HIGH_PERCENTILE = 98.0
HIGH_PERCENTILE_CANDIDATES = (98.0, 96.0, 94.0, 92.0, 90.0, 88.0, 85.0, 82.0, 80.0)
GAMMA = 0.85
GAMMA_CANDIDATES = (0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55)
TARGET_MEAN = 100.0
TARGET_MEAN_LOW = 85.0
TARGET_MEAN_HIGH = 135.0
TARGET_P98_LOW = 185.0
TARGET_P98_HIGH = 225.0
MIN_VALID_PIXELS = 64
MIN_STRETCH_RANGE = 4.0
MAX_SAMPLE_PIXELS = 1_000_000
VERSION = 1


@dataclass(frozen=True)
class DisplayStretch:
    low: list[float]
    high: list[float]
    low_percentile: float = LOW_PERCENTILE
    high_percentile: float = HIGH_PERCENTILE
    gamma: float = GAMMA
    display_low: float = DISPLAY_LOW
    display_high: float = DISPLAY_HIGH


def apply_display_stretch(data_dir: str, force: bool = False) -> dict[str, Any]:
    """Stretch ``naip_rgb.png`` and ``drape.png`` for display after analysis.

    The stretch is computed from valid visible RGB pixels in ``naip_rgb.png``
    only.  Pixels with any RGB channel equal to 255 are treated as nodata and
    kept at 255 in the rewritten display files.  ``false_color.png`` is never
    opened for writing.
    """
    imagery_dir = os.path.join(data_dir, "imagery")
    rgb_path = os.path.join(imagery_dir, "naip_rgb.png")
    drape_path = os.path.join(imagery_dir, "drape.png")
    meta_path = os.path.join(imagery_dir, "display_stretch.json")
    for path in (rgb_path, drape_path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)

    before_sha = {name: _sha1(path) for name, path in _display_paths(rgb_path, drape_path)}
    if not force and os.path.exists(meta_path):
        try:
            old = json.load(open(meta_path))
            old_after = old.get("after_sha1") or {}
            if old.get("version") == VERSION and all(
                old_after.get(name) == digest for name, digest in before_sha.items()
            ):
                old["skipped"] = "already_applied"
                return old
        except (OSError, json.JSONDecodeError):
            pass

    rgb_arr, rgb_mode = _read_rgb(rgb_path)
    valid = _valid_rgb_mask(rgb_arr)
    before_metrics = {
        "naip_rgb": image_metrics(rgb_path),
        "drape": image_metrics(drape_path),
    }
    if int(valid.sum()) < MIN_VALID_PIXELS:
        result = {
            "version": VERSION,
            "status": "skipped",
            "reason": "not enough valid visible RGB pixels",
            "valid_pixels": int(valid.sum()),
            "before": before_metrics,
            "before_sha1": before_sha,
            "false_color_touched": False,
            "fetched_at": _utcnow(),
        }
        _write_json(meta_path, result)
        return result

    vals = rgb_arr[:, :, :3][valid].astype(np.float32)
    stretch = _choose_display_stretch(vals)
    ranges = np.asarray(stretch.high) - np.asarray(stretch.low)
    if float(np.nanmax(ranges)) < MIN_STRETCH_RANGE:
        result = {
            "version": VERSION,
            "status": "skipped",
            "reason": "visible RGB percentile range is too small",
            "valid_pixels": int(valid.sum()),
            "stretch": {
                "low": [round(float(v), 3) for v in stretch.low],
                "high": [round(float(v), 3) for v in stretch.high],
            },
            "before": before_metrics,
            "before_sha1": before_sha,
            "false_color_touched": False,
            "fetched_at": _utcnow(),
        }
        _write_json(meta_path, result)
        return result

    for path in (rgb_path, drape_path):
        arr, mode = _read_rgb(path)
        out = _stretch_rgb(arr, stretch)
        _write_image(path, out, mode)
        _remove_png_aux(path)

    after_sha = {name: _sha1(path) for name, path in _display_paths(rgb_path, drape_path)}
    result = {
        "version": VERSION,
        "status": "applied",
        "params": {
            "low_percentile": stretch.low_percentile,
            "high_percentile": stretch.high_percentile,
            "display_low": DISPLAY_LOW,
            "display_high": DISPLAY_HIGH,
            "gamma": stretch.gamma,
            "nodata": NODATA,
            "target_mean": TARGET_MEAN,
            "target_mean_range": [TARGET_MEAN_LOW, TARGET_MEAN_HIGH],
            "target_p98_range": [TARGET_P98_LOW, TARGET_P98_HIGH],
        },
        "stretch": {
            "low": [round(float(v), 3) for v in stretch.low],
            "high": [round(float(v), 3) for v in stretch.high],
        },
        "before": before_metrics,
        "after": {
            "naip_rgb": image_metrics(rgb_path),
            "drape": image_metrics(drape_path),
        },
        "before_sha1": before_sha,
        "after_sha1": after_sha,
        "false_color_touched": False,
        "fetched_at": _utcnow(),
    }
    _write_json(meta_path, result)
    return result


def image_metrics(path: str) -> dict[str, Any]:
    arr, _mode = _read_rgb(path)
    rgb = arr[:, :, :3]
    valid = _valid_rgb_mask(arr)
    total = int(valid.size)
    valid_count = int(valid.sum())
    if valid_count == 0:
        return {
            "width": int(arr.shape[1]),
            "height": int(arr.shape[0]),
            "valid_pct": 0.0,
            "mean": [0.0, 0.0, 0.0],
            "p98": [0.0, 0.0, 0.0],
        }
    vals = rgb[valid].astype(np.float32)
    return {
        "width": int(arr.shape[1]),
        "height": int(arr.shape[0]),
        "valid_pct": round(100.0 * valid_count / total, 3) if total else 0.0,
        "mean": [round(float(v), 2) for v in vals.mean(axis=0)],
        "p98": [round(float(v), 2) for v in np.percentile(vals, 98, axis=0)],
    }


def _choose_display_stretch(vals: np.ndarray) -> DisplayStretch:
    sample = _sample_values(vals)
    low = np.percentile(sample, LOW_PERCENTILE, axis=0)
    best = None
    for high_pct in HIGH_PERCENTILE_CANDIDATES:
        high = np.percentile(sample, high_pct, axis=0)
        high = np.where(high - low < MIN_STRETCH_RANGE, low + MIN_STRETCH_RANGE, high)
        for gamma in GAMMA_CANDIDATES:
            mean_avg, p98_avg = _preview_display_metrics(sample, low, high, gamma)
            score = (
                abs(mean_avg - TARGET_MEAN)
                + max(0.0, TARGET_MEAN_LOW - mean_avg) * 3.0
                + max(0.0, mean_avg - TARGET_MEAN_HIGH) * 2.0
                + max(0.0, TARGET_P98_LOW - p98_avg) * 0.5
                + max(0.0, p98_avg - TARGET_P98_HIGH) * 0.5
                + (HIGH_PERCENTILE - high_pct) * 0.2
                + abs(GAMMA - gamma)
            )
            candidate = {
                "score": score,
                "mean_avg": mean_avg,
                "p98_avg": p98_avg,
                "high_pct": high_pct,
                "gamma": gamma,
                "high": high,
            }
            if best is None or candidate["score"] < best["score"]:
                best = candidate
    if best is None:
        high = np.percentile(sample, HIGH_PERCENTILE, axis=0)
        high = np.where(high - low < MIN_STRETCH_RANGE, low + MIN_STRETCH_RANGE, high)
        return DisplayStretch(
            low=[float(v) for v in low],
            high=[float(v) for v in high],
        )
    return DisplayStretch(
        low=[float(v) for v in low],
        high=[float(v) for v in best["high"]],
        high_percentile=float(best["high_pct"]),
        gamma=float(best["gamma"]),
    )


def _sample_values(vals: np.ndarray) -> np.ndarray:
    if vals.shape[0] <= MAX_SAMPLE_PIXELS:
        return vals
    step = int(np.ceil(vals.shape[0] / MAX_SAMPLE_PIXELS))
    return vals[::step]


def _preview_display_metrics(vals: np.ndarray, low: np.ndarray, high: np.ndarray,
                             gamma: float) -> tuple[float, float]:
    span = np.maximum(high - low, MIN_STRETCH_RANGE)
    norm = np.clip((vals - low) / span, 0.0, 1.0)
    mapped = DISPLAY_LOW + ((DISPLAY_HIGH - DISPLAY_LOW) * np.power(norm, gamma))
    return float(mapped.mean()), float(np.percentile(mapped, 98))


def _display_paths(rgb_path: str, drape_path: str):
    return (("naip_rgb", rgb_path), ("drape", drape_path))


def _read_rgb(path: str) -> tuple[np.ndarray, str]:
    with Image.open(path) as image:
        mode = image.mode
        if mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
            mode = "RGB"
        return np.array(image, dtype=np.uint8), mode


def _valid_rgb_mask(arr: np.ndarray) -> np.ndarray:
    rgb_valid = np.all(arr[:, :, :3] != NODATA, axis=2)
    if arr.shape[2] >= 4:
        rgb_valid &= arr[:, :, 3] > 0
    return rgb_valid


def _stretch_rgb(arr: np.ndarray, stretch: DisplayStretch) -> np.ndarray:
    out = arr.copy()
    valid = _valid_rgb_mask(out)
    rgb = out[:, :, :3].astype(np.float32)
    low = np.asarray(stretch.low, dtype=np.float32)
    high = np.asarray(stretch.high, dtype=np.float32)
    span = np.maximum(high - low, MIN_STRETCH_RANGE)
    norm = np.clip((rgb - low) / span, 0.0, 1.0)
    mapped = stretch.display_low + (
        (stretch.display_high - stretch.display_low) * np.power(norm, stretch.gamma)
    )
    out[:, :, :3] = np.clip(np.rint(mapped), 0, NODATA - 1).astype(np.uint8)
    rgb_out = out[:, :, :3]
    rgb_out[~valid] = NODATA
    return out


def _write_image(path: str, arr: np.ndarray, mode: str) -> None:
    image = Image.fromarray(arr, mode=mode if mode in ("RGB", "RGBA") else "RGB")
    image.save(path)


def _write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def _remove_png_aux(path: str) -> None:
    aux = path + ".aux.xml"
    if os.path.exists(aux):
        os.remove(aux)


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
