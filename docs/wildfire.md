# Wildfire simulation — architecture & implementation spec

> Status: **built and operational** (2026-07-06): Tier-1 fuels, Tier-2 ignition
> scenarios, viewer layers, server route, MCP tools, hydrology-fire coupling, and
> regression checks are implemented. This is the canonical spec the implementation
> follows; pin any interface change here, never edit code silently against it. It is
> the fire-side companion to the hydrology stack and mirrors it part-for-part.
> Modeling choices are physics-based and were validated by a
> 16-agent SOTA research + adversarial-feasibility workflow, and the fuel-moisture
> scenario model by a GPT-5.5 (xhigh) study that cross-checked the CFFDRS R
> source, the NFDRS4 C++ source, and BehavePlus-derived implementations. See
> **Provenance** at the end.

## 0. What this is

A wildfire-risk simulation with a **user-chosen ignition source/location**, built
**alongside hydrology** as a second resident of the viewer's collapsible Simulation
surface. It is a **structural clone of the hydrology stack** — same pure-numpy
engine posture, same Tier-1 (static) / Tier-2 (event scenario) split, same draped
raster layer format, same `POST /api/simulate`-style endpoint, same store pipeline
run + journal, same MCP tool shape, same synthesized "…at this spot" identify card.

The one substantive addition over hydrology is a **weather + fuel-moisture scenario
model**: the user drives fire behavior with a coherent *weather narrative* (date,
weather class, drought, wind), and the engine **derives every moisture input** —
including **crown foliar moisture** — from it. No raw moisture sliders.

Honest framing (carried verbatim from hydrology): **geometry is reliable, magnitude
is ±class.** Where fire concentrates, which flank runs uphill/downwind, and relative
arrival order are trustworthy; absolute rate of spread, arrival time, and flame
length are a factor-of-2 estimate under one weather guess.

### The hydrology → fire mirror

| Layer | Hydrology (exists) | Wildfire (new) |
|---|---|---|
| Engine (pure numpy) | `scripts/twin_hydrology.py` | `scripts/twin_fire.py` |
| Tier-1 exporter | `scripts/analyze_hydrology.py` | `scripts/analyze_fuels.py` |
| Tier-2 scenario CLI | `scripts/hydro_scenario.py` | `scripts/fire_scenario.py` |
| Server endpoint | `POST /api/simulate` (`handleSimulate`) | `POST /api/fire-simulate` (`handleFireScenario`) |
| MCP tools | `hydrology_at` / `hydrology_summary` / `run_scenario` | `fire_at` / `fire_summary` / `run_fire_scenario` |
| Viewer module | `public/simulation.js` | `public/wildfire.js` |
| Layer catalog | `data/hydrology/simulation-layers.json` | `data/fire/fire-layers.json` |
| Result state | `data/hydrology/last-scenario.json` | `data/fire/last-fire-scenario.json` |
| Summary | `data/hydrology/summary.json` | `data/fire/summary.json` |
| Store | `hydro_<id>` layers + run | `fire_<id>` layers + run |
| npm scripts | `analyze-hydrology`, `hydro-scenario` | `analyze-fuels`, `fire-scenario` |

Groups: Tier-1 fire layers carry `group:"fire"`; scenario layers `group:"fire_scenario"`.
The catalog-merge rule is "replace only my own group" so the tiers never clobber
each other and hydrology's `simulation-layers.json` is never touched.

## 1. Modeling approach

Tiered exactly like hydrology's static-Tier-1 / event-Tier-2 split.

- **Tier-1 (static fire environment, weather-independent):** fuelscape rollup;
  no-wind/no-slope **base rate-of-spread**; slope-driven **spread hazard**;
  **crown-fire potential** (torching/crowning index under a reference weather).
  "Where can fire carry and torch on this ground."
- **Tier-2 (ignition scenario):** from a picked ignition + a weather/moisture
  scenario: **fire arrival-time** raster, **flame length**, **fireline intensity**,
  **crown-fire class** (surface / passive / active), optional burn-probability
  mini-map from a small seeded ensemble.

### Method stack (name-by-name)

- **Surface spread — Rothermel (1972)**, coded against **RMRS-GTR-371 (Andrews 2018)**:
  `ROS = I_R·ξ·(1+Φ_w+Φ_s) / (ρ_b·ε·Q_ig)`. Closed-form algebra, fully vectorizable.
  Fuels = **Scott & Burgan FBFM40** (already on disk; see §3).
- **Wind & slope + midflame reduction.** `Φ_w`, `Φ_s` combine as vectors into an
  effective wind and max-spread direction. The user's wind is treated as **20-ft
  open wind**, matching RMRS-GTR-266 / Behave / FlamMap wind-adjustment convention,
  and is reduced to midflame via a **Wind Adjustment Factor** from canopy cover
  (RMRS-GTR-266). The
  **Andrews (2013) wind-limit cap** is applied before `Φ_w` — load-bearing, or a
  high demo wind yields a runaway ROS.
- **Fireline intensity & flame length — Byram (1959).** `I = H·w·R` (kW/m);
  `L = 0.0775·I^0.46` (m). Thomas' `L ∝ I^0.667` where a cell is crown fire.
- **Crown fire — Van Wagner (1977) / Scott & Reinhardt (2001).**
  initiation `I₀ = (0.010·CBH·(460+25.9·FMC))^1.5`; active-crowning
  `R_active = 3.0/CBD`. `CBH/CBD/CH` from LANDFIRE canopy layers (on disk). `FMC`
  is **derived from the weather scenario** — see §2, the crux of this spec.
- **Front propagation — anisotropic Minimum-Travel-Time (Dijkstra / eikonal
  arrival-time).** A single heap-based Dijkstra from the ignition cell over the
  8-neighbor graph, edge cost = directional travel time = distance / `ROS(θ)`,
  with the Anderson/Finney elliptical anisotropy
  `ROS(θ) = a(1−e²)/(1 − e·cos θ)`, `L/W = min(0.936·e^{0.2566U} + 0.461·e^{−0.1548U} − 0.397, 8)`.
  Reported flank ROS uses the ellipse semi-minor rate, not the 90-degree polar
  radius: `flank = head·√((1−e)/(1+e))`; back ROS is `head·(1−e)/(1+e)`.
  The effective wind that shapes the ellipse is capped against the current
  reaction intensity with the same Andrews-style wind-limit guard used by the
  surface ROS engine, preventing high-wind ellipse blowups after moisture and
  hydrology damping. Returns `T(x,y)` in minutes; isochrone perimeters are
  `T ≤ t` contours.

**Why MTT over the alternatives.** MTT reuses the exact `heapq` priority-flood +
topological-sweep machinery and the `_NB` 8-neighbor table already in
`twin_hydrology.py`; it is order-independent (no CFL time-step, no level-set
reinitialization); and its arrival-time raster drapes/identifies like every
hydrology grid and animates naturally under a time slider. **Level-set
(ELMFIRE-style `∂φ/∂t + R|∇φ| = 0`)** is the documented future upgrade if a
smoother physical front is ever wanted — also numpy-native but carries the
stability/reinitialization cost. **FSim ensemble** (needs multi-decade climatology
+ ignition density + suppression calibration), **ML/DL emulators** (no local
training data; a 0.6 km² parcel is deep out-of-distribution; benchmark AUC-PR ≈ 0.28),
and a **FARSITE-style vector Huygens** front (fragile polygon maintenance) are all
out of scope — see §12.

## 2. Weather & fuel-moisture scenario model  ← the decided crux

**Decision:** the tool is driven by a coherent **weather scenario**, and the engine
**derives every fuel-moisture input from it** — including crown **foliar moisture
content (FMC)**. No raw moisture sliders. This resolves the previously-open weather
and FMC questions.

### 2.1 The scenario abstraction (user-facing inputs)

A scenario is the minimal set from which all moistures derive deterministically:

1. **Date** (→ day-of-year `DJ`) and afternoon time.
2. **Weather class** *or* explicit `T_air_F`, `RH_min_pct`, `wind_mph`, `wind_dir_deg`.
   `wind_mph` is 20-ft open wind, not 10 m wind. If future UI/API inputs accept
   meteorological 10 m wind, convert before the WAF (`10 m wind ≈ 1.15 × 20-ft
   wind`) and record that conversion. `wind_dir_deg` is the **downwind /
   maximum-spread azimuth**, not the meteorological wind-from bearing.
3. **Days since a wetting rain** (`days_since_rain`).
4. **Drought class** — `normal` / `dry` / `severe` / `extreme` (optionally backed by
   ERC percentile or KBDI from a baked gridMET snapshot; see §3).
5. **Exposure class** — `shaded` (closed forest litter, the Adirondack default) /
   `mixed` / `open`.
6. **Phenology** — auto from date (or override `dormant` / `greenup` / `leaf-on-drought`).

Wind is a **spread** input, not a moisture input.

### 2.2 Dead fuel moisture (1-hr / 10-hr / 100-hr)

**Simard equilibrium moisture content (EMC)**, RH-banded (the formula used across
U.S. fire-behavior tooling), with `H` = RH %, `T` = temperature °F, `E` = EMC %:

```
H < 10 :  E = 0.03229 + 0.281073·H − 0.000578·H·T
10..50 :  E = 2.22749 + 0.160107·H − 0.014784·T
H ≥ 50 :  E = 21.0606 + 0.005565·H² − 0.00035·H·T − 0.483199·H
```

Then a **time-lag dry-down** toward class targets, `D = days_since_rain`:

```
m_i = E_i + (m_wet_i − E_i)·exp(−24·D / τ_i)
   1-hr:  τ=1 h,   E_i=E,     m_wet=35%
  10-hr:  τ=10 h,  E_i=E+2,   m_wet=35%
 100-hr:  τ=100 h, E_i=E+4,   m_wet=30%
```

Clamp `1h∈[2,35] 10h∈[3,35] 100h∈[4,40] %`. A "wetting rain" resets the class:
≥ ~0.10 in for 1/10-hr, ≥ ~0.25 in for 100-hr. This is not full NFDRS conditioning,
but it is coherent, pure-numpy, explainable, and strictly better than independent
sliders. If a baked gridMET snapshot exists, use its `fm100` as the 100-hr override
and keep scenario-derived `1h/10h`.

### 2.3 Live fuel moisture (herbaceous & woody)

Live moisture is a seasonal greenness/drought problem, **not** same-day RH.

- **v1 (no RH history required):** map the scenario's **phenology + drought class**
  to Scott & Burgan **D/L** live values (label presets with their nearest class):

  | | Live herb | Live woody |
  |---|---:|---:|
  | dormant / cured (L1) | 30 | 60 |
  | transition (L2) | 60 | 90 |
  | green-up (L3) | 90 | 120 |
  | fully green (L4) | 120 | 150 |

- **Upgrade (M7, once gridMET RH is baked): Growing Season Index** (Jolly 2005,
  NFDRS4), 21-day running mean over the daily history:
  ```
  GSI  = I_Tmin · I_VPD · I_daylength
  I_Tmin      = clip((Tmin_C + 2)/7, 0, 1)
  es(T)       = 610.7·exp(17.38·T/(239+T))   [Pa]
  VPD         = es(Tmax_C)·(1 − RHmin/100)
  I_VPD       = clip(1 − (VPD − 900)/(4100 − 900), 0, 1)
  I_daylength = clip((daylength_s − 36000)/(39600 − 36000), 0, 1)
  above GSI21 = 0.5:  M_herb = 440·GSI21 − 190 ;  M_woody = 280·GSI21 − 80
  below:              M_herb = 30% ;              M_woody = 60%
  ```
  Blocked in v1 because Daymet has no RH (VPD term); the D/L mapping is the honest
  stand-in until gridMET `rmin/rmax` is snapshotted.

Remote-sensing LFMC (NDVI/NDWI/microwave) **cannot** be derived from weather alone —
never imply otherwise.

### 2.4 Foliar moisture content (FMC) — derived from the scenario (the decision)

**Yes: derive FMC from the scenario, via the Canadian FBP seasonal model** — a
function of day-of-year and the twin's **latitude / longitude / elevation** (all in
`data/georef.json`), optionally drought-nudged. **Not** from RH: live conifer foliar
moisture is phenologically buffered, and the spring dip is physiological, not a
response to a hot dry afternoon. **This is the *conifer* case of a region-selected FMC**
(`select_fmc_method()`): the FBP spring-dip is valid for boreal/temperate North-American
conifer (right for this 76%-evergreen twin) but is switched off or replaced for
chaparral / desert / grassland / hardwood — see **Generalizability**.

FBP equations (longitude as **positive °W**; from CFFDRS `foliar_moisture_content.r`
and Forestry Canada ST-X-3):

```
ELV > 0 :  LAT_n = 43 + 33.7·exp(−0.0351·(150 − LONG))
           D0    = round(142.1·(LAT/LAT_n) + 0.0172·ELV)
ELV ≤ 0 :  LAT_n = 46 + 23.4·exp(−0.0360·(150 − LONG))
           D0    = round(151·(LAT/LAT_n))

ND  = |DJ − D0|
ND < 30      :  FMC = 85 + 0.0189·ND²
30 ≤ ND < 50 :  FMC = 32.9 + 3.17·ND − 0.0288·ND²
ND ≥ 50      :  FMC = 120
```

**For this twin** (`LAT=43.280`, `LONG=74.062 W`, `ELV≈320 m`): `LAT_n=45.35`,
`D0≈141` → the **spring-dip minimum lands on day ≈141 (May 21)**. FMC by season:

| Date | DJ | ND | FMC % | Reading |
|---|---:|---:|---:|---|
| Apr 20 | 110 | 31 | ~104 | rising out of dip |
| **May 21** | **141** | **0** | **85** | **spring-dip minimum — most crown-prone** |
| Jun 21 | 172 | 31 | ~104 | rising |
| Jul 15 | 196 | 55 | 120 | plateau (well-hydrated) |
| Oct 15 | 288 | 147 | 120 | dormant fall — crown-resistant |

The spring dip (≈ Apr 21 – Jun 20 window of depressed FMC) coincides with NY's
legislated **March 16 – May 14 residential burn ban** — the tool's most crown-prone
foliar moisture aligns with the real spring fire season. That alignment is a
credibility asset, and it is why deriving FMC (vs a constant) matters here.

**Drought nudge** (multi-week, not same-day): `normal` → +0; `severe`
(ERC > 80th pct / moderate KBDI) → −5; `extreme` (ERC > 95th pct / prolonged deficit)
→ −10. Clamp `[80,120]` (`[75,120]` only for an explicitly catastrophic scenario).

**Why it's worth deriving — Van Wagner I₀ sensitivity** (`CBH = 5 m`):

| FMC % | 460+25.9·FMC | I₀ (kW/m) | vs 85% |
|---:|---:|---:|---:|
| 85 | 2661 | ~1535 | 1.00 |
| 100 | 3050 | ~1883 | 1.23 |
| 120 | 3568 | ~2383 | 1.55 |

85% spring-dip vs 120% plateau moves the crown-initiation threshold ~**55%** — large
enough to flip class-level crown-fire outcomes. A constant is not acceptable; a
`fmc_override` input remains for expert use only.

### 2.5 Preset scenarios (label with nearest D/L class)

Concrete v1 presets for this parcel (shaded litter; scenario-class numbers, not
measurements):

| Preset | Narrative | Dead 1/10/100 | Live herb/woody | FMC |
|---|---|---:|---:|---:|
| Normal spring | Apr 20, 60 °F, RH 45%, 8 mph, 5 dry days | 9 / 11 / 18 | 30 / 60 | ~104 |
| High — dry windy spring | May 10, 68 °F, RH 30%, 15 mph, 10 dry days | 6 / 8 / 12 | 60 / 90 | ~88 |
| Extreme — spring Red Flag | May 21, 78 °F, RH 15%, 25 mph, 21 dry days, severe drought | 4 / 6 / 8 | 60 / 90 | 80 |
| Late-summer drought | Aug 15, 85 °F, RH 25%, 18 mph, 14 dry days, extreme drought | 5 / 7 / 10 | 60 / 90 | 110 |
| Dormant fall | Oct 20, 55 °F, RH 30%, 12 mph, 8 dry days | 7 / 9 / 14 | 30 / 60 | 120 |

### 2.6 Moisture guards (mandatory)

- Clamp `RH∈[1,100]`; clamp every moisture output to its plausible range.
- If SWE/snow is present (queryable from the twin), force very-wet dead fuels /
  disable spread.
- If a cell's dead moisture exceeds the fuel model's **moisture of extinction**,
  Rothermel damping drives `ROS → 0` (an absorbing state in the Dijkstra).
- Document the small EMC discontinuities at 10% and 50% RH.
- **Never** infer RH or wind from Daymet climatology — the scenario (or a baked
  gridMET snapshot) must supply them.

## 3. Data & inputs

### 3.1 Already on disk (verified) — a complete `.LCP`-equivalent fuelscape

In `data/atlas/local/` (with `data/atlas/vat/<id>.json` value→{name,color} sidecars):

- **Surface fuel:** `landfire_fbfm40_2024` (Scott & Burgan 40 — **use this**; TL/TU
  classes fit humid eastern litter) and `landfire_fbfm13_2024` (Anderson 13).
- **Canopy:** `landfire_cc_2024` (cover %), `landfire_ch_2024` (height ×10 m),
  `landfire_cbh_2024` (base height ×10 m), `landfire_cbd_2024` (bulk density ×100 kg/m³).
  **Mind the ×10 / ×100 encoding scale factors.**
- **Terrain:** `data/terrain/grid.json` (220×289, xStep ≈ 3.06 m; the frozen grid
  contract in `docs/grid-contract.md`); slope/aspect via `twin_hydrology.slope_radians`.
- **Georef:** `data/georef.json` — `origin_wgs84` (lat 43.280, lon −74.062),
  `grid_min_elevation_m` 287.4, EPSG:26918. Supplies FMC's lat/lon/elevation.
- **Climate:** `data/climate/forcing-summary.json` — 44-yr Daymet daily climatology
  (tmax/tmin/prcp/swe/srad/dayl; **no wind, no RH**).
- **Per-tree fuels:** `data/vegetation/tree_instances.json` (29,109 stems) — useful
  for a future full Albini/BehavePlus stochastic spotting solver. Current scenarios
  export a screening downwind ember-exposure band, not recursive spot-fire ignition.
- **SSURGO:** `data/soils/` — indirect dead-fuel/soil-moisture proxy if wanted.

> **Vintage/extent:** `*_2024` is a *pinned* LANDFIRE snapshot (LF2025 exists) —
> parameterize the edition, never call it "latest." LANDFIRE FBFM40 + canopy cover
> **CONUS + Alaska + Hawaii + Puerto Rico/USVI** (via LFPS), so the same fetcher
> generalizes to any US parcel; outside the US there is no LANDFIRE — see
> **Generalizability**.

### 3.2 To vendor (no download)

- **Scott & Burgan FBFM40 parameter table** — a static ~40-row numpy lookup (dead
  1/10/100-hr + live herb/woody loads, SAV, bed depth, moisture-of-extinction, heat
  content) transcribed from **RMRS-GTR-153 Table 7**. Lives in
  **`packs/us-national/fuels.py`** — it is *national* fuel-model knowledge, **not**
  Adirondack botany (moving it out of the regional pack is a generalizability fix; see
  **Generalizability**). It exposes the FBFM40→parameters map; **scenario presets and
  the default FMC method live in the regional pack**, and the moisture *physics*
  (Simard/GSI/FBP) plus the FMC selector are universal → engine.

### 3.3 Fire weather

The one true data gap. **v1: preset-driven** — wind + weather class as scenario
inputs, moisture derived per §2. **No fetch.** **Optional (unlocks GSI live moisture
+ ERC/KBDI drought classing):** bake a one-time **gridMET** snapshot (`vs`, `th`,
`rmin/rmax`, `fm100/fm1000`, `erc`) into a fire-weather climatology, mirroring the
Daymet pattern — gridMET machinery already exists (`gridmet_tmean_2025`,
`gridmet_precip_2025` are in the atlas) via `packs/us-national/` / `national_fetch.py`.
Never fetched at view time.

### 3.4 Wildfire Risk to Communities reference drapes  ← included in v1 (not deferred)

Ingest the national **Wildfire Risk to Communities** rasters as **reference/
calibration drapes** through the existing generic path
(`scripts/add_layer.py` → reproject any-CRS → scene-local, clip to footprint,
auto-style, append to `viewer-layers.json`, register in store): **Burn Probability
(BP)**, **Conditional Flame Length (CFL)**, **Flame-Length Exceedance (FLEP4/8)**,
optionally **Wildfire Hazard Potential** and **Conditional Risk to Potential
Structures**. Add a `packs/us-national/fetch_wrc.py` fetcher (sibling of
`fetch_landfire.py`) that pulls the `apps.fs.usda.gov` `RDW_Wildfire` ImageServer
tiles for the AOI, then `add_layer.py` each.

These are **WRC v2 (2024 publication): burn probability from FSim at 270 m upsampled
to 30 m, built on LANDFIRE 2020 landscape conditions; US lands (50 states + DC; AK/HI
included, territories unverified), and not ignition-sensitive** — so
they are a complementary authoritative anchor a viewer can compare the live scenario
against, **not** a replacement for it. (Outside the US, WRC is disabled — EFFIS/GWIS
are the European reference, not a WRC equivalent; see **Generalizability**.) They ride the atlas layer channel (so MCP
`set_layer_visibility`/`filter_layer` can reveal them, unlike the viewer-toggled
`fire`/`fire_scenario` drapes). Surface their vintage/resolution on the card.

### 3.5 Resolution

Fuels are 30 m; the DEM is 3 m over the same footprint. **Run spread on the 3 m
grid**, nearest-neighbor-upsampling the categorical FBFM40/canopy grids, so terrain
steering is at native LiDAR resolution and the drape aligns with hydrology. Surface
the honest caveat: fuel is genuinely one class per 900 m². Compute is sub-second
either way (63.6k cells).

## 4. Engine — `scripts/twin_fire.py`

Same posture as `twin_hydrology.py`: imports only `heapq/json/math/os/numpy`; no
file I/O beyond reusing `twin_hydrology.load_grid`; `__main__` smoke test; fully
deterministic (any ensemble RNG seeded `np.random.seed(141)` so re-runs are exact
no-ops against the store). Reuse `twin_hydrology._NB` and `slope_radians`.

Public surface (the moisture physics **and the FMC selector** are region-agnostic →
they live here; the FBFM40 parameter table comes from `packs/us-national`, and scenario
presets + the default FMC method from the regional pack — see **Generalizability**):

```
# --- fuel-moisture scenario (§2) ---
emc_simard(T_F, RH_pct) -> EMC_pct                     # RH-banded Simard EMC
dead_moisture(T_F, RH_pct, days_since_rain, exposure)  # -> (m1, m10, m100) %
gsi21(tmin_C, tmax_C, rhmin_pct, daylength_s)          # -> GSI (M7, needs RH history)
live_moisture(gsi_or_class, phenology, drought)        # -> (herb, woody) %
fbp_fmc(lat, lon_west, elev_m, doy, drought)           # -> FMC %  (Canadian FBP; the CONIFER case)
select_fmc_method(veg, region) -> method               # dispatch: fbp | lfmc_obs | not_applicable | const
derive_fmc(method, veg, region, doy, drought, lfmc?)   # region-selected FMC (see Generalizability)

# --- fire behavior ---
fuel_bed(fbfm_grid, param_table, live_herb_moisture)   # per-cell load/SAV/depth/Mx/heat;
    # DYNAMIC herbaceous transfer for GR/GS: cured=clip((120-M_herb)/(120-30),0,1);
    # move cured*live_herb_load into the 1-hr dead class before Rothermel
midflame_wind(wind_20ft_open, canopy_cover)            # WAF reduction (RMRS-GTR-266)
wind_slope_factors(fuelbed, sav, midflame_u, slope, aspect, wind_dir)
    -> (phi_w, phi_s, eff_wind, max_dir)               # Andrews-2013 wind cap applied
rothermel_ros(fuelbed, moisture, phi_w, phi_s)         # head-fire ROS, m/min
byram_intensity(ros, fuel_consumed, heat)              # kW/m
flame_length(intensity, crown_mask=None)               # Byram / Thomas
active_crown_ros(open_wind_mph, slope, moisture)       # 3.34 * original FM10 at 0.40 WAF
crown_class(surf_I, crown_ros, cbh, cbd, fmc)          # {0 surface,1 passive,2 active};
    # VALIDITY GATE: CBD<=0 / CBH<=0 / no conifer canopy -> surface / not-applicable
    # (chaparral reports high SURFACE flame length, not Van Wagner tree-crown fire)
torching_crowning_index(fuelbed, cbh, cbd, fmc, slope, moisture)  # (TI, CI); scalar
    # Rothermel-over-wind inversion by bisection — numpy, no scipy
ellipse_lw(eff_wind_mph)                               # Anderson/Finney L/W, cap 8
arrival_time(ros_field, eff_wind, max_dir, ignition_cells, cellsize)
    -> T(x,y) minutes                                  # anisotropic Dijkstra over _NB
compute_static(grid, fuelbed, canopy, moisture_scenario)
    -> {base_ros, slope_ros, crown_potential, TI, CI, cell_area_m2}   # Tier-1
```

**Numeric guards** (parallel to hydrology's slope floor / CN clamp / epsilon fill):
clamp midflame wind ≥ 0 and apply the wind-limit before `Φ_w`; floor `tan φ`; guard
nonburnable (NB 91–99) and zero-load cells to `ROS=0`; zero ROS above moisture-of-
extinction; the §2.6 moisture guards; NaN-fill near footprint holes.

## 5. Tier-1 exporter — `scripts/analyze_fuels.py`

Mirror `analyze_hydrology.main()`: `import analyze_hydrology as t1` and reuse
`t1.write_png / colorize / ramp / grid_json / percentile_norm / _use_data_dir`.
Build a local `export(layer_id, label, rgba, values, legend, description, decimals,
metadata)` closure that writes `data/fire/local/<id>.png` + `<id>.grid.json` and
appends a layer dict with `group:"fire"`. Each layer carries `value_kind` /
`value_unit` metadata (e.g. `m/min`, `m`, class code) so the identify card degrades
gracefully like hydrology's `flowSampleToHa` rather than emitting bare numbers.

Tier-1 layer ids: `fuel_model` (categorical, from FBFM40 VAT colors), `base_ros`
(no-wind/no-slope), `slope_hazard` (slope-driven ROS potential),
`torching_index` and `crowning_index` (20-ft open-wind thresholds in mph). TI/CI
are the FlamMap-style presentation of crown potential: lower values mean the
stand torches / actively crowns more easily; cells whose threshold is not reached
by the 120 mph cap are shown as a distinct crown-resistant class, while no-canopy
cells are transparent/not applicable. The older Tier-1 `crown_potential` drape is
not exported; the Tier-2 scenario `crown_class` layer remains unchanged.

Write `data/fire/summary.json` (fuel-model breakdown, mean slope/aspect, canopy
stats, reference crown-class fractions, TI/CI baseline, FMC-by-season note, honest-framing
note) and `data/fire/fire-layers.json` (the catalog; preserve any `group:"fire_scenario"`
entries a prior `fire_scenario.py` appended, exactly as `analyze_hydrology.py`
preserves `group=="scenario"`). Register `fire_<id>` layers in the store:
`begin_run('analyze_fuels.py', …)`, `upsert_layer('fire_'+id, kind='raster',
acquisition='derived', source_path, content_sha1=sha1(png))`, `finish_run`; wrap in
try/except → warning. Add `npm run analyze-fuels`.

## 6. Tier-2 scenario CLI — `scripts/fire_scenario.py`

`argparse` main mirroring `hydro_scenario.py`, with the ignition + weather scenario:

```
--ignition-x, --ignition-y     scene-local meters (or --ignition-line "x1,y1;x2,y2;...")
--date YYYY-MM-DD              -> day-of-year for FBP FMC (default: today)
--weather-class {normal_spring|high_spring|extreme_redflag|summer_drought|dormant_fall}
   or explicit:  --temp-f --rh-min --wind-mph --wind-dir
                 (--wind-dir is downwind / max-spread azimuth)
--days-since-rain             (default 5)
--drought {normal|dry|severe|extreme}
--exposure {shaded|mixed|open}   (default shaded)
--fmc-override                expert-only; bypass the FBP derivation
--duration-min                (default 240)
--ensemble N                  optional seeded wind/moisture Monte-Carlo -> burn_probability
--json  --data-dir
```

Replicate the **data-dir redirection** (reassign module `D`/`STORE_PATH`,
`twin_store.JOURNAL_DIR`, `twin_georef.GEOREF_PATH`, `t1._use_data_dir(D)`).

Pipeline: load grid + fuelscape + canopy (upsample to the 3 m DEM grid); derive the
full moisture set from the scenario (§2) — FMC from `twin_georef` lat/lon +
mean parcel elevation + `--date` + `--drought`; map ignition scene-local (x,y) →
(row,col) via the grid-contract cell-center math; run `twin_fire.arrival_time` +
intensity/flame/crown fields; `export()` `group:"fire_scenario"` layers: `fire_arrival`
(minutes), `flame_length` (m), `fireline_intensity` (kW/m), `crown_class`,
`ember_exposure`, and (if `--ensemble`) `burn_probability` (0–1). **Merge only `group=="fire_scenario"`**
into `fire-layers.json`, preserving `group=="fire"`.

Write `data/fire/last-fire-scenario.json`: ignition point (scene-local + lat/lon via
`twin_georef.transformers()`), the full scenario echo, the **derived moisture set**
(so the card and MCP can explain *why* fire behaved as it did), per-flank head/flank/
back ROS, max flame length, crown fractions, burned-area estimate, `layers`, `notes`,
`run_id`. Open one store run with the clamped params as inputs. **Print the result
JSON as the last stdout line** under `--json` (the store `journal:` note precedes it —
the `lines[-1]` contract both the server and MCP rely on). Add `npm run fire-scenario`.

Store/journal: reuse existing journal op kinds — `rebuild_store.py` replays fire runs
unchanged. Any ignition-marker entity uses the deterministic
`"<kind>:"+sha1(source|round(x,1)|round(y,1))[:12]` id and 0–1 confidence.

### 6.1 Hydrology-to-fire coupling — `scripts/hydro_fire.py`

Tier-2 can apply `scripts/hydro_fire.py` before spread. It returns a
`hydro_barrier_mask` plus per-cell moisture arrays. Open-water polygons, NWI
open-water classes, wide waterbody/stream coverage, soil water table at the
surface, snow/SWE, and normal-moisture saturated ponding can force ROS to zero.
Wetlands are moisture dampers rather than automatic hard barriers; TWI, ponding,
seep candidates, contributing-area flow paths, wetland polygons/edges, and mapped
stream corridors lift 1-hour, 10-hour, 100-hour, herbaceous, and woody moistures
toward riparian/wet/very-wet/saturated targets. Drought scales that uplift down,
and no-width stream centerlines are treated as wet corridors rather than hard
firebreaks.

## 7. Server — `POST /api/fire-simulate`

Add `handleFireScenario()` in `server.js`, a copy of `handleSimulate()` (L2781):
build argv → clamp (argv-only, never shell-interpolated) → `spawn(FIRE_PYTHON, argv,
{cwd:ROOT, env:{...process.env, TWIN_DATA_DIR:DATA_DIR}})` → `SIMULATE_TIMEOUT_MS`
(120 s) kill → parse `lines[-1]` as the result JSON. `FIRE_PYTHON = VEIL_FIRE_PYTHON
|| HYDRO_PYTHON`. Register the route in the dispatcher (~L3099) and add
`'/api/fire-simulate'` to **`CSRF_PROTECTED`** (L2979) so `sameOriginOk()` gates it
(non-browser MCP clients send no Origin/Referer and pass).

Clamps (mirroring the hydrology clamps at L2790–2811):

- `ignition_x`, `ignition_y`: numeric, must fall inside the grid's scene-local bounds
  (reject otherwise).
- `wind_mph`: `min(120, max(0, x))` as 20-ft open wind (paired with the Andrews-2013 engine cap).
- `wind_dir`: `((x % 360) + 360) % 360`; downwind / maximum-spread azimuth,
  not meteorological wind-from direction.
- `temp_f`: `min(130, max(-20, x))`; `rh_min`: `min(100, max(1, x))`.
- `days_since_rain`: `min(120, max(0, x))`.
- `fuel_source`: `landfire|computed`; `hydrology`: `on|off`.
- `weather_class` / `drought` / `exposure`: enum allowlists; `weather_class` includes `custom`.
- `date`: ISO `YYYY-MM-DD`, else default today.
- `duration_min`: `min(1440, max(1, x))`; `ensemble`: `min(200, max(0, x|0))`.

## 8. MCP — `scripts/twin_query.py` + `scripts/mcp_server.py`

In `twin_query.py`, add `FIRE_DIR` / `FIRE_SIM_CATALOG` / `FIRE_SUMMARY` /
`FIRE_LAST_SCENARIO` path constants next to the `HYDRO_*` ones (L60–63), a
`fire-layers.json` loader, and (mirroring `hydrology_at`@2847 / `hydrology_summary`@2895
/ `run_scenario`@2914 / `_scenario_argv`@2959):

- `fire_at(point)` — sample the fire grids at a scene-local or `{lat,lon}` point (via
  `resolve_region`@443), return the synthesized reading + the derived-moisture context
  + provenance (`source/confidence/run_id/observed_at`).
- `fire_summary()` — read `data/fire/summary.json` + `last-fire-scenario.json`.
- `run_fire_scenario(...)` — the **only new store-writing MCP tool** (document as such
  in the FastMCP instructions string, `mcp_server.py` L49). Shell `fire_scenario.py`
  via `subprocess.run([sys.executable, …]+argv, cwd=PROJECT, env={TWIN_DATA_DIR:DATA},
  timeout=180)`.
- `_fire_scenario_argv(...)` — clamps matching `server.js` **byte-for-byte** (HTTP and
  MCP must not diverge — a hard rule).

In `mcp_server.py`, add thin `@mcp.tool()` wrappers `fire_at` / `fire_summary` /
`run_fire_scenario` over `_run(_query().<fn>)`, and extend the instructions string.

The **WRC reference drapes are atlas layers**, so they are already
`set_layer_visibility` / `filter_layer` / `layer_summary`-able through the existing
atlas-catalog channel — no new MCP work. The `fire`/`fire_scenario` drapes are
viewer-toggled (like hydrology scenario layers), not in the atlas catalog.

## 9. Viewer — `public/wildfire.js` + Fire pane

The Simulation surface is a **left rail-driven flyout pane** (`shell.css .flyout`;
`index.html section.pane[data-pane]`); outputs land in the right `#inspector`
(`#key-list` + `#identify-results`). The Fire feature is **another left flyout pane**.

- **`public/wildfire.js`** — IIFE → `window.ADKLRWildfire.create(api)` with the same
  `api` contract `simulation.js` uses (`{catalog, isEnabled, isLoading, setEnabled,
  refresh}`). Owns no pixels; all drape/identify/key flow through `app.js`. It owns:
  a `fire-*` els map; `renderPresets` (the §2.5 weather presets); `buildParams()`;
  the ignition picker (below); form submit → `POST /api/fire-simulate` →
  `renderResult` + `api.refresh(data.layers)`; `renderToggles/toggleRow` (fire-group
  swatch colors — `fire_scenario` = fire-red, `fire` = ember-orange); `boot()`
  (restore `summary.json` + `last-fire-scenario.json`); and **`interpretAt(x,y,samples)`**
  → the "Fire at this spot" card (§9.2).
- **`public/index.html`** — add `<script src="/wildfire.js">` **before** `/app.js`
  (the module must exist in `main()`). Add a **Fire** rail button (`data-mode="fire"`,
  e.g. title "Simulate fire") and a `<section class=pane data-pane=fire>` cloning the
  Simulate pane markup (a Terrain-fire-layers group, a Weather-scenario group with the
  ignition picker + presets + form, a Scenario-layers group).
- **`public/shell.js`** — add `fire: 'Simulate fire'` to `TITLES` (L19).
- **`app.js` wiring** — `allLayers()` (L51) concat `state.wildfire?.layers`;
  `main()` `state.wildfire = await loadWildfireCatalog()` + seed each fire layer
  `enabled=false` (mandatory or toggles break) + `window.__twin.wildfire =
  ADKLRWildfire.create(...)`; clone `loadSimulationCatalog`/`refreshSimulationLayers`
  → `loadWildfireCatalog`/`refreshWildfireLayers` (refetch, drop stale `layerData`,
  `ensureEnabledLayerData`, `redrawDrape`, `renderKey`). `renderKey()` (L836) already
  surfaces `description`/`group` generically. Raster<polygon<line<point draw order at
  `globalAlpha 0.8` is unchanged.

### 9.1 Ignition picker

Reuse the **terrain raycast** the GPS readout / chat "Pick point" use (the
`updatePickReadout` path, `app.js` L1116+). Flow:

1. Fire pane shows **"Pick ignition"** (and optionally "Draw ignition line", reusing
   the multi-click "Draw region" raycast).
2. While the mode is active, the readout's click handler **stands down** — it already
   checks `__twin.chat.state.mode`; add a parallel `__twin.wildfire.state.mode` guard
   so a click sets the ignition instead of identifying.
3. On click: raycast → world → **scene-local meters** (`x = easting − origin`, the
   georef inverse the readout already computes). Store `{x,y}`, drop an orange
   ignition marker (reuse annotation marker rendering), show its lat/lon.
4. `buildParams()` sends `ignition_x/ignition_y` (line: `;`-joined) + the weather
   scenario to `POST /api/fire-simulate`.
5. Result renders the isochrone drape + the card; a duration/time slider contours
   `fire_arrival` at `T ≤ t`.

### 9.2 "Fire at this spot" identify card

The **one hardcoded gate to touch:** `app.js identify()` L1047 whitelists
`layer.group === 'hydrology' || 'scenario'` for routing raster samples to
`interpretAt`. **Extend it to `'fire'` and `'fire_scenario'`**, then call
`window.__twin.wildfire.interpretAt(x, y, samples)` and append a second
`div.info-card` via `identifyResultsHtml` (L963) — or fire samples fall through to
raw-number rows (L1051). `interpretAt` is keyed by `layer.id`; each id needs a
sentence builder or its sample is dropped; return `null` when no sentences accrue.
Draft, hedged in the hydrology house style ("~", "±", "worth a field check", scenario
framing):

- `fire_arrival` → "Fire reaches this spot ~34 min after ignition (±class; one wind guess)."
- `flame_length` → "Flame length ~2.4 m — passive-crown range."
- `crown_class` → "Modeled as surface / torching / active-crown fire under this scenario."
- `torching_index` / `crowning_index` → "This stand torches / actively crowns at
  ~47 mph 20-ft open wind — crown-prone" or "Crown-resistant here: does not torch
  below 120 mph 20-ft open wind (given this fuel)."
- `fuel_model` → the FBFM40 VAT class name ("TL2 broadleaf litter").
- plus a one-line scenario recap ("May 21, RH 15%, 25 mph, FMC 80% — spring-dip crown risk")
  and, if a building is within ~30–60 m, a Home-Ignition-Zone exposure sentence.

## 10. Phased implementation

Each milestone is independently verifiable per the repo's no-test posture — numeric
via the `__main__` smoke test + oracle cross-check (firebehavioR `ros()` / fireLib /
pyretechnics; CFFDRS for FMC), visual via `scripts/screenshot.js` against `npm start`.
Effort is rough, one engineer.

| # | Milestone | Verify | Effort | v1 |
|---|---|---|---:|:---:|
| **M0** | Fuel table (`packs/us-national/fuels.py`) + engine core (`fuel_bed` incl. **dynamic herbaceous curing**, `wind_slope_factors`, `rothermel_ros`, `byram_intensity`, `flame_length`) | smoke test matches firebehavioR/fireLib; cured-grass GR/GS transfers live→dead | 2–3 d | ✓ |
| **M1** | Fuel-moisture module (`emc_simard`, `dead_moisture`, `live_moisture`, `fbp_fmc`) + **`select_fmc_method()` region selector** | FMC matches CFFDRS for the twin; selector returns FBP for conifer, `not_applicable` for grass/shrub | 1–2 d | ✓ |
| **M2** | Crown fire (`crown_class` **with conifer-canopy validity guards**, `torching_crowning_index`) + Tier-1 exporter `analyze_fuels.py` + `npm run analyze-fuels` | harness drapes fuel_model/base_ros/torching_index/crowning_index; a canopy-free cell reports not applicable for TI/CI and surface, not crown, in scenario crown_class | 2 d | ✓ |
| **M3** | Front propagation `arrival_time` (anisotropic Dijkstra) | synthetic flat uniform-fuel grid → circular front; wind/slope skew downwind/uphill; sub-second | 2–3 d | ✓ |
| **M4** | Tier-2 CLI `fire_scenario.py` (ignition + weather scenario → arrival/flame/crown, group merge, last-scenario, store run) + server `handleFireScenario` + clamps + CSRF | `curl POST /api/fire-simulate` returns envelope with `layers[]`; clamps reject OOB ignition, cap wind | 2–3 d | ✓ |
| **M5** | Viewer Fire pane + ignition picker + refresh + identify-gate extension | harness: open pane, pick ignition, run, isochrone drape toggles; no console errors | 3 d | ✓ |
| **M6** | "Fire at this spot" card + honest framing + HIZ sentence | burned cell → synthesized card; nonburnable → graceful reading | 1–2 d | ✓ |
| **M7** | WRC reference drapes (`packs/us-national/fetch_wrc.py` → `add_layer.py`) | BP/CFL/FLEP drape + identify + MCP `filter_layer` | 1–2 d | ✓ |
| **M8** | MCP tools `fire_at`/`fire_summary`/`run_fire_scenario` + `_fire_scenario_argv` clamps (byte-for-byte with server) + tests | MCP session runs a scenario, reads `fire_at`; HTTP==MCP for equal inputs | 1–2 d | ✓ |
| **M9** (opt) | Ensemble burn-probability; gridMET fire-weather + GSI live moisture + ERC/KBDI drought classing; Albini spotting from `tree_instances.json`; level-set upgrade | each drape independently verified | 3–5 d | — |
| **M10** (global) | `fire_fuel_provider` pack hook; `packs/nato/fire.py` EFFIS + land-cover→FBFM crosswalk; ERA5-Land weather; WRC disabled off-US | a European/Chile twin runs screening-grade spread; a no-fuel twin skips with a recorded reason | 4–6 d | — |

**v1 = M0–M8** (~3 engineer-weeks; WRC drapes and the scenario-derived weather/moisture
model are in scope). The **CONUS-generalization fixes** (fuel table → `packs/us-national`,
`select_fmc_method`, dynamic fuels, crown guards) are folded into M0–M2, so v1 is correct
across all CONUS fuel types — not just the Adirondack reference twin. M9 is the
differentiator backlog; **M10 extends the feature to VEIL's European/global footprint.**

## Generalizability — CONUS-wide, and VEIL's global footprint

The reference twin is one humid NE-US parcel, but VEIL is a region-agnostic engine and
the sister repo (`../veil`) already builds twins across the full CONUS fuel spectrum
(desert `saguaro`/`whitesands`/`moab`, CA chaparral + Sierra `bigsur`/`sequoia`,
tallgrass `flinthills`, wetland `everglades`/`okefenokee`, Appalachian hardwood
`smokies`, PNW/Rockies conifer `mthood`/`hoh`/`sawtooth`/`teton`) **and globally** via
`packs/nato/` (all 32 NATO members + a global 30 m open-data fallback, e.g. the Chile
twin). The fire feature must generalize to that footprint. A GPT-5.5 (xhigh)
generalizability study (both repos + web, verified against the sources below) found the
**physics stack is portable; the geography-specific parts are the fuel data, the foliar-
moisture model, crown-fire applicability, and the scenario presets.** Verdict:
**structurally generalizable, and correct across all CONUS fuel types after four fixes**
(the "US-wide set" below); VEIL's global footprint needs a fuel-provider hook and
degrades honestly to screening-grade.

### The fire-fuel capability ladder (mirror the vegetation ladder)

Exactly like `analyze_vegetation.py`'s LiDAR → CHM → NDVI → skip ladder, fuels degrade
by data tier, and every result carries `fuel_source` + `fuel_data_tier` provenance:

| Tier | Fuel source | Model validity | Fallback |
|---|---|---|---|
| **CONUS + AK + HI + PR/USVI** | LANDFIRE FBFM40 + canopy (30 m, LFPS) | Rothermel+FBFM40 authoritative; Van Wagner where conifer canopy; gridMET drives GSI/ERC | — |
| **VEIL Europe (`nato`)** | EFFIS European Fuel Map (JRC 2017; 42 complexes → Anderson-13) or the 2023 85-type EU classification | surface spread via an Anderson-13 crosswalk; crown only where conifer | low-confidence crosswalk metadata |
| **Global (`nato` fallback)** | ESA WorldCover / CORINE / CGLS land-cover → FBFM/Anderson crosswalk | **screening-grade** surface geometry | land-cover proxy, clearly labeled |
| **No defensible fuel** | — | — | **skip the fire sim**, record why (like the veg ladder) |

Weather: gridMET is CONUS-only → **ERA5-Land** (global) or explicit user weather outside
CONUS. WRC drapes are **US-only** → disabled elsewhere; EFFIS/GWIS are the European
reference, not a WRC equivalent.

### Region-selected foliar moisture (the biggest correctness fix)

The Canadian FBP spring-dip FMC (§2.4) is valid **only for boreal/temperate North-
American conifer** — right for the Adirondack twin, Alaska, Canada, PNW/Rockies conifer,
and (acceptably) temperate European conifer, but **wrong** for CA/Mediterranean
chaparral, the arid Southwest, southern pine, hardwood, grassland, and tropical
Hawaii/Caribbean. Replace the bare `fbp_fmc()` call with the engine
`select_fmc_method()` / `derive_fmc()` dispatch:

| Case | FMC method |
|---|---|
| No tree canopy (NB/GR/GS/SH, chaparral, desert, grassland) | `not_applicable` — crown module **off**; live herb/woody still drive surface fuels |
| Boreal/temperate NA (and temperate EU) conifer | `fbp_spring_dip` (CFFDRS/ST-X-3 D0/ND curve) |
| Mediterranean / chaparral shrub | pack-calibrated **live-woody LFMC** from NFMD/FSD observations or an RS index (e.g. Dennison NDWI→LFMC), **not** crown FMC |
| Hardwood/deciduous | crown model off by default; if forced, conservative `FMC=120%`, labeled low-confidence |
| CONUS conifer outside FBP comfort / unknown | gridMET-GSI/water-balance, or `FMC=100%` constant with a high-uncertainty warning |

The regional pack supplies the default method and any local LFMC coefficients; the
engine owns the selector and each method's math. Every result records `fmc_method`. A
pack-calibrated precipitation/drought LFMC (chaparral) fits, e.g.,
`LFMC = clip(Lmin, Lmax, β0 + β1·NDWI + β2·Σ(P−ET0)_{30–90d} − β3·VPD_{21d} + seasonal)`
— coefficients fit to NFMD/field samples, **not** derivable from weather alone.

### Dynamic herbaceous fuels

The static FBFM40 lookup underpredicts **cured-grass** spread (GR/GS and any model with
live-herbaceous load) — material for `flinthills`/`badlands`-class grass twins. Add the
standard Scott & Burgan **dynamic** transfer in `fuel_bed()`:
`cured = clip((120 − M_live_herb)/(120 − 30), 0, 1)`, moving `cured × live_herb_load`
into the 1-hr dead class before Rothermel.

### Crown-model validity guards

Van Wagner is *conifer* crown fire. Guard it: `CBD ≤ 0` or `CBH ≤ 0` or no conifer
canopy → `crown_class = surface / not-applicable`; chaparral reports high **surface/
shrub** flame length, not tree crown fire. Every result records `crown_model_validity`.

### Architecture placement (generalizable by construction)

- **Engine (`twin_fire.py`, universal):** Rothermel, dynamic load transfer, WAF, Andrews
  wind cap, Byram, Van Wagner *with validity gates*, MTT/Dijkstra, Simard/Nelson dead
  moisture, GSI, and `select_fmc_method()` + each FMC method's math.
- **`packs/us-national/` (US datasets):** LANDFIRE fetch (FBFM40 + canopy, all US
  extents), **the Scott & Burgan FBFM40 parameter table** (moved here from the regional
  pack), WRC fetch, gridMET fire-weather, NFMD hooks.
- **Regional pack (e.g. `packs/adirondack/`):** scenario presets (spring Red Flag here;
  Santa Ana / monsoon-Haines / trade-wind elsewhere), the default FMC method, local LFMC
  coefficients, conifer/hardwood tuning.
- **`packs/nato/` / global:** a **`fire_fuel_provider` pack hook** returning
  `(fuel_grid, param_table, canopy, validity_tier)` (EFFIS / WorldCover crosswalk),
  ERA5-Land weather, European/Canadian presets, confidence metadata — so the engine is
  fuel-source-agnostic.

### Change set

- **Required for US-wide correctness** (fold into M0–M2): move the fuel table to
  `packs/us-national/`; add dynamic herbaceous fuels; the `select_fmc_method()` selector;
  crown validity guards; pack-supplied presets; `fuel_source`/`fuel_data_tier`/
  `fmc_method`/`crown_model_validity` provenance in `last-fire-scenario.json`; corrected
  LANDFIRE/WRC/gridMET vintage + extent text. AK/HI/PR-USVI then come for free once the
  US fetchers cover those LANDFIRE extents.
- **Required for VEIL's global footprint** (M10): the `fire_fuel_provider` hook;
  `packs/nato/fire.py` (EFFIS + land-cover crosswalk, Canada FBP fuel types); ERA5-Land
  weather ingestion; WRC disabled off-US; global "authoritative / crosswalk / land-cover
  proxy / skip" output warnings.

## 11. Honest framing

Carry the hydrology posture verbatim into every result `notes`/`uncertainty` field,
every tool docstring, and every card:

- **Reliable (geometry / where):** terrain- and fuel-channeled spread direction,
  relative arrival order, uphill/downwind flanks, where fire concentrates.
- **±class (magnitude):** absolute ROS, arrival time, flame length, crown thresholds —
  factor-of-2, dominated by 30 m fuel-class error, CBH/CBD uncertainty, and wind.
  Present flame length and probability in **binned classes** (fire-intensity levels;
  flame-length exceedance > 4 ft / > 8 ft), never spurious point values. Every card
  states its fuel model, weather scenario, derived moistures, and wind.
- **Adirondack framing is a credibility asset.** NE hardwood/mixed forest is a
  low-frequency but real, **dormant-season / drought-driven** fire regime (the 1903
  ~400k-acre Adirondack fire; the Mar 16–May 14 NY burn ban; the Nov 2024 Jennings
  Creek fire and statewide ban). Default presets to the spring / drought narratives;
  the FBP spring-dip FMC (§2.4) aligns the model's crown-prone window with the real
  spring fire season.
- **Determinism:** the base scenario is fully reproducible; any ensemble seeds
  `np.random.seed(141)` so rebuilds are exact no-ops against the store.
- **Non-goals (named):** FSim national burn probability, ML/DL spread emulators,
  coupled fire-atmosphere (WRF-SFIRE/QUIC-Fire/FIRETEC), and vector Huygens perimeter
  tracking — out of scope for the reasons in §1.

## 12. Resolved decisions (formerly open questions)

1. **Weather + moisture: scenario-derived, not sliders.** §2. FMC derived from the
   FBP seasonal model (date + georef lat/lon/elev + drought), **not** a constant
   (~55% I₀ sensitivity). *(User directive.)*
2. **WRC drapes: in v1** (M7), via `fetch_wrc.py` → `add_layer.py`, as atlas
   reference layers. *(User directive.)*
3. **Catalog: separate `data/fire/fire-layers.json`** (clean tier separation, mirrors
   the survey/sim split).
4. **Ignition: point in v1**, line optional (low marginal cost via the multi-click
   raycast).
5. **Fire weather: preset-only in v1**; optional gridMET bake unlocks GSI + ERC/KBDI (M9).
6. **FMC: derived** (see #1); `--fmc-override` for expert use only.
7. **Building susceptibility: HIZ exposure sentence in M6**; a full per-building
   susceptibility score deferred to M9.
8. **Clamps live in two places byte-for-byte:** `server.js` and
   `twin_query._fire_scenario_argv`.
9. **Generalizability (US-wide):** fuel table in **`packs/us-national/`** (not
   adirondack); **region-selected FMC** (`select_fmc_method`); **dynamic herbaceous
   fuels**; **crown validity guards** (conifer-only). Folded into M0–M2. *(User-directed
   review.)*
10. **Capability ladder + global (M10):** LANDFIRE → EFFIS/land-cover crosswalk → skip,
    with `fuel_source`/`fuel_data_tier`/`fmc_method`/`crown_model_validity` provenance;
    `fire_fuel_provider` hook + ERA5-Land + WRC-off-US for VEIL's global footprint.
11. **Vintage corrected:** LANDFIRE `*_2024` is a *pinned* snapshot (LF2025 exists),
    extent CONUS+AK+HI+PR/USVI; WRC **v2** (2024 pub; FSim 270 m → 30 m; LANDFIRE-2020
    conditions; US-only).

## Provenance

- **Modeling & feasibility:** a 16-agent workflow (6 SOTA web sweeps + 5 adversarial
  verifications, all SUPPORTED; 198 sources). Key refs: Rothermel RMRS-GTR-371;
  Scott & Burgan RMRS-GTR-153 / FBFM40; Van Wagner 1977 (x77-004) & Scott & Reinhardt
  2001; Byram 1959; WAF RMRS-GTR-266 & Andrews 2013 wind limit; FlamMap/MTT (Finney);
  ELMFIRE; LANDFIRE 2024; Wildfire Risk to Communities methods; Adirondack fire
  history (1903/1908; NYSDEC burn ban; 2024 Jennings Creek).
- **Fuel-moisture scenario model:** GPT-5.5 (xhigh) study cross-checked against the
  maintained sources — CFFDRS R `foliar_moisture_content.r` / `foliar_moisture_content_minimum.r`
  / `C6calc.r` (Forestry Canada ST-X-3); NFDRS4 `livefuelmoisture.cpp`; a
  BehavePlus-derived `FuelMoisture.js`. Simard EMC bands; Jolly 2005 GSI; Canadian
  FBP FMC (D₀/ND spring-dip curve, FME).
- **Generalizability review:** GPT-5.5 (xhigh) study across both repos + web, its key
  factual claims re-verified: LANDFIRE FBFM40/canopy extent **CONUS + AK + HI + PR/USVI**
  (LFPS; `*_2024` a pinned snapshot, LF2025 exists); WRC **v2** (2024 pub; FSim 270 m
  upsampled to 30 m; LANDFIRE-2020 conditions; US lands); **EFFIS European Fuel Map**
  (JRC 2017, 42 complexes → Anderson-13; effis.jrc.ec.europa.eu) + the ESSD-2023 85-type
  EU classification; ESA **WorldCover**; **ERA5-Land** (global weather); **NFMD/FSD**
  live-fuel-moisture; Dennison NDWI→LFMC. Sister-repo footprint confirmed:
  `packs/us-national` (CONUS build) + `packs/nato` (32 members + global open-data
  fallback) — the fire feature's US-vs-global tiering follows this split.
- Shareable summary Artifact: see the wildfire plan page published from this session.
