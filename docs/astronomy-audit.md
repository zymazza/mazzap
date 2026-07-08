# Astronomy implementation audit — findings & fix list

> Post-implementation review of the docs/astronomy.md build (2026-07-07), by
> Claude against the uncommitted working tree. Verified green before review:
> twin_query_test 222/222; Horizons validation max 0.46′ across 9 bodies
> (thresholds 1′/2′); JS↔Python parity < 0.03″; Polaris alt 42.83° vs lat
> 43.28°; solstice-noon sun alt 70.16°; 2024-04-08 eclipse local obscuration
> 0.9912. The positional core is sound. The items below are the gaps.
>
> **Instructions to the fixer:** implement every item in §1 and §2 in order;
> §3 items are spec-text updates only (codify the as-built behavior in
> docs/astronomy.md — do not change code). After fixes: run
> `python3 scripts/twin_query_test.py`, `node scripts/astronomy_parity_test.js`,
> `.venv-mcp/bin/python scripts/astronomy_validate.py`, and a browser smoke of
> the master toggle round-trip. Check items off in this file as you land them.

## 1. Bugs (fix in this order)

- [x] **1.1 annotations.js — transient poll failure wipes drawings/layer views/sky state.**
  `fetchAnnotationDoc` resolves `null` on XHR error/timeout/JSON-parse failure,
  so the old `catch { return; }` ("unreadable = unchanged") is dead code: a
  null result falls through with `stamp='absent'` ≠ last stamp → `rebuild([])`
  clears every drawing and `applyLayerViews([])` drops agent layers; 4 s later
  everything flashes back. One slow poll during `/api/simulate` triggers it.
  **Fix:** when `fetchAnnotationDoc()` returns null, `return` from
  `refreshImpl` without touching state (keep the XHR/timeout rewrite if you
  like, just restore the unchanged-on-unreadable semantics).

- [x] **1.2 astronomy.js `applySkyDirectives` — no per-directive change detection;
  any annotations-doc change stomps the user's clock.** After clear-on-open the
  doc always carries `view_time: null`, so every unrelated signature change
  (GAIA draws a polygon, toggles a layer view) re-invokes
  `applySkyDirectives(views, null)` → `setNow()`, yanking a manually-scrubbed
  clock back to realtime; symmetrically a stale non-null `view_time` restarts
  play from its old ISO on every unrelated change. Spec §9.2 requires manual
  wins until the next *sky* directive change.
  **Fix:** remember the last-applied values (e.g. `JSON.stringify` of
  `view_time` and of `sky_views`) inside astronomy.js; act on each key only
  when its serialized value differs from the last applied one. Baseline both
  as `null`/`[]` at create so the boot doc doesn't trigger a spurious
  `setNow()`.

- [x] **1.3 scene.js `setPhotometricMode(false)` — `sunLight.visible` not
  restored.** `updatePhotometricSky` (line ~755) sets
  `sunLight.visible = sunIntensity > 0.0001` (false at night / totality); the
  disable path (~708-732) restores color/intensity/position/castShadow/target
  but never `visible`. Toggle off after sunset → boot light stays invisible,
  scene ambient-only until reload. **Fix:** restore `visible` (snapshot or
  hardcode `true`, boot value); audit the disable path once more for any other
  prop `updatePhotometricSky` mutates (e.g. exposure is fine, it's restored).

- [x] **1.4 POV mode + feature-off fidelity.** The sky pass is installed
  unconditionally and `sky.skyActive()` is hard-coded `true`, so the POV
  gating added to pov.js can never take the classic branch: POV permanently
  loses its flat sky color **and its FogExp2**, even with the master switch
  off and the pane never opened — a regression on an untouched feature.
  **Decision (do it this way):** the sky pass stays always-on (sun/moon/stars
  by default is the product intent; the dark night background with master off
  is accepted — see §3.1). What gates POV is the *lighting* mode:
  astronomy.js exposes `photometricOn()`, and pov.js installs its flat
  sky/fog whenever `!__twin.astronomy?.photometricOn?.()` (i.e. classic POV
  look unless physical lighting is on). Remove or repurpose `skyActive()` so
  no caller can confuse "pass exists" with "physical sky on".

- [x] **1.5 Deterministic screenshot plates.** With the pass always-on, night
  plates depend on wall clock. **Fix:** in `scripts/screenshot.js` and
  `scripts/cam_shot.js`, after boot, set a fixed clock whenever
  `window.__twin.astronomy` exists:
  `__twin.astronomy.clock.setManual(Date.parse(process.env.ASTRO_TIME || '2026-06-21T16:00:00Z'), {rate: 0, playing: false})`
  (pass the value in via page.evaluate). `ASTRO_TIME` env overrides.

- [x] **1.6 sky.js — Sky dome material `toneMapped = false` defeats the
  exposure model.** The vendored r160 Sky shader ends in
  `#include <tonemapping_fragment>` and is designed for ACES + exposure;
  with `toneMapped: false` the dome renders raw HDR (clipped white near the
  sun) and `toneMappingExposure` — including the §7.1 eclipse dimming — has
  zero effect on the sky itself ("sky dims hard" acceptance fails).
  **Fix:** `toneMapped = true` on the dome material only (stars/sun/moon/
  planet markers stay `toneMapped: false`).

- [x] **1.7 sky.js `resolveHighlight` — `name.slice(0, 3)` misroutes stars.**
  `highlight({name: "Capella"})` → "Cap" matches the Capricornus abbr, enters
  the constellation branch, exact-name find fails, returns null — Capella (and
  Castor via "Cas", etc.) can never be highlighted. **Fix:** resolution order:
  explicit `target_type` wins; else exact body name; else exact star name;
  else constellation by abbr or full name, all case-insensitive; drop the
  `slice(0,3)` shortcut.

- [x] **1.8 sky.js `highlight()` bails when `!assets.loaded`, even for bodies.**
  An MCP `highlight_sky("mars")` or the eclipse-jump highlight landing before
  the catalogs finish downloading is silently dropped (permanently if the
  asset fetch failed). **Fix:** bodies resolve without catalogs; star/
  constellation highlights arriving early go into a pending list flushed when
  `loadAssets` completes.

- [x] **1.9 Per-frame ephemeris — implement the spec §5.2 throttles.**
  `setTime` currently does 9 bodies × 2 `Equator` + 2 `Horizon` +
  `Illumination`, the star matrix, and a `new THREE.Color` allocation every
  frame. **Fix:** sun+moon every frame (of-date Equator + airless Horizon
  only); planets recomputed when sim time moved > 60 s; star/constellation
  matrix when moved > 1 s; the J2000 Equator call moved out of the per-frame
  path (compute on identify only); reuse scratch Color/Matrix/Vector objects.
  Eclipse state can keep running per frame off the cached sun/moon.

- [x] **1.10 Shadow-map invalidation gaps.** (a) Async tree-library GLB loads
  (`vegetation.js loadTreeAsset` → internal render) never set
  `shadowMap.needsUpdate` — with the clock paused, late-loading trees render
  shadowless until the sun moves 0.1°; call
  `viewer.invalidateShadowMap('vegetation-asset')` on asset-load completion.
  (b) The *surrounding*-vegetation renderer (second VEILVegetation instance
  in app.js) and the terrain-apron mesh never get shadow flags — visible
  cast/receive seam at the AOI boundary. Give scene.js a small registry
  (`viewer.onPhotometricModeChange(cb)` or similar) and register both from
  app.js.

- [x] **1.11 twin_astro.py — fake star distances.** Every named star reports
  `"distance_light_years": 1000.0` (the `DefineStar` placeholder). GAIA will
  recite it as fact. **Fix:** omit the field (or `null`) for stars.

- [x] **1.12 twin_astro.py — `surface_albedo` parameter is inert.** The Bird
  ground-reflection/multiple-scattering term is never computed, so the listed
  `surface_albedo: 0.2` does nothing and GHI runs ~2–3 % low. **Fix:**
  implement Bird's ground-reflectance term (a few lines) so the parameter is
  real; re-run the sanity check (clear summer noon GHI ~975→~1000 W/m²).

- [x] **1.13 mcp_server.py — stale writer enumeration.** The instructions
  string still says `run_scenario, run_fire_scenario, the draw_* tools and the
  layer-view tools` are the only writers; add
  `set_view_time / highlight_sky / clear_sky_highlights` (annotations-file
  writers, never the store).

- [x] **1.14 twin_astro.py — star `ra_j2000`/`dec_j2000` include aberration**
  (`Equator(..., ofdate=False, aberration=True)`, up to ~20.5″ off catalog
  J2000). **Fix:** report catalog J2000 with `aberration=False`; keep the
  of-date/apparent values as they are.

## 2. Smaller gaps (fix after §1)

- [x] **2.1 Highlight force-shows the minimum layer** (spec §9.2): a highlight
  on a body/star whose layer is toggled off currently draws a ring around
  empty sky. While a highlight is active, force-show that object (constellation
  lines already do this); a manual layer toggle reclaims, and
  `clearHighlights` restores layer-driven visibility.
- [x] **2.2 Identify-card content** (spec §6.4): add the constellation line
  (`Astronomy.Constellation`) for every object; for the moon add next full/new;
  for the sun add today's rise/set. Compute on click only (these are
  search calls — never per frame).
- [x] **2.3 Moon orientation** (spec §6.3): apply libration (sub-observer
  lon/lat from `Astronomy.Libration`) so the familiar face points at the
  observer instead of an arbitrary `lookAt` roll. Position-angle refinement
  optional; note whichever approximation you land in the spec.
- [x] **2.4 golden_hour perf:** `_sun_alt` calls full `body_position`
  (rise/set searches + constellation) per 5-minute scan step; use a slim
  Equator+Horizon helper.
- [x] **2.5 Tests:** (a) reverse key-preservation — drawing/layer writers
  preserve `sky_views`/`view_time` (verified manually to work; encode it);
  (b) the spec §11 southern-hemisphere check (mock observer at lat −35°);
  (c) constellation-segment drops get a `console.warn` count (spec §3.2).
- [x] **2.6 Provenance blocks** on `set_view_time`, `highlight_sky`
  (constellation case), `clear_sky_highlights` responses, matching the fact
  tools.
- [x] **2.7 clearHighlights leak:** dispose ring/label geometries and canvas
  textures on clear (highlights are re-created on every directive change).

## 3. Codify in docs/astronomy.md (spec-text edits only — behavior is accepted)

- [x] **3.1** §1/§6.1: sky layers render whenever toggled on, independent of
  the master switch; the master gates *lighting/tone-mapping/shadows* only.
  At night with the master off, the backdrop goes dark and stars show — that
  is the feature, not a regression. POV keeps its classic flat sky/fog unless
  photometric mode is on (matches fix 1.4). Screenshot determinism via the
  fixed-clock default (fix 1.5).
- [x] **3.2** §3.2: as-built star catalog is 5,044 stars, mag ≤ 6.0 (the
  spec's "~8.9k / 6.5" claim was wrong for `stars.6.json`); moon texture is
  NASA SVS's pre-sized 1024×512 `lroc_color_poles_1k.jpg` (no gdal step).
- [x] **3.3** §7.4: corona ramps in over obscuration 0.94→0.995 rather than a
  hard 0.995 threshold.
- [x] **3.4** §10: the parity test compares vendored JS against live Python
  `twin_astro` (not reference-file exports) — equivalent and arguably better.
- [x] **3.5** Update the spec's status header to reflect built + audited, and
  reconcile any §12 acceptance text touched by 3.1.
