import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadSimulationTestApi() {
  const source = fs.readFileSync(new URL('../public/simulation.js', import.meta.url), 'utf8');
  const window = { __VEIL_SIMULATION_TEST__: true };
  vm.runInNewContext(source, { window });
  return window.VEILSimulation._test;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test('flow paths keep existing hectare sentence wording', () => {
  const api = loadSimulationTestApi();

  assert.equal(api.flowSampleToHa({
    value: 0.25,
    grid: { value_unit: 'ha' },
  }).ha, 0.25);
  assert.equal(
    api.flowSentenceForHa(0.25, 3.63),
    'A defined flow path: water from about 2,500 m² (0.25 ha) upslope funnels through here when it runs.',
  );
});

test('flow paths can convert square meters to hectares', () => {
  const api = loadSimulationTestApi();

  assert.deepEqual(plain(api.flowSampleToHa({
    value: 2500,
    grid: { value_unit: 'm2' },
  })), { ha: 0.25, reason: null });
});

test('flow paths can convert cells when cell area metadata is present', () => {
  const api = loadSimulationTestApi();

  assert.deepEqual(plain(api.flowSampleToHa({
    value: 100,
    grid: { value_unit: 'cells', cell_area_m2: 9.3636 },
  })), { ha: 0.093636, reason: null });
});

test('flow paths with missing or unknown units refuse area conversion', () => {
  const api = loadSimulationTestApi();

  assert.deepEqual(plain(api.flowSampleToHa({ value: 0.25, grid: {} })), {
    ha: null,
    reason: 'missing-unit',
  });
  assert.deepEqual(plain(api.flowSampleToHa({
    value: 0.25,
    grid: { value_unit: 'banana' },
  })), {
    ha: null,
    reason: 'unknown-unit',
  });
});

test('simulation result view keeps full response wording', () => {
  const api = loadSimulationTestApi();

  const view = api.simulationResultView({
    scenario: { label: 'Big winter' },
    water_input: { total_mm: 147.3, total_m3_on_aoi: 40149 },
    partition: {
      runoff_mm_mean: 5.7,
      runoff_pct: 3.8,
      runoff_m3: 1542,
      infiltration_mm_mean: 141.7,
      infiltration_m3: 38607,
      root_zone_storage_mm_mean: 50.0,
      root_zone_storage_m3: 13625,
      deep_drainage_mm_mean: 91.7,
      deep_drainage_m3: 24982,
      saturation_excess_mm_mean: 0.0,
      saturation_excess_m3: 0,
      profile_saturation_pct_mean: 74.4,
      initial_profile_saturation_pct_mean: 43.3,
      profile_saturation_change_pct_points_mean: 31.1,
    },
    outlet: { peak_discharge_cfs_est: 8.2, event_volume_m3: 1200 },
    ponding: {
      depression_storage_m3: 350,
      retained_water_m3: 140,
      storage_fill_pct: 40,
      storage_filled: false,
    },
    notes: ['geometry is reliable; discharge magnitude is scenario-grade'],
  });

  assert.equal(view.label, 'Big winter');
  assert.deepEqual(plain(view.rows), [
    ['Water input', '147 mm · 40,149 m³ on the land'],
    ['Not infiltrated', '6 mm (3.8%) · 1,542 m³'],
    ['Infiltrated', '142 mm · 38,607 m³'],
    ['Profile water gain', '50 mm · 13,625 m³'],
    ['Profile percolation', '92 mm · 24,982 m³'],
    ['Local saturation excess', '0 mm · 0 m³ generated'],
    ['Mean profile saturation', '74.4% final · 43.3% antecedent · +31.1 points'],
    ['Outlet peak', '~8.2 cfs (screening)'],
    ['Outlet volume', '1,200 m³ over the event'],
    ['Ponds & pools', '140 m³ retained · 40% of 350 m³ capacity'],
  ]);
  assert.equal(view.note, 'geometry is reliable; discharge magnitude is scenario-grade');
  assert.deepEqual(plain(view.missingFields), []);
});

test('simulation result view tolerates partial responses with fallbacks', () => {
  const api = loadSimulationTestApi();

  const view = api.simulationResultView({
    scenario: {},
    water_input: { total_mm: 42 },
    outlet: { event_volume_m3: 900 },
  });

  assert.equal(view.label, 'Scenario result');
  assert.deepEqual(plain(view.rows), [
    ['Water input', '42 mm · unknown m³ on the land'],
    ['Not infiltrated', 'unknown mm (unknown) · unknown m³'],
    ['Infiltrated', 'unknown mm · unknown m³'],
    ['Outlet peak', '~unknown cfs (screening)'],
    ['Outlet volume', '900 m³ over the event'],
  ]);
  assert.deepEqual(plain(view.missingFields), [
    'scenario.label',
    'water_input.total_m3_on_aoi',
    'partition.runoff_mm_mean',
    'partition.runoff_pct',
    'partition.runoff_m3',
    'partition.infiltration_mm_mean',
    'partition.infiltration_m3',
    'outlet.peak_discharge_cfs_est',
  ]);
});

test('simulation result suppresses unresolved trace peak flow', () => {
  const api = loadSimulationTestApi();
  const view = api.simulationResultView({
    scenario: { label: 'Slow melt' },
    water_input: { total_mm: 177.8, total_m3_on_aoi: 48456 },
    partition: {
      runoff_mm_mean: 0.3,
      runoff_pct: 0.2,
      runoff_m3: 76,
      infiltration_mm_mean: 177.5,
      infiltration_m3: 48380,
    },
    outlet: {
      event_volume_m3: 56,
      peak_discharge_cfs_est: null,
      below_screening_resolution: true,
    },
  });

  assert.deepEqual(plain(view.rows.slice(3, 5)), [
    ['Outlet peak', 'Below screening resolution (<0.2% of input)'],
    ['Outlet volume', '56 m³ over the event · budget bookkeeping only'],
  ]);
  assert.equal(view.missingFields.includes('outlet.peak_discharge_cfs_est'), false);
});
