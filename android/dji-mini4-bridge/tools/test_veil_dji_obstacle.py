#!/usr/bin/env python3

import math
import unittest

from veil_dji_obstacle import (
    AvoidanceMode,
    MissingDataBehavior,
    ObstacleAvoidanceConfig,
    ObstacleObservation,
    limit_body_velocity,
    safe_approach_speed,
)


class ObstacleAvoidanceTest(unittest.TestCase):
    def test_safe_speed_obeys_reaction_plus_braking_distance(self):
        config = ObstacleAvoidanceConfig(
            minimum_clearance_m=0.8,
            reaction_time_s=0.25,
            maximum_deceleration_mps2=2.0,
        )
        speed = safe_approach_speed(2.0, config)
        stopping_distance = (
            speed * config.reaction_time_s
            + speed * speed / (2 * config.maximum_deceleration_mps2)
        )
        self.assertAlmostEqual(1.2, stopping_distance)

    def test_brake_limits_only_components_toward_obstacles(self):
        config = ObstacleAvoidanceConfig(mode=AvoidanceMode.BRAKE)
        result = limit_body_velocity(
            forward_mps=3.0,
            right_mps=-2.0,
            up_mps=0.5,
            yaw_rate_deg_s=20.0,
            observation=ObstacleObservation(
                {"forward": 1.0, "left": 20.0, "upward": 20.0}, 1_000.0
            ),
            now_monotonic_ms=1_050.0,
            config=config,
        )
        self.assertLess(result.forward_mps, 1.0)
        self.assertEqual(-2.0, result.right_mps)
        self.assertEqual(0.5, result.up_mps)
        self.assertEqual(20.0, result.yaw_rate_deg_s)
        self.assertEqual(("forward",), result.limited_directions)

    def test_motion_away_from_near_obstacle_is_not_limited(self):
        result = limit_body_velocity(
            forward_mps=-1.0,
            right_mps=0.0,
            up_mps=0.0,
            yaw_rate_deg_s=0.0,
            observation=ObstacleObservation({"forward": 0.2, "backward": 5.0}, 1.0),
            now_monotonic_ms=2.0,
            config=ObstacleAvoidanceConfig(mode=AvoidanceMode.BRAKE),
        )
        self.assertEqual(-1.0, result.forward_mps)
        self.assertFalse(result.translation_limited)

    def test_advisory_reports_threat_without_changing_command(self):
        result = limit_body_velocity(
            forward_mps=2.0,
            right_mps=0.0,
            up_mps=0.0,
            yaw_rate_deg_s=0.0,
            observation=ObstacleObservation({"forward": 0.9}, 10.0),
            now_monotonic_ms=11.0,
            config=ObstacleAvoidanceConfig(mode=AvoidanceMode.ADVISORY),
        )
        self.assertEqual(2.0, result.forward_mps)
        self.assertEqual(("forward",), result.threat_directions)
        self.assertFalse(result.translation_limited)

    def test_stale_data_can_fail_closed_for_autonomous_motion(self):
        result = limit_body_velocity(
            forward_mps=1.0,
            right_mps=0.5,
            up_mps=0.0,
            yaw_rate_deg_s=5.0,
            observation=ObstacleObservation({"forward": 20.0, "right": 20.0}, 0.0),
            now_monotonic_ms=1_000.0,
            config=ObstacleAvoidanceConfig(
                mode=AvoidanceMode.BRAKE,
                maximum_source_age_ms=100.0,
                missing_data_behavior=MissingDataBehavior.STOP_TRANSLATION,
            ),
        )
        self.assertEqual((0.0, 0.0, 0.0), (
            result.forward_mps, result.right_mps, result.up_mps
        ))
        self.assertEqual(5.0, result.yaw_rate_deg_s)
        self.assertEqual("missing_or_stale_obstacle_data_stop", result.reason)

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ObstacleAvoidanceConfig(maximum_deceleration_mps2=0)
        with self.assertRaises(ValueError):
            ObstacleAvoidanceConfig(minimum_clearance_m=math.nan)


if __name__ == "__main__":
    unittest.main()
