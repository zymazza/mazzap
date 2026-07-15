#!/usr/bin/env python3
"""Pure Mac-side route revision and guidance core for the VEIL DJI bridge.

This module deliberately performs no I/O and imports no Android or DJI code. It
accepts immutable ``veil.route-revision.v1`` documents, maintains an atomic
revisioned route state, derives bounded ground-NED velocity commands from fresh
telemetry, and converts those commands to the body axes expected by the thin
Android Virtual Stick transport.

It is not DJI Fly waypoint interoperability and it is not an aircraft-resident
mission implementation. The persistent Mac flight session owns execution.
"""

import json
import math
import threading
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType


BRIDGE_ROUTE_SCHEMA = "veil.route-revision.v1"
BRIDGE_ROUTE_ENGINE = "bridge_virtual_stick"
EARTH_RADIUS_METERS = 6_378_137.0
MAX_JSON_CHARACTERS = 512 * 1024
MAX_PARSED_WAYPOINTS = 10_000
SIGNED_INT64_MIN = -(1 << 63)
SIGNED_INT64_MAX = (1 << 63) - 1


_UNSUPPORTED_ACTIONS = MappingProxyType({
    "native_waypoint_upload": (
        "Mini 4 Pro does not expose native waypoint execution through MSDK 5.18"
    ),
    "native_waypoint_start": (
        "Mini 4 Pro does not expose native waypoint execution through MSDK 5.18"
    ),
    "dji_fly_import": (
        "DJI Fly route-library interoperability is not exposed through MSDK"
    ),
    "dji_fly_export": (
        "DJI Fly route-library interoperability is not exposed through MSDK"
    ),
})

ROUTE_CAPABILITIES = MappingProxyType({
    "schema": BRIDGE_ROUTE_SCHEMA,
    "route_engine": BRIDGE_ROUTE_ENGINE,
    "execution_owner": "mac_persistent_session",
    "revision_acceptance": True,
    "mid_flight_replacement": True,
    "android_route_endpoint": False,
    "native_waypoint_execution": False,
    "fly_library_interop": False,
    "aircraft_resident_route": False,
    "unsupported_actions": _UNSUPPORTED_ACTIONS,
})


def route_capabilities_dict():
    """Return a JSON-serializable copy without exposing mutable module state."""
    result = dict(ROUTE_CAPABILITIES)
    result["unsupported_actions"] = dict(_UNSUPPORTED_ACTIONS)
    return result


class RouteYawMode(str, Enum):
    FACE_WAYPOINT = "face_waypoint"
    FIXED_HEADING = "fixed_heading"
    HOLD_HEADING = "hold_heading"


class RoutePhase(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"


class RouteReplacementMode(str, Enum):
    IMMEDIATE = "immediate"
    AT_WAYPOINT_BOUNDARY = "at_waypoint_boundary"


class RouteReplacementScope(str, Enum):
    # The replacement is a complete route; retain progress through its indices.
    FULL_ROUTE_CONTINUE = "full_route_continue"
    # The replacement contains only targets remaining from the current position.
    REMAINING_ROUTE_FROM_CURRENT_STATE = "remaining_route_from_current_state"


class RouteCommandReason(str, Enum):
    ACTIVE = "active"
    NOT_STARTED = "not_started"
    PAUSED = "paused"
    ABORTED = "aborted"
    COMPLETED = "completed"
    WAYPOINT_ADVANCED = "waypoint_advanced"
    PLAN_REPLACED = "plan_replaced"
    STALE_TELEMETRY = "stale_telemetry"
    INVALID_TELEMETRY = "invalid_telemetry"
    INVALID_STATE = "invalid_state"
    TARGET_TOO_FAR = "target_too_far"


class RouteRevisionAcceptanceStatus(str, Enum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    REVISION_CONFLICT = "revision_conflict"
    UNSUPPORTED = "unsupported"


class RouteParseErrorCode(str, Enum):
    INVALID_JSON = "invalid_json"
    MISSING_FIELD = "missing_field"
    WRONG_TYPE = "wrong_type"
    UNKNOWN_FIELD = "unknown_field"
    INVALID_VALUE = "invalid_value"


class RouteParseError(ValueError):
    def __init__(self, code, path, message):
        super().__init__(message)
        self.code = RouteParseErrorCode(code)
        self.path = path
        self.message = message

    def to_dict(self):
        return {
            "error": self.code.value,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class RouteWaypoint:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    horizontal_speed_mps: float = 2.0
    vertical_speed_mps: float = 1.0
    horizontal_tolerance_m: float = 1.0
    vertical_tolerance_m: float = 0.5
    yaw_mode: RouteYawMode = RouteYawMode.FACE_WAYPOINT
    yaw_deg: float = None
    maximum_yaw_rate_deg_s: float = 30.0


@dataclass(frozen=True)
class RoutePlan:
    route_id: str
    revision: int
    waypoints: tuple


@dataclass(frozen=True)
class RouteBounds:
    maximum_waypoint_count: int = 100
    minimum_altitude_m: float = 0.0
    maximum_altitude_m: float = 120.0
    maximum_horizontal_speed_mps: float = 5.0
    maximum_vertical_speed_mps: float = 2.0
    maximum_yaw_rate_deg_s: float = 45.0
    maximum_horizontal_tolerance_m: float = 10.0
    maximum_vertical_tolerance_m: float = 5.0
    maximum_plan_leg_m: float = 2_000.0
    maximum_plan_extent_m: float = 5_000.0
    maximum_distance_to_target_m: float = 2_000.0
    telemetry_maximum_age_ms: float = 500.0
    telemetry_maximum_future_skew_ms: float = 100.0
    horizontal_proportional_gain_per_s: float = 0.8
    vertical_proportional_gain_per_s: float = 0.8
    yaw_proportional_gain_per_s: float = 1.5


@dataclass(frozen=True)
class RouteTelemetry:
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    yaw_deg: float
    # Must use the same monotonic clock domain as ``now_ms`` passed to tick().
    sample_monotonic_ms: float


@dataclass(frozen=True)
class RouteRevisionRequest:
    schema: str
    engine: str
    expected_accepted_revision: int
    activation: RouteReplacementMode
    scope: RouteReplacementScope
    plan: RoutePlan


@dataclass(frozen=True)
class RouteValidationIssue:
    path: str
    message: str

    def to_dict(self):
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True)
class RouteExecutionState:
    active_plan: RoutePlan
    bounds: RouteBounds
    phase: RoutePhase
    target_waypoint_index: int
    pending_plan: RoutePlan = None
    pending_target_waypoint_index: int = None

    @property
    def newest_accepted_revision(self):
        if self.pending_plan is None:
            return self.active_plan.revision
        return max(self.active_plan.revision, self.pending_plan.revision)


@dataclass(frozen=True)
class RouteRevisionAcceptance:
    status: RouteRevisionAcceptanceStatus
    state: RouteExecutionState
    accepted_revision: int
    issues: tuple = ()

    @property
    def accepted(self):
        return self.status is RouteRevisionAcceptanceStatus.ACCEPTED

    def to_dict(self):
        return {
            "accepted": self.accepted,
            "status": self.status.value,
            "accepted_revision": self.accepted_revision,
            "issues": [issue.to_dict() for issue in self.issues],
            "route": state_to_dict(self.state),
        }


@dataclass(frozen=True)
class RouteStateChange:
    state: RouteExecutionState
    accepted: bool
    issues: tuple = ()


@dataclass(frozen=True)
class NedVelocityCommand:
    north_mps: float
    east_mps: float
    down_mps: float
    yaw_rate_deg_s: float
    reason: RouteCommandReason

    @property
    def is_active(self):
        return self.reason is RouteCommandReason.ACTIVE

    @staticmethod
    def neutral(reason):
        return NedVelocityCommand(0.0, 0.0, 0.0, 0.0, reason)


@dataclass(frozen=True)
class BodyVelocityCommand:
    forward_mps: float
    right_mps: float
    up_mps: float
    yaw_rate_deg_s: float
    reason: RouteCommandReason

    @property
    def is_active(self):
        return self.reason is RouteCommandReason.ACTIVE


@dataclass(frozen=True)
class RouteTickResult:
    state: RouteExecutionState
    command: NedVelocityCommand
    horizontal_distance_m: float = None
    vertical_error_down_m: float = None


_ENVELOPE_FIELDS = frozenset({
    "schema",
    "engine",
    "expected_accepted_revision",
    "activation",
    "scope",
    "plan",
})
_PLAN_FIELDS = frozenset({"route_id", "revision", "waypoints"})
_WAYPOINT_FIELDS = frozenset({
    "latitude_deg",
    "longitude_deg",
    "altitude_m",
    "horizontal_speed_mps",
    "vertical_speed_mps",
    "horizontal_tolerance_m",
    "vertical_tolerance_m",
    "yaw_mode",
    "yaw_deg",
    "maximum_yaw_rate_deg_s",
})


def parse_route_revision(document):
    """Strictly parse one ``veil.route-revision.v1`` JSON document.

    Parsing does not imply acceptance. Schema/engine support and revision CAS are
    checked by :class:`AtomicRouteRevisionStore`.
    """
    if not isinstance(document, str):
        raise RouteParseError(RouteParseErrorCode.WRONG_TYPE, "$", "must be text")
    if len(document) > MAX_JSON_CHARACTERS:
        raise RouteParseError(
            RouteParseErrorCode.INVALID_VALUE,
            "$",
            f"route document exceeds {MAX_JSON_CHARACTERS} characters",
        )
    try:
        root = json.loads(
            document,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
    except RouteParseError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise RouteParseError(
            RouteParseErrorCode.INVALID_JSON, "$", "invalid JSON"
        ) from error

    if not isinstance(root, dict):
        _wrong_type("$", "must be a JSON object")
    _require_only_fields(root, _ENVELOPE_FIELDS, "$")

    schema = _required_string(root, "schema", "$.schema")
    engine = _required_string(root, "engine", "$.engine")
    expected = _nullable_int64(
        root, "expected_accepted_revision", "$.expected_accepted_revision"
    )
    activation_raw = _required_string(root, "activation", "$.activation")
    try:
        activation = RouteReplacementMode(activation_raw)
    except ValueError:
        _invalid("$.activation", "must be immediate or at_waypoint_boundary")
    scope_raw = _required_string(root, "scope", "$.scope")
    try:
        scope = RouteReplacementScope(scope_raw)
    except ValueError:
        _invalid(
            "$.scope",
            "must be full_route_continue or remaining_route_from_current_state",
        )

    plan_value = _required_dict(root, "plan", "$.plan")
    _require_only_fields(plan_value, _PLAN_FIELDS, "$.plan")
    route_id = _required_string(plan_value, "route_id", "$.plan.route_id")
    revision = _required_int64(plan_value, "revision", "$.plan.revision")
    waypoint_values = _required_list(plan_value, "waypoints", "$.plan.waypoints")
    if len(waypoint_values) > MAX_PARSED_WAYPOINTS:
        _invalid(
            "$.plan.waypoints",
            f"must contain at most {MAX_PARSED_WAYPOINTS} entries",
        )

    waypoints = []
    for index, waypoint_value in enumerate(waypoint_values):
        path = f"$.plan.waypoints[{index}]"
        if not isinstance(waypoint_value, dict):
            _wrong_type(path, "must be an object")
        _require_only_fields(waypoint_value, _WAYPOINT_FIELDS, path)
        yaw_mode_raw = _optional_string(
            waypoint_value, "yaw_mode", f"{path}.yaw_mode"
        ) or RouteYawMode.FACE_WAYPOINT.value
        try:
            yaw_mode = RouteYawMode(yaw_mode_raw)
        except ValueError:
            _invalid(
                f"{path}.yaw_mode",
                "must be face_waypoint, fixed_heading, or hold_heading",
            )
        waypoints.append(RouteWaypoint(
            latitude_deg=_required_number(
                waypoint_value, "latitude_deg", f"{path}.latitude_deg"
            ),
            longitude_deg=_required_number(
                waypoint_value, "longitude_deg", f"{path}.longitude_deg"
            ),
            altitude_m=_required_number(
                waypoint_value, "altitude_m", f"{path}.altitude_m"
            ),
            horizontal_speed_mps=_optional_number_or_default(
                waypoint_value,
                "horizontal_speed_mps",
                f"{path}.horizontal_speed_mps",
                2.0,
            ),
            vertical_speed_mps=_optional_number_or_default(
                waypoint_value,
                "vertical_speed_mps",
                f"{path}.vertical_speed_mps",
                1.0,
            ),
            horizontal_tolerance_m=_optional_number_or_default(
                waypoint_value,
                "horizontal_tolerance_m",
                f"{path}.horizontal_tolerance_m",
                1.0,
            ),
            vertical_tolerance_m=_optional_number_or_default(
                waypoint_value,
                "vertical_tolerance_m",
                f"{path}.vertical_tolerance_m",
                0.5,
            ),
            yaw_mode=yaw_mode,
            yaw_deg=_optional_number(waypoint_value, "yaw_deg", f"{path}.yaw_deg"),
            maximum_yaw_rate_deg_s=_optional_number_or_default(
                waypoint_value,
                "maximum_yaw_rate_deg_s",
                f"{path}.maximum_yaw_rate_deg_s",
                30.0,
            ),
        ))

    return RouteRevisionRequest(
        schema=schema,
        engine=engine,
        expected_accepted_revision=expected,
        activation=activation,
        scope=scope,
        plan=RoutePlan(route_id, revision, tuple(waypoints)),
    )


def validate_route_plan(plan, bounds=RouteBounds()):
    issues = list(validate_route_bounds(bounds))
    if not isinstance(plan, RoutePlan):
        return tuple(issues + [RouteValidationIssue("plan", "must be a RoutePlan")])

    if not isinstance(plan.route_id, str):
        _issue(issues, "route_id", "must be a string")
    else:
        if not plan.route_id.strip():
            _issue(issues, "route_id", "must not be blank")
        if len(plan.route_id) > 128:
            _issue(issues, "route_id", "must be at most 128 characters")
    if not _is_int(plan.revision):
        _issue(issues, "revision", "must be an integer")
    elif plan.revision < 0:
        _issue(issues, "revision", "must be non-negative")
    elif plan.revision > SIGNED_INT64_MAX:
        _issue(issues, "revision", "must fit signed 64-bit range")

    try:
        waypoints = tuple(plan.waypoints)
    except TypeError:
        _issue(issues, "waypoints", "must be an iterable")
        return tuple(issues)
    if not waypoints:
        _issue(issues, "waypoints", "must contain at least one waypoint")
    if len(waypoints) > bounds.maximum_waypoint_count:
        _issue(issues, "waypoints", "exceeds maximum_waypoint_count")

    for index, waypoint in enumerate(waypoints):
        path = f"waypoints[{index}]"
        if not isinstance(waypoint, RouteWaypoint):
            _issue(issues, path, "must be a RouteWaypoint")
            continue
        _finite_in_range(
            issues, f"{path}.latitude_deg", waypoint.latitude_deg, -90.0, 90.0
        )
        _finite_in_range(
            issues, f"{path}.longitude_deg", waypoint.longitude_deg, -180.0, 180.0
        )
        _finite_in_range(
            issues,
            f"{path}.altitude_m",
            waypoint.altitude_m,
            bounds.minimum_altitude_m,
            bounds.maximum_altitude_m,
        )
        _finite_positive_at_most(
            issues,
            f"{path}.horizontal_speed_mps",
            waypoint.horizontal_speed_mps,
            bounds.maximum_horizontal_speed_mps,
        )
        _finite_positive_at_most(
            issues,
            f"{path}.vertical_speed_mps",
            waypoint.vertical_speed_mps,
            bounds.maximum_vertical_speed_mps,
        )
        _finite_positive_at_most(
            issues,
            f"{path}.horizontal_tolerance_m",
            waypoint.horizontal_tolerance_m,
            bounds.maximum_horizontal_tolerance_m,
        )
        _finite_positive_at_most(
            issues,
            f"{path}.vertical_tolerance_m",
            waypoint.vertical_tolerance_m,
            bounds.maximum_vertical_tolerance_m,
        )
        _finite_positive_at_most(
            issues,
            f"{path}.maximum_yaw_rate_deg_s",
            waypoint.maximum_yaw_rate_deg_s,
            bounds.maximum_yaw_rate_deg_s,
        )
        if not isinstance(waypoint.yaw_mode, RouteYawMode):
            _issue(issues, f"{path}.yaw_mode", "must be a RouteYawMode")
        elif waypoint.yaw_mode is RouteYawMode.FIXED_HEADING:
            if waypoint.yaw_deg is None:
                _issue(issues, f"{path}.yaw_deg", "is required for fixed_heading")
            else:
                _finite_in_range(
                    issues, f"{path}.yaw_deg", waypoint.yaw_deg, -180.0, 180.0
                )
        elif waypoint.yaw_deg is not None:
            _issue(
                issues,
                f"{path}.yaw_deg",
                "must be null unless yaw_mode is fixed_heading",
            )

    usable = [waypoint for waypoint in waypoints if _usable_waypoint(waypoint)]
    if len(usable) == len(waypoints) and waypoints:
        for index in range(1, len(waypoints)):
            distance = _waypoint_horizontal_distance(
                waypoints[index - 1], waypoints[index]
            )
            if distance > bounds.maximum_plan_leg_m:
                _issue(issues, f"waypoints[{index}]", "leg exceeds maximum_plan_leg_m")
        origin = waypoints[0]
        for index, waypoint in enumerate(waypoints[1:], 1):
            if _waypoint_horizontal_distance(origin, waypoint) > bounds.maximum_plan_extent_m:
                _issue(issues, f"waypoints[{index}]", "exceeds maximum_plan_extent_m")

    return tuple(issues)


def validate_route_bounds(bounds):
    issues = []
    if not isinstance(bounds, RouteBounds):
        return (RouteValidationIssue("bounds", "must be RouteBounds"),)
    if not _is_int(bounds.maximum_waypoint_count) or not 1 <= bounds.maximum_waypoint_count <= 10_000:
        _issue(issues, "bounds.maximum_waypoint_count", "must be between 1 and 10000")
    _finite(issues, "bounds.minimum_altitude_m", bounds.minimum_altitude_m)
    _finite(issues, "bounds.maximum_altitude_m", bounds.maximum_altitude_m)
    if _is_finite_number(bounds.minimum_altitude_m) and bounds.minimum_altitude_m < -500.0:
        _issue(issues, "bounds.minimum_altitude_m", "must be at least -500")
    if _is_finite_number(bounds.maximum_altitude_m) and bounds.maximum_altitude_m > 5_000.0:
        _issue(issues, "bounds.maximum_altitude_m", "must be at most 5000")
    if (
        _is_finite_number(bounds.minimum_altitude_m)
        and _is_finite_number(bounds.maximum_altitude_m)
        and bounds.minimum_altitude_m >= bounds.maximum_altitude_m
    ):
        _issue(issues, "bounds.maximum_altitude_m", "must exceed minimum_altitude_m")
    _finite_positive_at_most(
        issues, "bounds.maximum_horizontal_speed_mps",
        bounds.maximum_horizontal_speed_mps, 23.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_vertical_speed_mps",
        bounds.maximum_vertical_speed_mps, 6.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_yaw_rate_deg_s", bounds.maximum_yaw_rate_deg_s, 100.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_horizontal_tolerance_m",
        bounds.maximum_horizontal_tolerance_m, 100.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_vertical_tolerance_m",
        bounds.maximum_vertical_tolerance_m, 100.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_plan_leg_m", bounds.maximum_plan_leg_m, 5_000.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_plan_extent_m", bounds.maximum_plan_extent_m, 10_000.0
    )
    _finite_positive_at_most(
        issues, "bounds.maximum_distance_to_target_m",
        bounds.maximum_distance_to_target_m, 5_000.0
    )
    _finite_positive_at_most(
        issues, "bounds.telemetry_maximum_age_ms", bounds.telemetry_maximum_age_ms, 10_000.0
    )
    if not _is_finite_number(bounds.telemetry_maximum_future_skew_ms):
        _issue(issues, "bounds.telemetry_maximum_future_skew_ms", "must be finite")
    elif not 0.0 <= bounds.telemetry_maximum_future_skew_ms <= 5_000.0:
        _issue(
            issues,
            "bounds.telemetry_maximum_future_skew_ms",
            "must be between 0 and 5000",
        )
    _finite_positive_at_most(
        issues, "bounds.horizontal_proportional_gain_per_s",
        bounds.horizontal_proportional_gain_per_s, 10.0
    )
    _finite_positive_at_most(
        issues, "bounds.vertical_proportional_gain_per_s",
        bounds.vertical_proportional_gain_per_s, 10.0
    )
    _finite_positive_at_most(
        issues, "bounds.yaw_proportional_gain_per_s",
        bounds.yaw_proportional_gain_per_s, 10.0
    )
    return tuple(issues)


class AtomicRouteRevisionStore:
    """Thread-safe owner of one immutable route execution state."""

    def __init__(self, bounds=RouteBounds()):
        issues = validate_route_bounds(bounds)
        if issues:
            raise ValueError(
                "invalid route bounds: "
                + "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
            )
        self._bounds = replace(bounds)
        self._state = None
        self._lock = threading.RLock()

    def snapshot(self):
        with self._lock:
            return self._state

    def newest_accepted_revision(self):
        with self._lock:
            return (
                None if self._state is None
                else self._state.newest_accepted_revision
            )

    def accept(self, request):
        if not isinstance(request, RouteRevisionRequest):
            raise TypeError("request must be RouteRevisionRequest")
        with self._lock:
            before = self._state
            newest_before = (
                None if before is None else before.newest_accepted_revision
            )
            if request.schema != BRIDGE_ROUTE_SCHEMA:
                return self._unsupported(
                    before, "schema", f"unsupported route schema: {request.schema}"
                )
            if request.engine != BRIDGE_ROUTE_ENGINE:
                return self._unsupported(
                    before, "engine", f"unsupported route engine: {request.engine}"
                )
            if (
                request.expected_accepted_revision is not None
                and (
                    not _is_int(request.expected_accepted_revision)
                    or request.expected_accepted_revision < SIGNED_INT64_MIN
                    or request.expected_accepted_revision > SIGNED_INT64_MAX
                )
            ):
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.INVALID,
                    before,
                    newest_before,
                    (RouteValidationIssue(
                        "expected_accepted_revision",
                        "must be null or a signed 64-bit integer",
                    ),),
                )
            if not isinstance(request.activation, RouteReplacementMode):
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.INVALID,
                    before,
                    newest_before,
                    (RouteValidationIssue(
                        "activation", "must be a supported replacement mode"
                    ),),
                )
            if not isinstance(request.scope, RouteReplacementScope):
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.INVALID,
                    before,
                    newest_before,
                    (RouteValidationIssue(
                        "scope", "must be a supported replacement scope"
                    ),),
                )
            if request.expected_accepted_revision != newest_before:
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.REVISION_CONFLICT,
                    before,
                    newest_before,
                    (RouteValidationIssue(
                        "expected_accepted_revision",
                        f"expected {request.expected_accepted_revision}, current is {newest_before}",
                    ),),
                )

            try:
                plan = _snapshot_plan(request.plan)
            except (TypeError, AttributeError) as error:
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.INVALID,
                    before,
                    newest_before,
                    (RouteValidationIssue("plan", str(error)),),
                )
            issues = validate_route_plan(plan, self._bounds)
            if issues:
                return RouteRevisionAcceptance(
                    RouteRevisionAcceptanceStatus.INVALID,
                    before,
                    newest_before,
                    issues,
                )

            if before is None:
                proposed = RouteExecutionState(
                    plan, self._bounds, RoutePhase.READY, 0
                )
            else:
                change = replace_route(
                    before, plan, request.activation, request.scope
                )
                if not change.accepted:
                    return RouteRevisionAcceptance(
                        RouteRevisionAcceptanceStatus.INVALID,
                        before,
                        newest_before,
                        change.issues,
                    )
                proposed = change.state

            self._state = proposed
            return RouteRevisionAcceptance(
                RouteRevisionAcceptanceStatus.ACCEPTED,
                proposed,
                proposed.newest_accepted_revision,
            )

    def start(self):
        return self._transition(start_route)

    def pause(self):
        return self._transition(pause_route)

    def resume(self):
        return self._transition(resume_route)

    def abort(self):
        return self._transition(abort_route)

    def tick(self, telemetry, now_ms):
        with self._lock:
            if self._state is None:
                return None
            result = tick_route(self._state, telemetry, now_ms)
            self._state = result.state
            return result

    def _transition(self, operation):
        with self._lock:
            if self._state is None:
                return None
            result = operation(self._state)
            if result.accepted:
                self._state = result.state
            return result

    def _unsupported(self, state, path, message):
        return RouteRevisionAcceptance(
            RouteRevisionAcceptanceStatus.UNSUPPORTED,
            state,
            None if state is None else state.newest_accepted_revision,
            (RouteValidationIssue(path, message),),
        )


def start_route(state):
    if state.phase is RoutePhase.READY:
        return RouteStateChange(replace(state, phase=RoutePhase.RUNNING), True)
    return RouteStateChange(state, False)


def pause_route(state):
    if state.phase is RoutePhase.RUNNING:
        return RouteStateChange(replace(state, phase=RoutePhase.PAUSED), True)
    return RouteStateChange(state, False)


def resume_route(state):
    if state.phase is RoutePhase.PAUSED:
        return RouteStateChange(replace(state, phase=RoutePhase.RUNNING), True)
    return RouteStateChange(state, False)


def abort_route(state):
    if state.phase in (RoutePhase.READY, RoutePhase.RUNNING, RoutePhase.PAUSED):
        return RouteStateChange(replace(
            state,
            phase=RoutePhase.ABORTED,
            pending_plan=None,
            pending_target_waypoint_index=None,
        ), True)
    return RouteStateChange(state, False)


def replace_route(state, replacement, activation, scope):
    if state.phase in (RoutePhase.COMPLETED, RoutePhase.ABORTED):
        return RouteStateChange(
            state,
            False,
            (RouteValidationIssue(
                "phase", f"cannot replace a {state.phase.value} route"
            ),),
        )
    issues = list(validate_route_plan(replacement, state.bounds))
    if not isinstance(activation, RouteReplacementMode):
        _issue(issues, "activation", "unsupported replacement activation")
    if not isinstance(scope, RouteReplacementScope):
        _issue(issues, "scope", "unsupported replacement scope")
    if replacement.route_id != state.active_plan.route_id:
        _issue(issues, "route_id", "must match the active route_id")
    if replacement.revision <= state.newest_accepted_revision:
        _issue(
            issues,
            "revision",
            "must be newer than active and pending revisions",
        )

    if scope is RouteReplacementScope.FULL_ROUTE_CONTINUE:
        if activation is RouteReplacementMode.IMMEDIATE:
            target_index = state.target_waypoint_index
        elif activation is RouteReplacementMode.AT_WAYPOINT_BOUNDARY:
            target_index = state.target_waypoint_index + 1
        else:
            target_index = -1
    elif scope is RouteReplacementScope.REMAINING_ROUTE_FROM_CURRENT_STATE:
        target_index = 0
    else:
        target_index = -1

    if target_index not in range(len(replacement.waypoints)):
        _issue(
            issues,
            "waypoints",
            f"must contain continuation target index {target_index}",
        )
    if issues:
        return RouteStateChange(state, False, tuple(issues))

    if activation is RouteReplacementMode.IMMEDIATE:
        return RouteStateChange(replace(
            state,
            active_plan=replacement,
            target_waypoint_index=target_index,
            pending_plan=None,
            pending_target_waypoint_index=None,
        ), True)
    return RouteStateChange(replace(
        state,
        pending_plan=replacement,
        pending_target_waypoint_index=target_index,
    ), True)


def tick_route(state, telemetry, now_ms):
    inactive_reason = {
        RoutePhase.READY: RouteCommandReason.NOT_STARTED,
        RoutePhase.PAUSED: RouteCommandReason.PAUSED,
        RoutePhase.COMPLETED: RouteCommandReason.COMPLETED,
        RoutePhase.ABORTED: RouteCommandReason.ABORTED,
    }.get(state.phase)
    if inactive_reason is not None:
        return _neutral_tick(state, inactive_reason)
    if state.phase is not RoutePhase.RUNNING or not _valid_execution_state(state):
        return _neutral_tick(state, RouteCommandReason.INVALID_STATE)

    telemetry_issue = _validate_telemetry(telemetry, now_ms, state.bounds)
    if telemetry_issue is not None:
        return _neutral_tick(state, telemetry_issue)

    target = state.active_plan.waypoints[state.target_waypoint_index]
    north, east, down = _local_target_offset(telemetry, target)
    if not all(math.isfinite(value) for value in (north, east, down)):
        return _neutral_tick(state, RouteCommandReason.INVALID_TELEMETRY)
    horizontal_distance = math.hypot(north, east)
    if horizontal_distance > state.bounds.maximum_distance_to_target_m:
        return RouteTickResult(
            state,
            NedVelocityCommand.neutral(RouteCommandReason.TARGET_TOO_FAR),
            horizontal_distance,
            down,
        )

    reached = (
        horizontal_distance <= target.horizontal_tolerance_m
        and abs(down) <= target.vertical_tolerance_m
    )
    if reached:
        if state.pending_plan is not None:
            pending_index = state.pending_target_waypoint_index
            if pending_index not in range(len(state.pending_plan.waypoints)):
                return _neutral_tick(state, RouteCommandReason.INVALID_STATE)
            replaced_state = replace(
                state,
                active_plan=state.pending_plan,
                target_waypoint_index=pending_index,
                pending_plan=None,
                pending_target_waypoint_index=None,
            )
            return RouteTickResult(
                replaced_state,
                NedVelocityCommand.neutral(RouteCommandReason.PLAN_REPLACED),
                horizontal_distance,
                down,
            )
        if state.target_waypoint_index == len(state.active_plan.waypoints) - 1:
            completed = replace(state, phase=RoutePhase.COMPLETED)
            return RouteTickResult(
                completed,
                NedVelocityCommand.neutral(RouteCommandReason.COMPLETED),
                horizontal_distance,
                down,
            )
        advanced = replace(
            state, target_waypoint_index=state.target_waypoint_index + 1
        )
        return RouteTickResult(
            advanced,
            NedVelocityCommand.neutral(RouteCommandReason.WAYPOINT_ADVANCED),
            horizontal_distance,
            down,
        )

    maximum_horizontal = min(
        target.horizontal_speed_mps,
        state.bounds.maximum_horizontal_speed_mps,
    )
    if horizontal_distance <= target.horizontal_tolerance_m:
        horizontal_magnitude = 0.0
    else:
        horizontal_magnitude = min(
            maximum_horizontal,
            horizontal_distance * state.bounds.horizontal_proportional_gain_per_s,
        )
    if horizontal_distance == 0.0:
        north_command = east_command = 0.0
    else:
        north_command = north / horizontal_distance * horizontal_magnitude
        east_command = east / horizontal_distance * horizontal_magnitude

    maximum_vertical = min(
        target.vertical_speed_mps,
        state.bounds.maximum_vertical_speed_mps,
    )
    if abs(down) <= target.vertical_tolerance_m:
        down_command = 0.0
    else:
        down_command = _clamp(
            down * state.bounds.vertical_proportional_gain_per_s,
            -maximum_vertical,
            maximum_vertical,
        )

    yaw_limit = min(
        target.maximum_yaw_rate_deg_s,
        state.bounds.maximum_yaw_rate_deg_s,
    )
    yaw_rate = _yaw_rate_command(
        target,
        telemetry.yaw_deg,
        north,
        east,
        horizontal_distance,
        yaw_limit,
        state.bounds,
    )
    return RouteTickResult(
        state,
        NedVelocityCommand(
            north_command,
            east_command,
            down_command,
            yaw_rate,
            RouteCommandReason.ACTIVE,
        ),
        horizontal_distance,
        down,
    )


def ground_ned_to_body(command, yaw_deg):
    """Rotate a ground-NED velocity into DJI body forward/right/up axes.

    ``yaw_deg`` is heading clockwise from north. Yaw angular velocity is already
    expressed in the aircraft axis and therefore passes through unchanged.
    """
    if not isinstance(command, NedVelocityCommand):
        raise TypeError("command must be NedVelocityCommand")
    if not _is_finite_number(yaw_deg):
        raise ValueError("yaw_deg must be finite")
    yaw_radians = math.radians(_normalize_degrees(float(yaw_deg)))
    cosine = math.cos(yaw_radians)
    sine = math.sin(yaw_radians)
    forward = command.north_mps * cosine + command.east_mps * sine
    right = -command.north_mps * sine + command.east_mps * cosine
    return BodyVelocityCommand(
        forward_mps=_zero_small(forward),
        right_mps=_zero_small(right),
        up_mps=_zero_small(-command.down_mps),
        yaw_rate_deg_s=command.yaw_rate_deg_s,
        reason=command.reason,
    )


def state_to_dict(state):
    if state is None:
        return None
    return {
        "route_id": state.active_plan.route_id,
        "active_revision": state.active_plan.revision,
        "newest_accepted_revision": state.newest_accepted_revision,
        "phase": state.phase.value,
        "target_waypoint_index": state.target_waypoint_index,
        "pending_revision": (
            None if state.pending_plan is None else state.pending_plan.revision
        ),
        "pending_target_waypoint_index": state.pending_target_waypoint_index,
    }


def _snapshot_plan(plan):
    if not isinstance(plan, RoutePlan):
        raise TypeError("must be RoutePlan")
    try:
        waypoints = tuple(plan.waypoints)
    except TypeError as error:
        raise TypeError("waypoints must be iterable") from error
    snapshots = []
    for index, waypoint in enumerate(waypoints):
        if not isinstance(waypoint, RouteWaypoint):
            raise TypeError(f"waypoints[{index}] must be RouteWaypoint")
        snapshots.append(replace(waypoint))
    return RoutePlan(plan.route_id, plan.revision, tuple(snapshots))


def _valid_execution_state(state):
    # Plans and bounds are fully validated and snapshotted at acceptance. Keep
    # this per-guidance-tick check O(1): walking every route leg at 20 Hz would
    # add latency without increasing integrity of the immutable store state.
    if (
        not isinstance(state, RouteExecutionState)
        or not isinstance(state.active_plan, RoutePlan)
        or not isinstance(state.bounds, RouteBounds)
        or not isinstance(state.active_plan.waypoints, tuple)
        or not isinstance(state.active_plan.route_id, str)
        or not _is_int(state.active_plan.revision)
    ):
        return False
    if (
        not _is_int(state.target_waypoint_index)
        or state.target_waypoint_index not in range(len(state.active_plan.waypoints))
    ):
        return False
    if not isinstance(
        state.active_plan.waypoints[state.target_waypoint_index], RouteWaypoint
    ):
        return False
    if state.pending_plan is None:
        return state.pending_target_waypoint_index is None
    return (
        isinstance(state.pending_plan, RoutePlan)
        and isinstance(state.pending_plan.waypoints, tuple)
        and isinstance(state.pending_plan.route_id, str)
        and _is_int(state.pending_plan.revision)
        and state.pending_plan.route_id == state.active_plan.route_id
        and state.pending_plan.revision > state.active_plan.revision
        and _is_int(state.pending_target_waypoint_index)
        and state.pending_target_waypoint_index
        in range(len(state.pending_plan.waypoints))
        and isinstance(
            state.pending_plan.waypoints[state.pending_target_waypoint_index],
            RouteWaypoint,
        )
    )


def _validate_telemetry(telemetry, now_ms, bounds):
    if not isinstance(telemetry, RouteTelemetry):
        return RouteCommandReason.INVALID_TELEMETRY
    if not _is_finite_number(now_ms) or now_ms < 0.0:
        return RouteCommandReason.INVALID_TELEMETRY
    if (
        not _is_finite_number(telemetry.latitude_deg)
        or not -90.0 <= telemetry.latitude_deg <= 90.0
        or not _is_finite_number(telemetry.longitude_deg)
        or not -180.0 <= telemetry.longitude_deg <= 180.0
        or not _is_finite_number(telemetry.altitude_m)
        or not _is_finite_number(telemetry.yaw_deg)
        or not _is_finite_number(telemetry.sample_monotonic_ms)
        or telemetry.sample_monotonic_ms < 0.0
    ):
        return RouteCommandReason.INVALID_TELEMETRY
    if telemetry.sample_monotonic_ms <= now_ms:
        if now_ms - telemetry.sample_monotonic_ms > bounds.telemetry_maximum_age_ms:
            return RouteCommandReason.STALE_TELEMETRY
    elif (
        telemetry.sample_monotonic_ms - now_ms
        > bounds.telemetry_maximum_future_skew_ms
    ):
        return RouteCommandReason.STALE_TELEMETRY
    return None


def _yaw_rate_command(
    target,
    current_yaw_deg,
    north,
    east,
    horizontal_distance,
    rate_limit,
    bounds,
):
    if target.yaw_mode is RouteYawMode.HOLD_HEADING:
        return 0.0
    if target.yaw_mode is RouteYawMode.FACE_WAYPOINT:
        if horizontal_distance <= target.horizontal_tolerance_m:
            return 0.0
        desired = _normalize_degrees(math.degrees(math.atan2(east, north)))
    else:
        desired = _normalize_degrees(target.yaw_deg)
    error = _normalize_degrees(desired - _normalize_degrees(current_yaw_deg))
    return _clamp(
        error * bounds.yaw_proportional_gain_per_s,
        -rate_limit,
        rate_limit,
    )


def _local_target_offset(current, target):
    latitude_delta = math.radians(target.latitude_deg - current.latitude_deg)
    longitude_delta = math.radians(
        _normalized_longitude_delta(target.longitude_deg - current.longitude_deg)
    )
    mean_latitude = math.radians(
        (target.latitude_deg + current.latitude_deg) * 0.5
    )
    return (
        latitude_delta * EARTH_RADIUS_METERS,
        longitude_delta * EARTH_RADIUS_METERS * math.cos(mean_latitude),
        current.altitude_m - target.altitude_m,
    )


def _waypoint_horizontal_distance(first, second):
    latitude_delta = math.radians(second.latitude_deg - first.latitude_deg)
    longitude_delta = math.radians(
        _normalized_longitude_delta(second.longitude_deg - first.longitude_deg)
    )
    mean_latitude = math.radians(
        (first.latitude_deg + second.latitude_deg) * 0.5
    )
    return math.hypot(
        latitude_delta * EARTH_RADIUS_METERS,
        longitude_delta * EARTH_RADIUS_METERS * math.cos(mean_latitude),
    )


def _normalized_longitude_delta(delta):
    result = delta
    while result > 180.0:
        result -= 360.0
    while result < -180.0:
        result += 360.0
    return result


def _normalize_degrees(value):
    result = value % 360.0
    if result >= 180.0:
        result -= 360.0
    return result


def _neutral_tick(state, reason):
    return RouteTickResult(state, NedVelocityCommand.neutral(reason))


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _zero_small(value):
    return 0.0 if abs(value) < 1e-12 else value


def _usable_waypoint(waypoint):
    return (
        isinstance(waypoint, RouteWaypoint)
        and _is_finite_number(waypoint.latitude_deg)
        and -90.0 <= waypoint.latitude_deg <= 90.0
        and _is_finite_number(waypoint.longitude_deg)
        and -180.0 <= waypoint.longitude_deg <= 180.0
    )


def _issue(issues, path, message):
    issues.append(RouteValidationIssue(path, message))


def _finite_in_range(issues, path, value, minimum, maximum):
    if not _is_finite_number(value):
        _issue(issues, path, "must be finite")
    elif value < minimum or value > maximum:
        _issue(issues, path, f"must be between {minimum} and {maximum}")


def _finite_positive_at_most(issues, path, value, maximum):
    if not _is_finite_number(value):
        _issue(issues, path, "must be finite")
    elif value <= 0.0:
        _issue(issues, path, "must be positive")
    elif value > maximum:
        _issue(issues, path, f"exceeds configured maximum {maximum}")


def _finite(issues, path, value):
    if not _is_finite_number(value):
        _issue(issues, path, "must be finite")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _object_without_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RouteParseError(
                RouteParseErrorCode.INVALID_JSON,
                "$",
                f"duplicate object field: {key}",
            )
        result[key] = value
    return result


def _reject_non_finite_json_constant(value):
    raise RouteParseError(
        RouteParseErrorCode.INVALID_VALUE, "$", f"non-finite number is not allowed: {value}"
    )


def _require_only_fields(value, allowed, path):
    unknown = sorted(set(value) - allowed)
    if unknown:
        key = unknown[0]
        raise RouteParseError(
            RouteParseErrorCode.UNKNOWN_FIELD,
            f"{path}.{key}",
            f"unknown field: {key}",
        )


def _required_string(value, name, path):
    if name not in value:
        _missing(path)
    result = value[name]
    if not isinstance(result, str):
        _wrong_type(path, "must be a string")
    return result


def _optional_string(value, name, path):
    if name not in value or value[name] is None:
        return None
    result = value[name]
    if not isinstance(result, str):
        _wrong_type(path, "must be a string")
    return result


def _required_int64(value, name, path):
    if name not in value:
        _missing(path)
    return _exact_int64(value[name], path)


def _nullable_int64(value, name, path):
    if name not in value or value[name] is None:
        return None
    return _exact_int64(value[name], path)


def _exact_int64(value, path):
    if not _is_int(value):
        _wrong_type(path, "must be an integer")
    if value < SIGNED_INT64_MIN or value > SIGNED_INT64_MAX:
        _invalid(path, "is outside signed 64-bit range")
    return value


def _required_number(value, name, path):
    if name not in value:
        _missing(path)
    return _finite_number(value[name], path)


def _optional_number(value, name, path):
    if name not in value or value[name] is None:
        return None
    return _finite_number(value[name], path)


def _optional_number_or_default(value, name, path, default):
    result = _optional_number(value, name, path)
    return default if result is None else result


def _finite_number(value, path):
    if not _is_finite_number(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            _invalid(path, "must be finite")
        _wrong_type(path, "must be a number")
    try:
        return float(value)
    except OverflowError:
        _invalid(path, "must be finite")


def _required_dict(value, name, path):
    if name not in value:
        _missing(path)
    result = value[name]
    if not isinstance(result, dict):
        _wrong_type(path, "must be an object")
    return result


def _required_list(value, name, path):
    if name not in value:
        _missing(path)
    result = value[name]
    if not isinstance(result, list):
        _wrong_type(path, "must be an array")
    return result


def _missing(path):
    raise RouteParseError(
        RouteParseErrorCode.MISSING_FIELD, path, "missing required field"
    )


def _wrong_type(path, message):
    raise RouteParseError(RouteParseErrorCode.WRONG_TYPE, path, message)


def _invalid(path, message):
    raise RouteParseError(RouteParseErrorCode.INVALID_VALUE, path, message)
