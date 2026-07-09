# Astronomy & sky simulation — architecture & implementation spec

> Status: **built and post-implementation audit fixes applied** (2026-07-07).
> This is the canonical spec the implementation follows; pin any interface
> change here, never edit code silently against it. Written for execution by
> GPT-5.5 Codex against this repo.
> Every `file:line` reference below was verified against the working tree on the
> date above (branch `wildfire-sim`); treat them as anchors, re-locate if drifted.

## 0. What this is

A **georeferenced sky**: the twin gains an accurate model of where the sun, moon,
planets, and stars are — right now (browser clock) and at any time in the past or
future — rendered into the 3D scene and driving a physically-based lighting mode
(sun/moon light, shadows from terrain, buildings, and trees, twilight, eclipses).
A new **Astronomy** rail pane holds the layer toggles and the time controls.
GAIA (the chat/MCP agent) can answer sky questions ("when is the next total
eclipse here?"), **set the viewer's clock** to show the answer, and **highlight a
body or constellation** on the live sky.

Decisions locked with the user:

| Decision | Choice |
|---|---|
| Ephemeris source | **Local engine** (`astronomy-engine`, MIT, same-author JS + Python ports, ±1 arcmin vs JPL). **JPL Horizons is the validation oracle**, fetched once per twin by an online script — never at view time. |
| Bodies | Sun, Moon (phase + libration), naked-eye planets (+ Uranus/Neptune faint), real star field, constellation lines. No satellites (TLEs go stale; out of scope). |
| Labels | **None persistent.** Click a body to identify it; GAIA can highlight + label one via MCP. |
| Sky/lighting fidelity | Sky layers render whenever toggled on. **Full physically-based lighting** is opt-in via the master switch: sun-position-driven light + shadow maps, atmospheric sky shader, phase-correct moonlight, eclipse darkening. Radiometric truth (W/m²) lives in a Python model, not in the shader — see §8. |
| Time UI | In the Astronomy pane. **Realtime = browser clock** (`Date.now()`), scrubber for arbitrary dates, play at selectable rates, "Now" snaps back. |
| Chat & clock | GAIA **can** set the viewer time and highlight sky targets, via the existing `annotations.json` channel (edge-triggered, manual wins). |
| Generalization | Zero pack involvement. Site = `data/georef.json`; star/constellation catalogs are **committed engine assets** under `public/astronomy-data/` that work for any twin, any hemisphere, offline. |

Honest framing (house style): **geometry is exact, photometry is perceptual.**
Positions, times, phases, and eclipse circumstances are arcminute-class and
validated against Horizons. The *rendered* brightness of the sky and lights is a
tone-mapped perceptual model tuned to look right, not a radiometric transfer —
the radiometric numbers (clear-sky DNI/GHI/DHI) come from `scripts/twin_astro.py`
and are reported as data, groundwork for the future solar-siting work.

Two rendering-accuracy caveats inside that claim: the viewer draws sun/moon/
planets at their **refracted** altitude ('normal' refraction, so on-screen
sunsets match MCP rise/set times) while the star field stays airless — the
sub-half-degree mismatch only exists at the horizon, where stars are extincted
anyway; and the star catalog has **no proper motion** (J2000 positions used
across the full 1600–2500 clock range — high-PM stars like Arcturus drift
~17 arcmin over ±450 years, well beyond the planetary arcminute claim, which
is unaffected).

## 1. Goals / non-goals

**Goals (v1):**
- Accurate topocentric positions for sun/moon/planets/stars at the twin's site,
  any date (UI-clamped to years 1600–2500; best accuracy 1800–2200), offline.
- Astronomy rail pane: layer toggles (Sun / Moon / Planets / Stars / Constellations
  as a Stars sub-toggle), time scrubber + play rates + Now, "jump to next
  sunset / sunrise / midday / night" buttons (solar noon and solar midnight via
  `SearchHourAngle`; eclipse/moon-event demonstration lives on the MCP side via
  `next_sky_event(demonstrate=true)`), master "Physical sky & lighting"
  switch (default **off** — with it off, sky layers still render when toggled on;
  the master gates lighting/tone-mapping/shadows).
- Physical lighting mode: ACES tone mapping, sun + moon directional lights,
  hemisphere/ambient from sky state, shadow maps (terrain self-shadowing,
  buildings, vegetation), solar-eclipse dimming computed geometrically per frame.
- Click-to-identify sky objects (name, alt/az, RA/Dec, magnitude, phase, rise/set,
  constellation) through the existing inspector.
- MCP tools: `sky_at`, `body_position`, `next_sky_event` (incl. local solar-eclipse
  circumstances), `set_view_time`, `highlight_sky`, `clear_sky_highlights`,
  `solar_irradiance` — all logic in `twin_query.py`/`twin_astro.py`, tested in
  `twin_query_test.py`, documented in `docs/mcp.md`.
- Horizons validation pipeline: `fetch_horizons_reference.py` (online, one-time)
  + `astronomy_validate.py` (offline compare, arcminute thresholds).

**Non-goals (v1, explicitly):**
- Solar-panel siting, annual insolation integration, radiation-on-surface maps
  (future phase; §8 is its substrate).
- Satellites/ISS passes, comets, asteroids, deep-sky objects, light pollution,
  atmospheric refraction in the *rendered* dome (reported in data outputs only).
- Store writes. Ephemeris is a pure function of (site, time), not observed twin
  state — no entities, no journal, no pipeline runs. Document this in
  `docs/mcp.md` "Phase boundaries". The Horizons reference file is a validation
  fixture on disk, not a store-registered input.
- Moonlight shadow maps (only the sun casts shadows in v1; note as v1.1 toggle).

## 2. Architecture overview

```
                    ┌─ public/vendor/astronomy.browser.min.js  (vendored engine, global `Astronomy`)
                    ├─ public/astronomy-data/{stars.json, constellations.json, moon_1k.jpg}
Viewer (browser)    │
  public/astronomy.js  — VEILAstronomy: clock, pane UI, lighting controller,
  │                      applySkyViews/applyViewTime, identify card
  └─ public/sky.js     — VEILSky: separate sky Scene (dome shader, star Points,
                         constellation LineSegments, planet sprites, sun disc,
                         textured moon), sky picking, highlight markers

Python (MCP + validation)
  scripts/twin_astro.py            — engine wrapper (PyPI astronomy-engine),
  │                                  name registries, eclipse/event search,
  │                                  clear-sky irradiance model
  ├─ scripts/twin_query.py         — new astronomy methods, annotations.json
  │                                  channel extension (sky_views, view_time)
  ├─ scripts/mcp_server.py         — thin @mcp.tool() wrappers
  ├─ scripts/fetch_horizons_reference.py  (online, one-time per twin)
  └─ scripts/astronomy_validate.py        (offline compare vs Horizons)

Channel (LLM → viewer): data/annotations.json gains `sky_views` + `view_time`,
polled by public/annotations.js (4 s, edge-triggered), handed to
window.__twin.astronomy. No new server endpoints.
```

## 3. Ephemeris engine & sky assets

### 3.1 Vendored engine

- **`public/vendor/astronomy.browser.min.js`** — the browser build of
  [`astronomy-engine`](https://github.com/cosinekitty/astronomy) (MIT, ~116 KB
  min). Pin the version; add a 3-line header comment (name, version, upstream
  URL, MIT) matching the vendored-file convention (`TransformControls.js:1-3`).
  It attaches the global `Astronomy`. Obtain from the npm tarball
  (`npm pack astronomy-engine` → `astronomy.browser.min.js`) or the GitHub
  source tree; do **not** add a package.json dependency — this repo vendors.
- **Python:** add `astronomy-engine>=2.1` to `requirements.txt` (pure Python,
  no transitive deps; installs into `.venv-mcp` like `mcp`/`pyproj`).
- JS and Python ports share one upstream and one test suite → cross-language
  parity is expected; §10 spot-checks it anyway.

Engine functions used (same names both languages, snake_case in Python):
`MakeTime/Observer`, `Equator(body, time, observer, ofdate, aberration)`,
`Horizon(time, observer, ra, dec, refraction)`, `Rotation_EQJ_HOR(time, observer)`,
`RotateVector`, `SearchRiseSet`, `SearchMoonPhase`/`MoonPhase`, `Illumination`
(magnitude, phase angle), `Libration`, `Seasons`, `SearchGlobalSolarEclipse`,
`SearchLocalSolarEclipse` (per-observer circumstances incl. obscuration),
`SearchLunarEclipse`, `Constellation(ra, dec)`, `AngleBetween`.

### 3.2 Star & constellation catalogs (committed, region-agnostic)

`scripts/fetch_sky_assets.py` — **one-time online** script (like
`fetch_remote_layers.py` in posture). It downloads, converts, and writes the
committed assets; re-running is idempotent. Sources and licenses (record both
in each output file's header/JSON field):

- Stars: `d3-celestial` `data/stars.6.json` (Hipparcos-derived, mag ≤ 6.0,
  5,044 stars in the committed asset, BSD-3-Clause,
  github.com/ofrohn/d3-celestial). Convert to
  **`public/astronomy-data/stars.json`**:
  ```json
  { "version": 1, "source": "...", "license": "BSD-3-Clause",
    "count": N, "stars": [[raDeg, decDeg, mag, bv, hip, "Name-or-\"\""], ...] }
  ```
  RA/Dec are **J2000 degrees**; `bv` is B−V color index; `hip` the Hipparcos id
  (0 if absent); proper names only where the source has them (Polaris, Sirius…).
- Constellations: `d3-celestial` `constellations.lines.json` +
  `constellations.json` → **`public/astronomy-data/constellations.json`**:
  ```json
  { "version": 1, "names": {"Ori": "Orion", ...},
    "lines": {"Ori": [[hip_a, hip_b], ...], ...} }
  ```
  Lines reference HIP ids; the viewer builds a hip→index map at load and drops
  (with a console.warn count) any segment whose star is missing from the mag-6.0
  catalog.
- Moon albedo: NASA SVS **CGI Moon Kit** public-domain color map, using the
  pre-sized 1024×512 equirectangular
  `lroc_color_poles_1k.jpg` → **`public/astronomy-data/moon_1k.jpg`**.

All three outputs are **committed** (~500 KB total) — they are engine assets
like `public/vendor/`, not twin data; every future twin gets a working sky with
zero fetching. Precession/nutation is applied at render time by the engine's
rotation matrix (§6.2), so J2000 storage stays correct for centuries; proper
motion is ignored (arcsec/century for all but a handful of stars — note in the
file header).

## 4. Site, coordinates, time — conventions

- **Observer** = the twin's georef origin. Viewer: `data/georef.json`
  `origin_wgs84` (`{lon, lat}`) if present, else derive via
  `VEILGeoref.projectedToGeographic(origin_utm[0], origin_utm[1])`
  (`georef.js:72-75`); height = `grid_min_elevation_m` (georef.json). Python:
  `twin_query.TwinQuery` already builds a `Georef` from store meta
  (`twin_query.py:598-615`) — reuse it, same fallback. Over a ~2 km parcel the
  worst-case lunar parallax difference across the parcel is < 0.4 arcsec;
  one observer per twin is correct. **No pack involvement anywhere.**
- **Axis mapping** (the one place to get right; verify with the Polaris check in
  §10). Scene: `x = east`, `y = up`, `z = −north` (`terrain.js:39-43`,
  `georef.js:6-9`). A body at azimuth `A` (deg, clockwise from north) and
  altitude `h` has scene-space unit direction:
  ```
  d = ( cos(h)·sin(A),  sin(h),  −cos(h)·cos(A) )
  ```
  astronomy-engine's HOR vector system is (x=north, y=west, z=zenith), so a
  HOR vector `v` maps to scene as `( −v.y, v.z, −v.x )`.
- **Refraction policy:** all *rendered* geometry (dome matrix, discs, sprites)
  uses **airless** (unrefracted) coordinates so the whole sky shares one rigid
  rotation. All *reported* numbers (identify card, MCP outputs) include both
  `altitude_deg` (airless) and `altitude_refracted_deg` (engine `'normal'`
  refraction). Rise/set searches use the engine defaults (refracted, standard
  horizon) — those are the times people mean.
- **Time policy:** internal representation is **UTC milliseconds** everywhere.
  MCP inputs/outputs are ISO-8601 UTC (`...Z`); tools also echo
  `unix_ms`. The viewer displays browser-local time (`datetime-local` input,
  `toLocaleString`). There is **no timezone database** and no guessing the
  twin's zone — GAIA converts in conversation if asked. Document this in
  `docs/mcp.md`.
- **Clamps:** any time accepted from UI or MCP is clamped to
  `1600-01-01T00:00Z … 2500-01-01T00:00Z`; play rate to `|rate| ≤ 604800`
  (one week per second). Clamp in *both* `twin_query.py` (authoritative,
  mirrors the `_scenario_argv` clamp pattern at `twin_query.py:2979-3003`) and
  the viewer UI.

## 5. Viewer — clock & the Astronomy pane

### 5.1 Files & registration (copy the Simulation pattern exactly)

- **`public/astronomy.js`** — IIFE `(function attachAstronomy(global){ … })(window)`
  exporting `global.VEILAstronomy = { create, _test }` (mirror
  `simulation.js:19,554-564`). `create(api)` grabs its DOM by id and returns
  `null` if `#astronomy-panel` is missing (mirror `simulation.js:163-185`).
- **`public/sky.js`** — IIFE exporting `global.VEILSky = { create }`; pure
  rendering, no DOM except the canvas it already shares.
- **`public/index.html`:**
  - Script order (order matters; `index.html:478-505`):
    `astronomy.browser.min.js` after `proj4.js` in the vendor block;
    `Sky.js` (vendored shader, §7.1) with the other vendor examples;
    `/sky.js` and `/astronomy.js` in the feature-module block after
    `/simulation.js` and **before `/app.js`**.
  - Rail button after the Simulation button (`index.html:35-38` as template),
    astrolabe icon, stroke-styled like the others:
    ```html
    <button class="rail-btn" data-mode="astronomy" title="Astronomy — sky, sun & time">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="13" r="8"/>
        <path d="M12 5v16M4.6 15.5h14.8M7 19.2A8 8 0 0 0 17 19.2"/>
        <circle cx="12" cy="3" r="1.4"/>
      </svg>
      <span>Astronomy</span>
    </button>
    ```
  - Pane `<section class="pane" data-pane="astronomy" id="astronomy-panel">`
    in the flyout body, containing (ids referenced by astronomy.js):
    `#astro-master` (Physical sky & lighting checkbox), `#astro-toggles`
    (layer toggle host), `#astro-clock` (datetime-local input `#astro-time`,
    buttons `#astro-now`, `#astro-play`, select `#astro-rate` with options
    1× / 60× / 600× / 3600× / 86400× and negative twins), jump row
    `#astro-jumps` (Sunset, Sunrise, Midday, Night), and a small status
    line `#astro-status` (current sim time, sun alt/az, moon phase %).
- **`public/shell.js:19-25`:** add `astronomy: 'Astronomy'` to `TITLES`.
- **`public/app.js`** (~`:225`, next to the other feature mounts): build the
  api object and mount:
  ```js
  window.__twin.astronomy = window.VEILAstronomy?.create({
    viewer, state,
    georef: /* {lat, lon, heightM} resolved from scene payload/georef.json */,
  });
  ```

### 5.2 The clock (first shared clock in the viewer — own it cleanly)

State inside astronomy.js:
`clock = { mode: 'realtime'|'manual', epochMs, anchorPerfMs, rate, playing }`.
- Realtime: `now() = Date.now()` every frame (satisfies "realtime = browser
  clock"; no drift bookkeeping).
- Manual: `now() = epochMs + (performance.now() − anchorPerfMs) · rate · playing`.
- `Now` button → realtime mode. Editing the datetime, pressing a jump button,
  or an MCP `view_time` directive → manual mode.
- Subscribers: `clock.onTick(fn)` called once per rendered frame with
  `(utcMs, dtSimMs)`. sky.js and the lighting controller subscribe; future
  consumers (solar siting, ET) reuse this instead of growing their own.
- Recompute throttles (engine calls are sub-ms but don't waste them):
  sun + moon every frame; star-dome rotation matrix when sim time moved
  > 1 s; planets when moved > 60 s; magnitudes/rise-set only on demand.
- The clock **keeps running when the pane is closed** — it is scene state, not
  pane animation (contrast the wildfire pane's `.active` observer,
  `shell.js:55-59`; do not copy that gating for the clock).
- **Gotcha (project-documented):** number/date inputs use `step="any"` where
  applicable — a preset value that violates `step` silently blocks form submit
  (see CLAUDE.md, Simulation window note). Don't wrap the clock controls in a
  `<form>` at all.

### 5.3 Layer toggles

Render `.toggle-row` rows (markup per `app.js:1044-1053`) into `#astro-toggles`
for: **Sun, Moon, Planets, Stars, Constellations**. Constellations is indented
and disabled unless Stars is checked. These are **scene-graph visibilities on
sky.js objects** — not drape layers, not part of `state.atlas`/`allLayers()`;
they route `change` → `sky.setLayerVisible(kind, on)` and live in
`localStorage` (`veil.astro.layers`) so preferences persist. Defaults: all on.
Toggles only control *rendering*; the ephemeris and (if enabled) lighting run
regardless — hiding the Sun disc does not turn off the sun light.

Screenshot harnesses scan `#layer-toggles, #atlas-toggles, #survey-toggles,
#astro-toggles` so plates can isolate sky layers by label. They also pin the
astronomy clock after boot to `ASTRO_TIME` or `2026-06-21T16:00:00Z` so plates
are deterministic.

## 6. Viewer — sky rendering (`public/sky.js`)

### 6.1 The sky pass

A separate `THREE.Scene` (`skyScene`) rendered **before** the main scene each
frame with a `skyCamera` that copies only the main camera's `quaternion`,
`fov`, `aspect`, `near/far` — never position. Modify the render call at
`scene.js:1445-1472` (single, minimal hook):

```js
// scene.js, in animate(), replacing the lone renderer.render(...)
if (this.skyPass) {
  this.renderer.autoClear = false;
  this.renderer.clear();
  this.skyPass.render(this.renderer, this.camera);   // sky first, depth off
  this.renderer.render(this.scene, this.camera);      // terrain occludes sky
  this.renderer.autoClear = true;
} else {
  this.renderer.render(this.scene, this.camera);
}
```
`viewer.setSkyPass(passOrNull)` sets it. All skyScene materials use
`depthWrite: false, depthTest: false` and explicit `renderOrder`
(dome 0 → stars 1 → constellation lines 2 → planets 3 → moon 4 → sun 5 →
highlight markers 6). Terrain, drawn after with depth, silhouettes the horizon
correctly — the sun sets behind the actual hills.

The sky pass is installed whenever the astronomy feature is mounted. Sky layers
render whenever their layer toggles are on, independent of the master switch.
At night with the master off the backdrop goes dark and stars show; the master
switch gates physical lighting, tone mapping, and shadows only. When the sky
pass is active, `scene.background` must be `null` except when POV deliberately
overrides it. **POV interplay:** `pov.js` sets/restores its own flat sky color
+ FogExp2 when physical lighting is off, using
`!window.__twin?.astronomy?.photometricOn?.()` as the gate. With photometric
mode on, POV lets the astronomy sky/lighting carry the scene. (Free win: the
POV water shader already reads `viewer.sunLight` direction, so sunset specular
tracks the real sun with no changes.)

### 6.2 Star field & constellations

- One `THREE.Points` with `BufferGeometry` (unit-sphere J2000 positions from
  `stars.json`, radius 1) and a custom `ShaderMaterial`: per-vertex `aMag`,
  `aColor`; additive blending, no depth. `gl_PointSize =
  clamp(k1 − k2·mag, 1.0, 9.0) · pixelRatio` (start k1=7.5, k2=1.1; tune).
  Color from B−V via a 10-entry LUT (B−V −0.3→+2.0 mapped bluish-white →
  orange-red; any standard published approximation is fine).
- **Orientation:** per throttled tick, `rot = Astronomy.Rotation_EQJ_HOR(time,
  observer)` (includes precession + nutation + sidereal rotation), compose with
  the fixed HOR→scene axis swap (§4) into one `Matrix4`; set it on the Points
  object with `matrixAutoUpdate = false`. One matrix, 5,044 stars, zero per-star
  work.
- **Daylight fade:** material uniform `uFade` =
  `smoothstep(sunAlt, 0°, −12°)` (1 at nautical dark, 0 by sunrise), applied to
  star alpha. Same uniform feeds constellation lines. This is perceptual, not
  radiometric — fine.
- Constellations: one `THREE.LineSegments` sharing the same rotation matrix,
  built from `constellations.json` pairs; subtle color (`0x5c7ea0`), opacity
  ~0.35 × `uFade`. Visible only when Stars+Constellations toggles are on —
  **except** an agent highlight (§9) force-shows the highlighted one at full
  opacity even in daylight.

### 6.3 Sun, moon, planets

All positioned per tick at `direction · R_DOME` (`R_DOME = 100`; skyScene has
its own scale, depth is off so the number only matters for parallax-free look).

- **Sun:** a `CircleGeometry` disc, angular diameter from
  `Astronomy.Illumination`/geometry (~0.533°, so radius
  `R_DOME · tan(0.2665°)`), `MeshBasicMaterial({ toneMapped: false })`
  white-yellow, plus a soft radial-gradient glow **mesh** (billboarded plane
  via `lookAt`, not a `THREE.Sprite` — `cam_shot.js:58` force-hides all
  Sprites in plates).
- **Moon:** `SphereGeometry` textured with `moon_1k.jpg`,
  `MeshLambertMaterial`, lit by a dedicated `DirectionalLight` **inside
  skyScene** aimed along the true sun direction → correct phase for free.
  Apply libration (engine `Libration`) so the familiar face points at the
  observer; v1 uses a fixed roll and does not yet apply a position-angle
  refinement. Angular size from true distance (~0.49–0.56°). During a lunar
  eclipse (§7.4) tint toward `0x883322`.
- **Planets:** Mercury, Venus, Mars, Jupiter, Saturn (+ Uranus, Neptune, drawn
  only when their computed magnitude ≤ 6.5): billboarded quad markers sized by
  the star-magnitude curve with a floor (planets never smaller than a bright
  star), colored per convention (Mars `0xff8866`, Jupiter `0xffe8c8`, …).
  Positions via `Equator(body, t, obs, true, true)` → `Horizon` (airless).

### 6.4 Sky picking (no persistent labels — click to identify)

Hook the existing pick flow in `app.js` `setupPicking` (~`:1473`): when the
terrain raycast **misses** (user clicked sky pixels) and the sky pass is
active, call `sky.pickAt(ndc)`. Implementation: build the click ray in
sky-camera space; a body/star is hit if the angular distance between the ray
and its direction is < max(1°, its angular radius). Nearest wins; bodies win
ties over stars. Return `{kind, name, ...}`; astronomy.js formats an identify
card (name; alt/az; RA/Dec; magnitude; for moon: phase % + next full/new; for
sun: today's rise/set; constellation via
`Astronomy.Constellation`) and shows it through the same inspector container
the identify pipeline uses (`notifyInspect` pattern, `app.js:1284-1296,1470`,
`veil:inspect` → `shell.js:96-102`).

### 6.5 Highlights (GAIA channel, §9)

`sky.highlight(target)` draws an orange (`0xff8c1a`, the annotations color,
`annotations.js:12`) ring mesh around a body/star (radius ~1.5× its marker) or
force-shows a constellation's lines in orange, plus one canvas-texture label
(same style as `annotations.js addLabel`, `:144-167`, but positioned on the
dome, not terrain-hugged). `sky.clearHighlights()` removes all. Highlights are
whatever the last `sky_views` directive says — they are not persisted locally.

## 7. Viewer — physical lighting mode

Master switch `#astro-master`, default **off**. Off = classic lighting
(ambient 0.85 + directional 1.15, no shadows, no tone mapping) while the
astronomy sky layers may still render when toggled on. The switch calls
`viewer.setPhotometricMode(on)` which snapshots and restores every renderer,
light, and material default it touches, so toggling off restores classic
lighting faithfully.

### 7.1 Renderer & sky shader

- On: `renderer.toneMapping = THREE.ACESFilmicToneMapping`;
  `renderer.toneMappingExposure` driven per tick (below); shadow map on
  (`renderer.shadowMap.enabled = true`, `type = THREE.PCFSoftShadowMap`).
- **Sky dome:** vendor three.js r160's `examples/jsm/objects/Sky.js`
  hand-converted to a plain global script `public/vendor/Sky.js` (destructure
  `global.THREE`, attach `global.THREE.Sky` — exactly the
  `TransformControls.js` conversion pattern, r160 to match core). Parameters:
  turbidity 3.2, rayleigh 1.2, mieCoefficient 0.004, mieDirectionalG 0.8;
  `sunPosition` uniform = true sun direction each tick. Render it in
  skyScene *behind* the stars. The Preetham-family model is
  physically-plausible, not radiometric — acceptable per §0's framing; a
  Hosek-Wilkie upgrade slots in behind the same uniform later.
- **Exposure curve** (perceptual autoexposure; parameters in one const block,
  expect tuning): with sun altitude `h` in degrees,
  `exposure = lerp(EV_night=2.6, EV_day=0.55, smoothstep(−12, +8, h))`,
  further × `1/(0.15 + 0.85·(1 − obscuration)^3)` capped ×3 during solar
  eclipses so totality actually goes dark.

### 7.2 Lights

Replace (while on) the boot lights with:
- `sunLight` — reuse the existing `viewer.sunLight` (`scene.js:280`).
  Direction: position = `aoiCenter + d_sun · 2000`, target = `aoiCenter`
  (AOI center from the terrain grid extents). Intensity:
  `3.2 · clamp(sin(h), 0, 1)^0.6 · (1 − obscuration)`; color lerped
  white → `0xffd9b0` → `0xff9955` as `h → 0` (airmass reddening, perceptual).
- `moonLight` — new `DirectionalLight`, same aiming scheme off the moon
  direction. Intensity `0.12 · illuminatedFraction · clamp(sin(h_moon),0,1)`,
  color `0xbfcfff`. `castShadow = false` (v1).
- `hemiLight` — `HemisphereLight(skyZenithColor, groundColor 0x3a3428)`,
  intensity `lerp(0.02, 0.55, smoothstep(−12, 10, h))` — this is the twilight
  ambient and the nighttime floor. Boot `AmbientLight` set to 0 while on.

### 7.3 Shadows

- `sunLight.castShadow = true`; orthographic shadow camera fitted once to the
  terrain grid bbox (+20 % margin, near/far spanning min/max elevation ± 200 m);
  `mapSize 4096` (const, fallback 2048); `bias −0.0004`,
  `normalBias 1.5` (meters-scale scene — expect to tune against acne/peter-panning).
- Flags: terrain mesh `castShadow = receiveShadow = true` (hills shade
  valleys — the load-bearing feature for future solar siting);
  buildings3d meshes both true (set at load, `buildings3d.js` mesh creation);
  vegetation — the instanced meshes currently hard-set `false`
  (`vegetation.js:1146-1149, 1179-1180, 1203-1204`): add
  `VEILVegetation…setShadows(on)` that flips flags on all live instanced
  meshes and applies to future rebuilds; astronomy calls it from
  `setPhotometricMode`. Ortho drape overlay (`scene.js:1235-1266`) is
  `MeshBasicMaterial` (unlit — it would glow at night): while photometric mode
  is on, build it as `MeshStandardMaterial({map, roughness:1, metalness:0})`
  with `receiveShadow = true` instead; restore basic on off.
- Perf: `renderer.shadowMap.autoUpdate = false`;
  `shadowMap.needsUpdate = true` whenever the sun moved > 0.1° or the scene
  changed (vegetation chunk sync, building placement) — at realtime rate
  that's one shadow render every ~24 s instead of 60/s.

### 7.4 Eclipses (geometric, per-frame — no event search in the render path)

Per tick with sun above horizon: angular separation
`θ = AngleBetween(sunVec, moonVec)`, angular radii `r_s`, `r_m` from true
distances. If `θ < r_s + r_m`, obscuration = standard two-circle lens
intersection area ÷ sun disc area (clamp 0–1; if `θ ≤ |r_s − r_m|` and
`r_m ≥ r_s` → 1.0). Feed obscuration into sun intensity + exposure (§7.1/7.2).
Ramp in the corona over obscuration 0.94→0.995: radial-gradient canvas texture
on a billboarded plane behind the moon disc, and let the star fade uniform use
the eclipse-darkened effective sun altitude so stars appear during totality.
Lunar eclipses: per tick with moon up, if the moon's angular distance from the
anti-solar point < umbra radius (compute umbral radius from geometry, or the
cheap standard approximation), tint the moon material toward `0x883322`
proportionally. These are the *visual* paths; the *authoritative* event times
come from the engine searches via MCP/jump buttons.

## 8. Radiometric substrate (`scripts/twin_astro.py`)

The honest split: shaders render, Python measures. `twin_astro.py` (pure
module, no store access, mirrors `twin_hydrology.py` posture) implements:

- `observer()` from georef (shared helper with twin_query).
- Position/phase/event wrappers over the PyPI engine (used by §9's tools).
- **Clear-sky irradiance** — Bird & Hulstrom clear-sky model (simple, ~40
  lines, well-documented): inputs sun zenith, site elevation, default
  atmospheric parameters (const block: TL/turbidity ≈ 3, precipitable water
  1.5 cm, albedo 0.2); outputs `{ghi_wm2, dni_wm2, dhi_wm2, sun_altitude_deg,
  airmass}`. Zero when sun below horizon. **v1 reports clear-sky at a
  timestamp only** — no cloud model, no terrain shading, no annual
  integration; say so in the tool description and provenance. This function +
  the shadow-capable terrain (§7.3) are the two hooks the future solar-siting
  phase builds on. (`et_scenario.py:63-68`'s day-length helper stays as is;
  a later cleanup can re-derive it from twin_astro.)
- Name registries: bodies (`sun, moon, mercury … neptune`), the 88 IAU
  constellations (name + 3-letter abbr from `constellations.json` — read the
  committed viewer asset, one source of truth), named stars from `stars.json`.
  Case-insensitive resolution; unknown name → `TwinQueryError` whose payload
  lists valid categories + close matches (house error convention,
  `twin_query.py:101-108`).

## 9. MCP & GAIA integration

### 9.1 `annotations.json` schema extension (version stays 1)

Two new top-level keys beside `annotations`/`layer_views`
(`_save_view_doc`, `twin_query.py:552-558`):
- `sky_views`: list of `{target_type: "body"|"star"|"constellation",
  name, label?, created_at}` — same upsert-by-name semantics as
  `_upsert_layer_view` (`:3738-3743`).
- `view_time`: `null` or `{iso, rate, created_at}` (`iso` UTC; `rate` sim
  seconds per real second, clamped per §4; `rate 0` = paused).

**Refactor `_load_view_doc`/`_save_view_doc` to carry the whole doc** (read
doc, mutate keys, write doc) so every existing writer preserves the new keys
and vice versa — the current tuple-of-two-lists shape would silently drop
them. Update `server.js clearAnnotations` (`:3144-3156`) to write
`sky_views: [], view_time: null` too (viewer opens clean, realtime — matches
`clearOnOpen`, `annotations.js:261-267`).

### 9.2 Viewer application (edge-triggered, manual wins)

`annotations.js refreshImpl` (`:221-245`): alongside the existing
`applyLayerViews` call (non-initial polls only), add
`global.__twin?.astronomy?.applySkyDirectives?.(skyViews, viewTime)`.
Semantics identical to layer views: applied only when the file's content
signature changes; the user touching the scrubber or toggles afterward wins
until the next directive change. `chat.js:457`'s post-reply
`annotations.refresh()` makes chat-driven changes land immediately.
`applySkyDirectives`: `view_time` → set clock (manual mode, given rate);
`sky_views` → `sky.clearHighlights()` then highlight each, force-showing the
minimum needed layer (e.g. planets layer off + highlight Mars → show Mars
anyway, agent-override style; a manual Planets toggle reclaims, mirroring
`state.agentLayers`, `app.js:1084-1090, 1142-1170`).

### 9.3 Tools (wrappers in `mcp_server.py`, logic in `twin_query.py`)

Follow the house pattern exactly: `@mcp.tool()` docstring-described wrapper →
`_run(_query().method, ...)` (`mcp_server.py:98-103, 567-577`); every output
carries provenance `{"source": "astronomy-engine <ver>", "validated_against":
"JPL Horizons (data/astronomy/horizons-reference.json)" }` and echoes times as
ISO UTC + `unix_ms`. New methods on `TwinQuery` (delegate math to
`twin_astro`):

| Tool | Signature (defaults) | Returns |
|---|---|---|
| `sky_at` | `(time=None)` | Sun & moon alt/az (+refracted), moon phase % & name, is_night/twilight kind, visible planets w/ alt/az/mag, today's sunrise/sunset/moonrise/moonset at the site. |
| `body_position` | `(body, time=None)` | alt/az, RA/Dec (of-date + J2000), distance, magnitude, angular size, phase (sun/moon/inner planets), constellation, next rise/set/culmination. Accepts planets, sun, moon, named stars. |
| `next_sky_event` | `(kind, from_time=None, count=1, max_span_deg=50, horizon_years=100, demonstrate=False)` | kinds: `solar_eclipse` (local circumstances at the site via `SearchLocalSolarEclipse`: kind partial/annular/total **as seen here**, obscuration, partial/total begin-peak-end times, plus the matching global eclipse kind), `total_solar_eclipse` (loops `NextLocalSolarEclipse` until the site is inside the path of totality, bounded by `horizon_years`), `lunar_eclipse` (phase begin/end times + `visible_from_site`: moon apparently up during the deepest phase), `total_lunar_eclipse`, `blood_moon` (total lunar eclipse the moon is up for at this site), `planetary_alignment` (five naked-eye planets within `max_span_deg` of geocentric ecliptic longitude — adaptive daily scan; window begin/peak/end, per-planet solar elongation + `observable` flags), `supermoon` (full moon ≤ 360,000 km), `full_moon`, `new_moon`, `sunrise`, `sunset`, `moonrise`, `moonset`, `solstice`, `equinox`, `golden_hour` (sun alt 6°→−4° window). Horizon-bounded kinds return `events: []` plus a `note` when nothing qualifies. `demonstrate=True` also writes `view_time` (eclipses: ~10 min before the event at rate 60) and highlights the bodies involved — the one-call "answer AND show it" path. |
| `set_view_time` | `(time, rate=1.0)` | Writes `view_time`; returns the applied (clamped) values. Doc guidance: for an eclipse, set time ≈ 10 min before peak with rate 60. `time="now"` → clears to realtime (`view_time: null`… write `{iso: null-follow}` as `null` + bump `updated_at` so the edge triggers). |
| `highlight_sky` | `(name, label=None)` | Resolves against registries (body/star/constellation auto-detected), upserts into `sky_views`. Returns resolved target + current alt/az so GAIA can say where to look. |
| `clear_sky_highlights` | `()` | Empties `sky_views`. |
| `solar_irradiance` | `(time=None)` | Bird clear-sky GHI/DNI/DHI + sun geometry (§8), with the honest-framing caveat in the payload. |

Update the `FastMCP` instructions string (`mcp_server.py:47-86`) with one
sentence on the sky capability; docs: new rows in the `## Tools` table and a
new `## Astronomy` section in `docs/mcp.md` (after `## Hydrology simulation`),
plus `## Phase boundaries` note that astronomy adds **no** store writers —
only the annotations-channel writers (`sky_views`, `view_time`).

### 9.4 Tests (`scripts/twin_query_test.py`)

New `print("== astronomy ==")` section, house style (`check`/`expect_error`,
direct `json.load(open(ANN))` assertions — `:33-51, :860-925`):
- `body_position("polaris")` altitude ≈ site latitude ± 1° (the classic
  invariant; site lat from georef, don't hardcode).
- `next_sky_event("solar_eclipse", from_time="2024-04-01T00:00Z")` finds the
  2024-04-08 eclipse with local obscuration > 0.9 at this site (southern
  Adirondacks sat just off the totality path).
- `sky_at` at a fixed winter-noon instant: sun altitude within ±1.5° of the
  Horizons reference row when `data/astronomy/horizons-reference.json` exists
  (skip-with-note when absent).
- `set_view_time` + `highlight_sky("orion")` → assert `view_time.iso`, one
  `sky_views` entry, `annotations`/`layer_views` untouched; `clear_sky_highlights`
  empties only `sky_views`. Clamp tests: year 9999 → clamped; `rate=1e9` →
  clamped; unknown body name → `{"error": ...}` with suggestions.

## 10. Validation against Horizons

- **`scripts/fetch_horizons_reference.py`** (online, one-time per twin, like
  the other `fetch_*` snapshots): site from georef; query
  `https://ssd.jpl.nasa.gov/api/horizons.api` with `format=json`,
  `EPHEM_TYPE=OBSERVER`, `CENTER='coord@399'`, `COORD_TYPE=GEODETIC`,
  `SITE_COORD='lon,lat,elev_km'`, `QUANTITIES='2,4'` (apparent RA/Dec +
  az/el), `APPARENT='AIRLESS'` (matches our airless policy) for bodies
  Sun `10`, Moon `301`, planets `199,299,499,599,699,799,899`, at 48
  timestamps spanning ±5 years from the run date (TLIST). Write
  `data/astronomy/horizons-reference.json` (per-twin fixture, gitignored with
  the rest of `data/`; **not** store-registered).
- **`scripts/astronomy_validate.py`** (offline): for every reference row,
  compute the same quantity via `twin_astro.py` and report per-body max
  angular error. **Pass thresholds: ≤ 1.0 arcmin sun/planets, ≤ 2.0 arcmin
  moon** (topocentric parallax is elevation-sensitive). Nonzero exit on
  failure, printed table always. Run it in CI-of-one fashion: by hand after
  vendoring and after any engine version bump.
- **JS↔Python parity:** `scripts/astronomy_parity_test.js` — plain Node,
  `require`s the vendored UMD file, compares sun/moon/Mars alt-az at 10
  timestamps against live `scripts/twin_astro.py` results; agreement expected
  < 1 arcsec (same upstream). Cheap insurance that the vendored JS and the pip
  Python are the same engine version.
- **Visual acceptance** (manual checklist, screenshot harness where possible):
  Polaris due north at altitude ≈ 43.3° (this twin); sun rises visually
  between the correct ridgelines vs NOAA table for a chosen date; scrub to
  2024-04-08 18:25 UTC → sky dims hard, corona-less deep partial at this
  site; with the master off, night shows the dark astronomy backdrop and stars;
  full-moon photometric mode shows blue-grey shadowed scene with stars; planets lie
  on the ecliptic band.

## 11. Generalization to other twins

The whole feature reads exactly three site facts — lat, lon, height — from
`data/georef.json` / store meta, both already twin-agnostic. Catalogs and the
engine are committed engine assets. A brand-new twin made with
`ingest_dem.py` gets the full sky with **zero** additional fetching; the only
optional per-twin step is `fetch_horizons_reference.py` for validation.
Southern hemisphere and far-north sites work through the same math (the axis
mapping in §4 has no hemisphere assumptions; add one unit check in
`twin_query_test.py` calling twin_astro with a mocked observer at lat −35° and
asserting the Southern Cross's declination is below the horizon at lat +43°).
Nothing in `packs/` is touched.

## 12. Implementation plan (phased; each phase lands green on its own)

**Phase 0 — engine, assets, Python substrate (no viewer changes)**
1. Vendor `astronomy.browser.min.js` (+ header); add `astronomy-engine>=2.1`
   to `requirements.txt`; pip-install into `.venv-mcp`.
2. `scripts/fetch_sky_assets.py` → commit `public/astronomy-data/*`.
3. `scripts/twin_astro.py` (§8) with its name registries.
4. `scripts/fetch_horizons_reference.py` + `scripts/astronomy_validate.py`
   (§10); run both; thresholds green.
   **Accept:** validate passes; `python3 -c "import twin_astro"` clean.

**Phase 1 — clock, pane, sky rendering (visual, lighting still off)**
5. index.html: vendor tags, rail button, pane markup; `shell.js` TITLES.
6. `public/sky.js` (§6: sky pass, dome placeholder color, stars,
   constellations, sun/moon/planets, picking, highlights API).
7. `public/astronomy.js` (§5: clock, toggles, jumps via in-browser engine
   searches, status line); mount in `app.js`; sky-pick fallback in
   `setupPicking`; identify card.
8. Add `#astro-toggles` to both screenshot harness selector lists.
   **Accept:** scrub a night — stars rotate about Polaris at alt ≈ lat; moon
   phase matches reality for "Now"; click Jupiter → card; harness plate with
   only "Stars" toggled works; with the master off, classic lighting remains
   while sky layers still render.

**Phase 2 — photometric mode**
9. `viewer.setPhotometricMode` in scene.js (renderer, lights, exposure,
   snapshot/restore); vendored `Sky.js`; §7.2 lights; POV gating.
10. Shadows (§7.3): shadow camera fit, terrain/building flags,
    `vegetation setShadows`, ortho-overlay material swap, autoUpdate throttle.
11. Eclipse dimming + corona + lunar tint (§7.4).
    **Accept:** toggling master off restores classic lighting/render settings; sunset
    golden hour reads correctly; tree/building shadows track the sun through a
    scrubbed day at 60 fps-ish; 2024-04-08 scrub darkens; full-moon night lit.

**Phase 3 — channel + MCP + docs**
12. `twin_query.py`: doc-carrying `_load/_save_view_doc` refactor, clamps,
    new methods; `server.js clearAnnotations` keys; `annotations.js` handoff;
    `applySkyDirectives` in astronomy.js.
13. `mcp_server.py` tools + instructions; `docs/mcp.md` sections;
    `twin_query_test.py` `== astronomy ==`; CLAUDE.md short section.
    **Accept:** full test file green; in-chat "when is the next total eclipse
    here?" answers with dates AND (on request) scrubs the viewer there with
    the sun highlighted; "Clear drawings" + reload returns to realtime/clean.

**Deferred (v1.1+, tracked here, not built now):** moonlight shadows toggle;
Hosek-Wilkie dome; refraction-bent rendering near horizon; per-point
`solar_irradiance` with terrain-shading (needs a horizon-mask precompute — the
solar-siting phase); satellites; `et_scenario.py` day-length re-derivation.

## 13. Gotchas for the implementer (read before coding)

1. **No modules, no build.** Every new file is a plain script attaching a
   `window.VEIL*` global; script order in `index.html` is the dependency
   graph. Vendored examples are hand-converted to globals
   (`TransformControls.js:1-3` is the template). three.js is **r160 UMD**.
2. `cam_shot.js:58` hides all `THREE.Sprite`s — use billboarded meshes for
   sun glow/corona/highlight labels, never Sprites.
3. OrbitControls `maxPolarAngle = π·0.49` (`camera.js:13`) — the orbit view
   mostly looks *down*; the sky shows near the horizon and shines in POV
   mode. Don't fight the controls in v1; the lighting/shadows are the payoff
   in orbit view.
4. The annotations doc refactor (§9.1) touches **every** existing writer path
   (`draw_*`, `clear_drawings`, `set_layer_visibility`, `filter_layer`,
   `reset_layer_views`, `server.js clearAnnotations`) — run the whole
   `twin_query_test.py` drawings/layer-views sections after, not just the new one.
5. `_save_view_doc` conventions: `.tmp` + `os.replace`, `indent=1`,
   `_utc_now()` format `"%Y-%m-%dT%H:%M:%SZ"` — keep byte-compatible so the
   FNV signature edge-trigger (`annotations.js:14-21`) behaves.
6. Store discipline: astronomy writes **nothing** to the store/journal. If
   you find yourself importing `twin_store` from `twin_astro.py`, stop.
7. Clamps live in Python (`twin_query`) as the authority; the viewer clamps
   are UX mirrors. Same pattern as `_scenario_argv` vs `handleSimulate`.
8. Timestamps: UTC everywhere internally; `confidence` (0–1) is never used
   for GPS/angle accuracy — irrelevant here but a house rule; don't add a
   `confidence` to astronomy payloads, use explicit arcmin error fields if
   needed.
9. `setPhotometricMode(false)` must restore *exact* boot state — snapshot
   `toneMapping`, `toneMappingExposure`, `shadowMap.enabled`, background,
   light intensities/colors, and the ortho-overlay material. The existing
   screenshot suite is the regression check.
10. The engine's `Horizon()` wants RA in **sidereal hours**, Dec in degrees
    (as returned by `Equator`) — don't convert twice. Verify the §4 axis
    mapping with Polaris before building anything on top of it.

## 14. Future work this spec deliberately enables

- **Solar siting / radiation:** terrain casts shadows (§7.3) + clear-sky
  irradiance (§8) + the shared clock (§5.2) are the three primitives; the
  future phase adds a horizon-mask/insolation integrator over the store's
  terrain + vegetation and a scenario-style pipeline run (that phase *will*
  write the store, unlike this one).
- **Planting planner:** same sun-hours substrate joined with the hydrology
  wetness layers and vegetation store queries.
- **Live weather → sky:** cloud cover attenuating both the render and the
  irradiance numbers (Daymet/forcing pipeline already exists for hydrology).
