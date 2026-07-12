#!/usr/bin/env node
const assert = require('node:assert/strict');

const api = require('../server.js')._test;

assert.equal(api.isPlanEditIntent('Remove all trees within 10m of the creek'), true);
assert.equal(api.isPlanEditIntent('Plant an orchard in this plan'), true);
assert.equal(api.isPlanEditIntent('How many trees are near the creek?'), false);
assert.equal(api.isPlanEditIntent('What soil types occur here?'), false);
assert.equal(api.isPlanConfirmationIntent('make the changes'), true);
assert.equal(api.isPlanConfirmationIntent('Yes, go ahead'), true);
assert.equal(api.isPlanConfirmationIntent('apply it please'), true);
assert.equal(api.isPlanConfirmationIntent('Apply proposal_fixture123'), true);
assert.equal(api.isPlanConfirmationIntent("don't apply it yet"), false);
assert.equal(api.isPlanConfirmationIntent('make the changes but keep the pines'), false);

const confirmation = api.planConfirmationContext(
  'make the changes',
  [{ role: 'assistant', content: 'Preview ready: proposal_history' }],
  {
    annotations: { plan_view: { proposal_id: 'proposal_active' } },
    loadProposal(proposalId) {
      return {
        proposal_id: proposalId,
        status: 'proposed',
        plan_id: 'plan_fixture',
        expected_revision_id: 'rev_fixture',
        label: 'Creek clearance',
        preview: { vegetation: { entities_removed: 1734 } },
      };
    },
  },
);
assert.equal(confirmation.proposal_id, 'proposal_active');
assert.equal(confirmation.status, 'proposed');
const alreadyApplied = api.planConfirmationContext(
  'do it', [{ role: 'assistant', content: 'proposal_applied' }], {
    annotations: { plan_view: null },
    loadProposal(proposalId) {
      return {
        proposal_id: proposalId, status: 'applied', plan_id: 'plan_fixture',
        expected_revision_id: 'rev_old', applied_revision_id: 'rev_applied',
        label: 'Creek clearance', preview: {},
      };
    },
  });
assert.equal(alreadyApplied.status, 'applied');
assert.equal(alreadyApplied.applied_revision_id, 'rev_applied');
assert.equal(api.planConfirmationContext(
  "don't apply it", [], {
    annotations: { plan_view: { proposal_id: 'proposal_active' } },
    loadProposal() { throw new Error('negative confirmation must not load'); },
  }), null);
assert.match(api.appliedPlanReply(confirmation, {
  revision: { revision_id: 'rev_applied' },
}), /1,734/);
assert.match(api.appliedPlanReply(confirmation, {
  revision: { revision_id: 'rev_applied' },
}), /rev_applied/);

const routing = api.planEditDynamicInstructions(
  'Remove all trees within 10m of the creek');
assert.match(routing, /do NOT\s+enumerate/i);
assert.match(routing, /propose_vegetation_clearance/);
assert.match(routing, /stop calling tools/i);
assert.equal(api.ecologyDynamicInstructions(
  'Remove all trees within 10m of the creek'), '');
assert.match(api.ecologyDynamicInstructions(
  'Which wildlife habitat is near the creek?'), /THEMATIC PREFLIGHT/);

assert.equal(api.successfulPlanTerminalTool('propose_plan_edits', JSON.stringify({
  proposal_id: 'proposal_fixture', preview: { vegetation: { entities_removed: 3 } },
})), true);
assert.equal(api.successfulPlanTerminalTool('propose_vegetation_clearance', JSON.stringify({
  proposal_id: 'proposal_fixture',
})), true);
assert.equal(api.successfulPlanTerminalTool('propose_plan_edits', JSON.stringify({
  error: 'empty_vegetation_removal',
})), false);
assert.equal(api.successfulPlanTerminalTool('apply_plan_proposal', JSON.stringify({
  revision: { revision_id: 'rev_fixture' },
  proposal: { status: 'applied' },
})), true);
assert.equal(api.successfulPlanTerminalTool('find_entities', JSON.stringify({
  proposal_id: 'proposal_fixture',
})), false);

const compact = JSON.parse(api.toolResultForModel(
  'propose_vegetation_clearance', JSON.stringify({
    proposal_id: 'proposal_fixture', status: 'proposed', plan_id: 'plan_fixture',
    expected_revision_id: 'rev_fixture', label: 'Creek clearance',
    edits: Array.from({ length: 120 }, (_, index) => ({ edit_id: `old_${index}` })),
    proposed_edits: [{
      edit_id: 'edit_clearance', kind: 'vegetation_remove',
      geometry: { type: 'LineString', coordinates: [[0, 0], [1, 1]] },
      params: {
        buffer_m: 10, kinds: ['tree'],
        entity_ids: Array.from({ length: 1904 }, (_, index) => `tree:${index}`),
      },
    }],
    preview: { vegetation: { entities_removed: 1904 } },
    visualization: {
      plan_id: 'plan_fixture', revision_id: 'rev_fixture',
      proposal_id: 'proposal_fixture', view: 'difference',
      preview_edits: [{ params: { entity_ids: Array(1904).fill('tree') } }],
    },
  })));
assert.equal(compact.preview.vegetation.entities_removed, 1904);
assert.equal(compact.proposed_edits[0].params.entity_count, 1904);
assert.equal('entity_ids' in compact.proposed_edits[0].params, false);
assert.equal('edits' in compact, false);
assert.ok(JSON.stringify(compact).length < 5000);
assert.ok(api.MAX_TOOL_ROUNDS >= 16);

console.log('chat_plan_routing_test: ok');
