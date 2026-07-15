#!/usr/bin/env python3
"""Pure host-side obstacle speed limiter for body-velocity flight commands.

DJI Mini 4 Pro exposes directional obstacle distances through MSDK but does not
document firmware APAS/braking support while Virtual Stick owns authority.  This
module therefore implements a small deterministic last-line limiter that can be
unit-tested without an aircraft.  It never invents a clear path: callers choose
whether missing/stale sensing passes through, reports advisory-only, or stops
translation.
"""

import math
from dataclasses import dataclass
from enum import Enum


class AvoidanceMode(str, Enum):
    OFF = "off"
    ADVISORY = "advisory"
    BRAKE = "brake"


class MissingDataBehavior(str, Enum):
    PASS_THROUGH = "pass_through"
    STOP_TRANSLATION = "stop_translation"


@dataclass(frozen=True)
class ObstacleAvoidanceConfig:
    mode: AvoidanceMode = AvoidanceMode.ADVISORY
    minimum_clearance_m: float = 0.8
    reaction_time_s: float = 0.25
    maximum_deceleration_mps2: float = 2.0
    maximum_source_age_ms: float = 350.0
    missing_data_behavior: MissingDataBehavior = MissingDataBehavior.PASS_THROUGH

    def __post_init__(self):
        if not isinstance(self.mode, AvoidanceMode):
            raise TypeError("mode must be AvoidanceMode")
        if not isinstance(self.missing_data_behavior, MissingDataBehavior):
            raise TypeError("missing_data_behavior must be MissingDataBehavior")
        for name, value, allow_zero in (
            ("minimum_clearance_m", self.minimum_clearance_m, True),
            ("reaction_time_s", self.reaction_time_s, True),
            ("maximum_deceleration_mps2", self.maximum_deceleration_mps2, False),
            ("maximum_source_age_ms", self.maximum_source_age_ms, False),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be finite and {qualifier}")


@dataclass(frozen=True)
class ObstacleObservation:
    distances_m: dict
    source_updated_monotonic_ms: float


@dataclass(frozen=True)
class AvoidanceResult:
    forward_mps: float
    right_mps: float
    up_mps: float
    yaw_rate_deg_s: float
    mode: AvoidanceMode
    observation_fresh: bool
    limited_directions: tuple
    threat_directions: tuple
    missing_directions: tuple
    reason: str

    @property
    def translation_limited(self):
        return bool(self.limited_directions)


def limit_body_velocity(
    *,
    forward_mps,
    right_mps,
    up_mps,
    yaw_rate_deg_s,
    observation,
    now_monotonic_ms,
    config=ObstacleAvoidanceConfig(),
):
    """Limit only velocity components moving toward a sensed obstacle.

    The cap satisfies ``v*reaction + v²/(2*deceleration) <= clearance``.
    Rotation is never changed because the available Mini 4 perception surface
    provides distance, not a sufficiently complete swept-volume model.
    """
    values = (forward_mps, right_mps, up_mps, yaw_rate_deg_s, now_monotonic_ms)
    if not all(_finite_number(value) for value in values):
        raise ValueError("command and time values must be finite numbers")
    if not isinstance(config, ObstacleAvoidanceConfig):
        raise TypeError("config must be ObstacleAvoidanceConfig")

    requested = {
        "forward": max(0.0, float(forward_mps)),
        "backward": max(0.0, -float(forward_mps)),
        "right": max(0.0, float(right_mps)),
        "left": max(0.0, -float(right_mps)),
        "upward": max(0.0, float(up_mps)),
        "downward": max(0.0, -float(up_mps)),
    }
    active_directions = tuple(name for name, speed in requested.items() if speed > 0)
    if config.mode is AvoidanceMode.OFF or not active_directions:
        return _result(
            forward_mps, right_mps, up_mps, yaw_rate_deg_s, config.mode,
            observation_fresh=_observation_fresh(observation, now_monotonic_ms, config),
            reason="disabled" if config.mode is AvoidanceMode.OFF else "neutral_translation",
        )

    fresh = _observation_fresh(observation, now_monotonic_ms, config)
    distances = _normalized_distances(observation.distances_m) if fresh else {}
    missing = tuple(name for name in active_directions if name not in distances)
    threats = []
    capped = dict(requested)
    for direction in active_directions:
        distance = distances.get(direction)
        if distance is None:
            continue
        safe_speed = safe_approach_speed(distance, config)
        if requested[direction] > safe_speed:
            threats.append(direction)
            if config.mode is AvoidanceMode.BRAKE:
                capped[direction] = safe_speed

    reason = "clear"
    limited = tuple(threats) if config.mode is AvoidanceMode.BRAKE else ()
    if threats:
        reason = "braking" if config.mode is AvoidanceMode.BRAKE else "threat_advisory"
    if missing and config.missing_data_behavior is MissingDataBehavior.STOP_TRANSLATION:
        if config.mode is AvoidanceMode.BRAKE:
            for direction in active_directions:
                capped[direction] = 0.0
            limited = active_directions
            reason = "missing_or_stale_obstacle_data_stop"
        elif not threats:
            reason = "missing_or_stale_obstacle_data_advisory"

    if config.mode is not AvoidanceMode.BRAKE:
        capped = requested
    return AvoidanceResult(
        forward_mps=capped["forward"] - capped["backward"],
        right_mps=capped["right"] - capped["left"],
        up_mps=capped["upward"] - capped["downward"],
        yaw_rate_deg_s=float(yaw_rate_deg_s),
        mode=config.mode,
        observation_fresh=fresh,
        limited_directions=tuple(limited),
        threat_directions=tuple(threats),
        missing_directions=missing,
        reason=reason,
    )


def safe_approach_speed(distance_m, config):
    """Maximum speed whose reaction plus braking distance fits the clearance."""
    if not _finite_number(distance_m) or distance_m < 0:
        raise ValueError("distance_m must be finite and non-negative")
    available = max(0.0, float(distance_m) - config.minimum_clearance_m)
    acceleration = config.maximum_deceleration_mps2
    reaction = config.reaction_time_s
    return max(
        0.0,
        -acceleration * reaction
        + math.sqrt((acceleration * reaction) ** 2 + 2 * acceleration * available),
    )


def _observation_fresh(observation, now_ms, config):
    if not isinstance(observation, ObstacleObservation):
        return False
    updated = observation.source_updated_monotonic_ms
    return (
        _finite_number(updated)
        and 0.0 <= now_ms - updated <= config.maximum_source_age_ms
    )


def _normalized_distances(values):
    if not isinstance(values, dict):
        return {}
    allowed = {"forward", "backward", "right", "left", "upward", "downward"}
    return {
        key: float(value)
        for key, value in values.items()
        if key in allowed and _finite_number(value) and value >= 0
    }


def _finite_number(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _result(
    forward, right, up, yaw, mode, *, observation_fresh, reason
):
    return AvoidanceResult(
        float(forward), float(right), float(up), float(yaw), mode,
        observation_fresh, (), (), (), reason,
    )
