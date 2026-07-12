import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

function loadRenderPolicyApi() {
  const source = fs.readFileSync(new URL('../public/viewer/scene.js', import.meta.url), 'utf8');
  const window = { THREE: {} };
  vm.runInNewContext(source, { window });
  return window.VEILViewer?._test;
}

test('software WebGL backends are detected from renderer descriptions', () => {
  const api = loadRenderPolicyApi();
  assert.equal(api.isSoftwareRenderer('ANGLE (Google, Vulkan, SwiftShader driver)'), true);
  assert.equal(api.isSoftwareRenderer('llvmpipe (LLVM 19.1.7, 256 bits)'), true);
  assert.equal(api.isSoftwareRenderer('ANGLE (AMD, Radeon 780M, OpenGL ES 3.2)'), false);
  assert.equal(api.isSoftwareRenderer('ANGLE (NVIDIA, GeForce RTX 4090, Vulkan)'), false);
});

test('render cadence stays smooth during hardware interaction and backs off at idle', () => {
  const api = loadRenderPolicyApi();
  const hardwareActive = api.renderFrameIntervalMs({ active: true });
  const hardwareIdle = api.renderFrameIntervalMs({ active: false });
  const softwareActive = api.renderFrameIntervalMs({ software: true, active: true });
  const softwareIdle = api.renderFrameIntervalMs({ software: true, active: false });

  assert.ok(hardwareActive < hardwareIdle);
  assert.ok(hardwareIdle < softwareIdle);
  assert.ok(softwareActive < softwareIdle);
  assert.equal(api.renderFrameIntervalMs({ hidden: true }), Infinity);
});

test('frame gating renders immediately, waits for cadence, and pauses hidden pages', () => {
  const api = loadRenderPolicyApi();
  assert.equal(api.renderFrameDue(100, null, 50), true);
  assert.equal(api.renderFrameDue(120, 100, 50), false);
  assert.equal(api.renderFrameDue(150, 100, 50), true);
  assert.equal(api.renderFrameDue(150, 100, Infinity), false);
});
