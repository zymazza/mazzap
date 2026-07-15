import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const readJson = (relative) => JSON.parse(fs.readFileSync(new URL(relative, import.meta.url), 'utf8'));
const readText = (relative) => fs.readFileSync(new URL(relative, import.meta.url), 'utf8');

test('canonical flight schema and checked example stay aligned at the frozen v1 seam', () => {
  const schema = readJson('../docs/contracts/flight-mission.schema.json');
  const example = readJson('../docs/contracts/flight-mission.example.json');

  assert.equal(schema.$schema, 'https://json-schema.org/draft/2020-12/schema');
  assert.match(schema.$id, /flight-mission-v1/);
  assert.deepEqual(schema.required, ['id', 'name', 'drone_model', 'takeoff', 'defaults', 'waypoints']);
  assert.equal(schema.properties.drone_model.const, 'mini4pro');
  assert.equal(example.drone_model, schema.properties.drone_model.const);
  schema.required.forEach((key) => assert.ok(Object.hasOwn(example, key), `example is missing ${key}`));
  schema.$defs.defaults.required.forEach((key) => assert.ok(Object.hasOwn(example.defaults, key), `defaults are missing ${key}`));
  schema.$defs.waypoint.required.forEach((key) => assert.ok(Object.hasOwn(example.waypoints[0], key), `waypoint is missing ${key}`));
  assert.equal(example.waypoints[0].derived.exec_height_rel_m, 53.2);
});

test('durable implementation plan retains every numbered work section', () => {
  const plan = readText('../docs/flight-planning-implementation-plan-v1.5.md');

  assert.match(plan, /^# VEIL Flight Planning — Implementation Plan v1\.5/m);
  for (let section = 1; section <= 14; section += 1) {
    assert.match(plan, new RegExp(`^## ${section}\\.`, 'm'));
  }
  assert.doesNotMatch(plan, /PLAN-CONTINUES/);
  assert.match(plan, /Canonical flight mission JSON Schema/);
  assert.match(plan, /VEIL ↔ Android bridge contract/);
});

test('bridge contract freezes mission, telemetry, video, and control seams', () => {
  const contract = readText('../docs/bridge-contract.md');

  assert.match(contract, /veil-drone-bridge\/1\.0/);
  assert.match(contract, /`GET \/api\/flights`/);
  assert.match(contract, /`GET \/api\/flights\/:id\/artifact\.kmz`/);
  assert.match(contract, /`POST \/api\/flights\/:id\/events`/);
  assert.match(contract, /`WS \/api\/drone-telemetry`/);
  assert.match(contract, /`WS \/api\/drone-control`/);
  assert.match(contract, /tcp:8426/);
  assert.match(contract, /default command TTL is 500 ms/);
  assert.match(contract, /No unchecked setpoint may leave VEIL/);
});
