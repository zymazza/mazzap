# Plan: branchable land alternatives

Plan is VEIL's non-destructive design workspace. A user can remove inventoried
trees or shrubs, plant species-aware trees/shrubs by point or brush, cut
depressions, and place fill/mounds directly on the interactive 3D terrain.
Higher-level swale, orchard, and garden concepts remain available to GAIA as
reviewable compositions of those primitives instead of duplicating the viewer's
brushes. Every completed gesture autosaves as an immutable revision; named
checkpoints and branches preserve alternatives.

The baseline twin is never edited. A revision materializes a complete effective
terrain and vegetation bundle, and simulations run in a revision-scoped
workspace so results cannot leak between branches.

## Viewer workflow

1. Open **Plan** from the left rail and create or select a plan.
2. Pick a brush. Point-click plants one tree/bush; dragging paints plants or
   earthworks. The terrain deforms, plants appear, and removed vegetation
   disappears while the pointer is moving; there is no outline-then-result
   delay. Trees and shrubs remain seated on the terrain during live earthwork
   previews. Holding an earth brush stationary, or painting repeatedly over the
   same ground, continues to accumulate the selected depth/height at that amount
   per second. Completed earth strokes autosave in order in the background, so
   another earth stroke can begin immediately without switching to camera
   navigation. Species is a searchable text field backed by regional suggestions,
   but any non-empty custom species name is accepted with disclosed generic
   visualization dimensions. Modeled stage, spacing, brush radius, and earth
   depth or height remain explicit controls.
3. Hold **Ctrl** (or **Command** on macOS) while clicking/dragging to navigate
   the 3D camera without leaving the active brush. Leaving or closing the Plan
   pane automatically returns the tool to **Navigate** (and cancels an unfinished
   gesture) while keeping the selected plan terrain active for picking and
   simulations.
4. Switch between **Baseline**, **Planned**, and **Difference**. Difference
   colors earthworks/semantic features and marks removals.
5. Use Undo/Redo, **Save version** for a named checkpoint, or choose any older
   version to revisit/simulate it. Historical versions are read-only; **Branch**
   turns the selected old version into a new editable alternative.
   **Discard** archives the complete plan and hides it from the active list
   without deleting shared revision artifacts.
6. Open Simulation and run hydrology, wildfire, Water & ET, solar, or viewshed.
   When a plan is active, the existing controls route to that plan automatically.

Edits outside the finite AOI are rejected. A planting brush is clipped to the
editable land, unknown vegetation IDs are rejected, and stale writers receive a
409 conflict instead of overwriting a newer revision.

## Persistence and materialization

Schema version 2 adds `plan_bases`, `plans`, `plan_revisions`, `plan_edits`, and
`plan_simulation_runs` to the journal-rebuildable GeoPackage. Revisions carry a
complete canonical edit snapshot and a parent pointer. A plan head advances by
compare-and-swap using `expected_revision_id`; branch ancestry can cross the
source plan only at the explicit fork revision.

The first plan pins a content-hashed copy of the baseline terrain, vegetation,
scene, AOI, and georeference under:

```text
<data>/plans/bases/<base_id>/
```

Effective land is deterministic and content-addressed:

```text
<data>/plans/cache/<content_hash>/
```

Each revision gets a lightweight runtime facade with its own simulation output:

```text
<data>/plans/revisions/<revision_id>/
<data>/plans/runs/<plan_run_id>/result.json
```

The facade shares immutable terrain/vegetation artifacts but isolates mutable
hydrology, fire, ET, and solar catalogs. The 135 MB-class GeoPackage copy and
effective vegetation rewrite are lazy: ordinary brush saves only rebuild viewer
JSON; the store is created when a simulation needs it.

## Simulation effects

| Simulator | Planned terrain | Planned vegetation |
|---|---|---|
| Hydrology | Recomputed flow, wetness, depressions, routing and storage | Not an input to the current terrain/SSURGO event solver |
| Wildfire | Effective elevation/slope | Effective crowns/heights and computed fuels; new-plant CBH/CBD are disclosed screening defaults |
| Water & ET | Effective terrain redistribution | Effective canopy-cover water balance |
| Solar | Effective terrain horizon | Effective crown clearance and canopy horizon |
| Viewshed | Effective ground | Effective canopy blockers |

Every plan run records plan ID, revision ID, land content hash, parameters,
input hash, status, timestamps, artifact path, and result. Results also include a
`plan_effects` disclosure so clients do not imply unsupported coupling.

## REST API

The zero-dependency server exposes:

```text
GET  /api/plans
POST /api/plans
GET  /api/plans/catalog
GET  /api/plans/:plan_id
GET  /api/plans/:plan_id/revisions/:revision_id
POST /api/plans/:plan_id/commit
POST /api/plans/:plan_id/checkpoint
POST /api/plans/:plan_id/branch
POST /api/plans/:plan_id/update
POST /api/plans/:plan_id/revisions/:revision_id/simulations/:kind
```

Mutating browser requests use the server's same-origin protection. Request
bodies are bounded, edit geometry/count/radius/depth are clamped or rejected,
and Python owns domain validation and journal writes. The viewer's Discard
action calls `update` with `archived=true`; archived plans are omitted from the
normal list but remain journal-rebuildable.

## GAIA / MCP workflow

Discovery and lifecycle tools are `list_plans`, `get_plan`,
`planning_catalog`, `create_plan`, `branch_plan`, and `save_plan_version`.
`propose_plan_edits` handles the generic edit model; `propose_swale`,
`propose_orchard`, and `propose_garden` are semantic helpers that compose the
same depression, planting, and mound primitives without adding redundant viewer
brushes. Orchard polygons are filled deterministically at catalog spacing.

Proposals are deliberately two-step:

1. A `propose_*` tool validates the prospective full snapshot, computes a
   screening quantity summary, writes an ephemeral proposal, and (by default)
   opens the live Plan Difference view through `data/annotations.json`.
   Spatial vegetation removals resolve scene-local geometry plus `buffer_m`
   into frozen effective entity IDs before that preview; zero matches are
   rejected instead of producing an accept-able no-op.
2. GAIA asks the user to review it. `apply_plan_proposal` refuses to write
   unless `confirmed=true`, then creates a normal immutable revision. A stale
   proposal fails with a plan conflict.

The review surface is the user's already-open VEIL viewer. Agents must not open
a second Playwright/Chromium/CUA viewer merely to inspect the directive; if no
viewer is open, they ask the user to open one before approval. A viewer opened
after the proposal consumes the existing `plan_view` on its initial read.

`visualize_plan` and `clear_plan_visualization` are presentation-only.
`run_plan_simulation` executes the same plan-aware engines as the viewer and can
target any reachable immutable revision, including a saved historical version.

## Verification and current limits

Run the focused engine, replay, agent-surface, and real HTTP tests with:

```bash
npm run test:plan
```

- Earthworks resolve on the pinned terrain grid; the UI reports cell size and
  warns about features narrower than three cells.
- Species stages are visualization/screening dimensions, not growth forecasts.
- Swales still require field survey, overflow design, and engineering review.
- Garden yield/irrigation and orchard cultivar/rootstock/deer/cold-air behavior
  are outside the present simulation scope.
