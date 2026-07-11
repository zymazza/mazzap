#!/usr/bin/env python3
"""Focused water-allocation tests for the hydrology scenario drapes."""

import unittest

import numpy as np

import analyze_hydrology as terrain
import hydro_scenario as scenario


class RootZoneCapacityTest(unittest.TestCase):
    def test_horizon_awc_is_integrated_to_root_depth(self):
        rec = {
            "horizons": [
                {"top_cm": 0, "bottom_cm": 5, "awc_cm_cm": 0.35},
                {"top_cm": 5, "bottom_cm": 15, "awc_cm_cm": 0.14},
                {"top_cm": 15, "bottom_cm": 76, "awc_cm_cm": 0.15},
            ],
        }
        self.assertAlmostEqual(terrain.root_zone_taw_mm(rec), 114.0)

    def test_restriction_clips_root_zone_storage(self):
        rec = {
            "water_table_depth_annual_min_cm": 10,
            "horizons": [
                {"top_cm": 0, "bottom_cm": 5, "awc_cm_cm": 0.35},
                {"top_cm": 5, "bottom_cm": 15, "awc_cm_cm": 0.14},
            ],
        }
        self.assertAlmostEqual(terrain.root_zone_taw_mm(rec), 24.5)

    def test_map_unit_aws_is_a_coarse_fallback(self):
        rec = {"available_water_storage_0_150cm_cm": 15.0}
        self.assertAlmostEqual(terrain.root_zone_taw_mm(rec), 70.0)

    def test_surface_texture_skips_incomplete_horizons(self):
        rec = {"horizons": [
            {"sand_pct": 30, "silt_pct": 0, "clay_pct": None},
            {"sand_pct": 61, "silt_pct": 35, "clay_pct": 4},
        ]}
        self.assertEqual(terrain.surface_texture_fractions(rec), (61.0, 35.0, 4.0))


def synthetic_fields(dem, flowdir, depression_depth=None):
    dem = np.asarray(dem, dtype=float)
    depth = (np.zeros_like(dem) if depression_depth is None
             else np.asarray(depression_depth, dtype=float))
    return {
        "dem": dem,
        "filled": dem + depth,
        "depression_depth": depth,
        "flowdir": np.asarray(flowdir, dtype=np.int8),
        "twi": np.zeros_like(dem),
        "cell_area_m2": 1.0,
    }


def synthetic_soils(shape, ksat=10.0, restriction_cm=np.nan,
                    bedrock_cm=np.nan, water_table_cm=np.nan):
    ksat = np.broadcast_to(np.asarray(ksat, dtype=float), shape).copy()
    restriction = np.broadcast_to(np.asarray(restriction_cm, dtype=float), shape).copy()
    bedrock = np.broadcast_to(np.asarray(bedrock_cm, dtype=float), shape).copy()
    water_table = np.broadcast_to(np.asarray(water_table_cm, dtype=float), shape).copy()
    return {
        "available": True,
        "root_zone_taw_mm": np.full(shape, 100.0),
        "ksat_min": ksat,
        "surface_ksat": ksat.copy(),
        "restrictive_cm": restriction,
        "bedrock_cm": bedrock,
        "water_table_cm": water_table,
        "hsg": np.full(shape, "B", dtype=object),
        "sand_pct": np.full(shape, 60.0),
        "silt_pct": np.full(shape, 30.0),
        "clay_pct": np.full(shape, 10.0),
    }


class CoupledEventTest(unittest.TestCase):
    def test_runon_can_infiltrate_and_saturate_downslope(self):
        # Left cell is impermeable and routes east. The shallow right profile
        # must admit some of that runon, so its infiltration exceeds the 8 mm
        # that fell locally. This is impossible in the old per-pixel model.
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        soils = synthetic_soils((1, 2), ksat=[[0.0, 20.0]],
                                restriction_cm=[[np.nan, 5.0]])
        water = scenario.simulate_coupled_event(
            8.0, 1.0, fields, soils, antecedent="normal", steps=1)

        self.assertAlmostEqual(water["infiltration_mm"][0, 0], 0.0, places=8)
        self.assertGreater(water["infiltration_mm"][0, 1], 8.0)
        self.assertGreater(water["runon_infiltration_mm"][0, 1], 0.0)
        # It reaches saturation during routing, then drains toward field
        # capacity during the second half-step.
        self.assertGreater(water["saturation_pct"][0, 1], 80.0)
        self.assertGreater(water["saturation_excess_mm"][0, 1], 0.0)
        self.assertLess(water["mass_balance_relative_error"], 1e-10)

    def test_zero_conductivity_routes_everything_to_the_boundary(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        soils = synthetic_soils((1, 2), ksat=0.0)
        water = scenario.simulate_coupled_event(
            10.0, 1.0, fields, soils, steps=1)

        self.assertAlmostEqual(float(np.nansum(water["infiltration_mm"])), 0.0)
        self.assertAlmostEqual(water["boundary_outflow_m3"], 0.02, places=10)
        self.assertLess(water["mass_balance_relative_error"], 1e-10)

    def test_high_capacity_absorbs_a_small_event(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        soils = synthetic_soils((1, 2), ksat=20.0)
        water = scenario.simulate_coupled_event(
            1.0, 1.0, fields, soils, steps=1)

        self.assertAlmostEqual(water["boundary_outflow_m3"], 0.0, places=10)
        self.assertAlmostEqual(float(np.nansum(water["infiltration_mm"])), 2.0,
                               places=8)
        self.assertLess(water["mass_balance_relative_error"], 1e-10)

    def test_depression_retains_water_until_its_finite_volume_fills(self):
        # The center cell has 0.01 m3 of depression capacity and is the only
        # path from the forced left/center cells to the right-hand outlet.
        fields = synthetic_fields(
            [[2.0, 0.99, 1.0]], [[4, 4, -1]], [[0.0, 0.01, 0.0]])
        soils = synthetic_soils((1, 3), ksat=0.0)

        below = scenario.simulate_coupled_event(
            np.array([[4.0, 4.0, 0.0]]), 1.0, fields, soils, steps=1)
        self.assertAlmostEqual(below["boundary_outflow_m3"], 0.0, places=10)
        self.assertAlmostEqual(below["depression_storage_m3"], 0.008, places=10)

        above = scenario.simulate_coupled_event(
            np.array([[10.0, 10.0, 0.0]]), 1.0, fields, soils, steps=1)
        self.assertAlmostEqual(above["depression_storage_m3"], 0.01, places=8)
        self.assertAlmostEqual(above["boundary_outflow_m3"], 0.01, places=8)
        self.assertLess(above["mass_balance_relative_error"], 1e-10)

    def test_frozen_profile_reduces_infiltration(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        soils = synthetic_soils((1, 2), ksat=10.0)
        thawed = scenario.simulate_coupled_event(
            50.0, 1.0, fields, soils, steps=2)
        frozen = scenario.simulate_coupled_event(
            50.0, 1.0, fields, soils, frozen=True, steps=2)

        self.assertLess(np.nansum(frozen["infiltration_mm"]),
                        np.nansum(thawed["infiltration_mm"]))
        self.assertGreater(frozen["boundary_outflow_m3"],
                           thawed["boundary_outflow_m3"])
        self.assertLess(frozen["mass_balance_relative_error"], 1e-10)

    def test_shallow_bedrock_blocks_free_lower_boundary_drainage(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        free_soil = synthetic_soils((1, 2), ksat=[[0.0, 20.0]])
        restricted_soil = synthetic_soils(
            (1, 2), ksat=[[0.0, 20.0]],
            restriction_cm=[[np.nan, 5.0]], bedrock_cm=[[np.nan, 5.0]])

        free = scenario.simulate_coupled_event(
            80.0, 8.0, fields, free_soil, antecedent="normal", steps=8)
        restricted = scenario.simulate_coupled_event(
            80.0, 8.0, fields, restricted_soil, antecedent="normal", steps=8)

        self.assertGreater(free["deep_drainage_mm"][0, 1], 0.0)
        self.assertAlmostEqual(restricted["deep_drainage_mm"][0, 1], 0.0, places=10)
        self.assertGreater(restricted["saturation_excess_mm"][0, 1], 0.0)
        self.assertGreater(restricted["boundary_outflow_m3"],
                           free["boundary_outflow_m3"])
        self.assertLess(restricted["mass_balance_relative_error"], 1e-10)

    def test_bedrock_below_profile_has_finite_subprofile_storage(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        free_soil = synthetic_soils((1, 2), ksat=[[0.0, 20.0]])
        near_bedrock = synthetic_soils(
            (1, 2), ksat=[[0.0, 20.0]],
            restriction_cm=[[np.nan, 75.0]], bedrock_cm=[[np.nan, 75.0]])

        free = scenario.simulate_coupled_event(
            200.0, 24.0, fields, free_soil, steps=24)
        finite = scenario.simulate_coupled_event(
            200.0, 24.0, fields, near_bedrock, steps=24)
        capacity = finite["state"]["lower_boundary_capacity_mm"][0, 1]

        self.assertGreater(capacity, 0.0)
        self.assertAlmostEqual(
            finite["deep_drainage_mm"][0, 1], capacity, places=8)
        self.assertLess(finite["deep_drainage_mm"][0, 1],
                        free["deep_drainage_mm"][0, 1])
        self.assertGreater(finite["boundary_outflow_m3"],
                           free["boundary_outflow_m3"])
        self.assertLess(finite["mass_balance_relative_error"], 1e-10)

    def test_multicell_event_closes_machine_precision_budget(self):
        fields = synthetic_fields(
            [[4.0, 3.0], [2.0, 1.0]], [[7, 6], [4, -1]])
        soils = synthetic_soils((2, 2), ksat=[[0.5, 3.0], [8.0, 15.0]])
        water = scenario.simulate_coupled_event(
            40.0, 6.0, fields, soils, antecedent="wet", steps=6)

        accounted = (water["root_zone_storage_m3"] + water["deep_drainage_m3"] +
                     water["depression_storage_m3"] + water["boundary_outflow_m3"])
        self.assertAlmostEqual(water["input_m3"], accounted, places=10)
        self.assertLess(water["mass_balance_relative_error"], 1e-10)

    def test_relative_vsa_initialization_targets_only_high_twi(self):
        fields = synthetic_fields([[3.0, 2.0, 1.0]], [[-1, -1, -1]])
        fields["twi"] = np.array([[2.0, 6.0, 12.0]])
        soils = synthetic_soils((1, 3), ksat=10.0)
        water = scenario.simulate_coupled_event(
            40.0, 2.0, fields, soils, antecedent="normal", steps=2)

        self.assertAlmostEqual(water["local_runoff_mm"][0, 0], 0.0, places=8)
        self.assertGreater(water["local_runoff_mm"][0, 2],
                           water["local_runoff_mm"][0, 0])
        self.assertGreater(water["saturation_pct"][0, 2],
                           water["saturation_pct"][0, 0])

    def test_equal_twi_values_receive_equal_midrank(self):
        values = np.ones((1, 4))
        rank = scenario.tied_percentile_rank(values, np.ones_like(values, dtype=bool))
        np.testing.assert_allclose(rank, 0.5)

    def test_auto_antecedent_wetness_interpolates_physical_state(self):
        fields = synthetic_fields([[2.0, 1.0]], [[4, -1]])
        soils = synthetic_soils((1, 2), ksat=10.0)
        dry = scenario.hydraulic_state(
            soils, fields, "dry", antecedent_wetness=0.0)
        wet = scenario.hydraulic_state(
            soils, fields, "wet", antecedent_wetness=1.0)

        self.assertAlmostEqual(dry["antecedent_fill_fraction"], 0.2)
        self.assertAlmostEqual(wet["antecedent_fill_fraction"], 0.9)
        self.assertAlmostEqual(dry["vsa_twi_threshold"], 0.97)
        self.assertAlmostEqual(wet["vsa_twi_threshold"], 0.65)
        self.assertGreater(np.nanmean(wet["theta_initial"]),
                           np.nanmean(dry["theta_initial"]))

    def test_halving_time_step_has_small_effect(self):
        fields = synthetic_fields(
            [[4.0, 3.0], [2.0, 1.0]], [[7, 6], [4, -1]])
        soils = synthetic_soils((2, 2), ksat=[[0.5, 3.0], [8.0, 15.0]])
        hourly = scenario.simulate_coupled_event(
            40.0, 6.0, fields, soils, antecedent="wet", steps=6)
        half_hourly = scenario.simulate_coupled_event(
            40.0, 6.0, fields, soils, antecedent="wet", steps=12)

        np.testing.assert_allclose(
            hourly["infiltration_mm"], half_hourly["infiltration_mm"],
            rtol=0.02, atol=0.05)
        np.testing.assert_allclose(
            hourly["saturation_pct"], half_hourly["saturation_pct"],
            rtol=0.005, atol=0.05)

    def test_supported_maximum_event_keeps_hourly_steps(self):
        self.assertEqual(scenario.routing_step_count(30.0 * 24.0), 720)


if __name__ == "__main__":
    unittest.main()
