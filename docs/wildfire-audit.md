# VEIL Wildfire Model — Scientific & Engineering Audit

*Reconciled from two independent audits: a 9-dimension multi-agent workflow (38 agents, adversarial verification of every finding) and an independent GPT-5.5 xhigh holistic pass. Where they agree, the finding is doubly-confirmed; where one caught something the other missed, it's noted. All line numbers verified against the working tree.*

**Scope audited:** `scripts/twin_fire.py` (engine), `packs/us-national/fuels.py` (FBFM40 table), `packs/us-national/derive_fuel.py` (crosswalk), `scripts/hydro_fire.py` (hydrology influence), `scripts/analyze_fuels.py` (Tier-1), `scripts/fire_scenario.py` (Tier-2).

---

## Verdict

**Grade ≈ B+ for a screening-grade surface-fire model.** The physics engine is trustworthy: the Rothermel surface-spread & reaction-intensity core, the fuel-moisture suite (Simard EMC, time-lag dead, NFDRS2016 GSI, CFFDRS foliar moisture), the FBFM40 fuel table, wind/slope coefficients, Byram intensity/flame length, and crown **initiation** (Van Wagner I₀) + Torching Index were each checked **line-by-line against source equations** and found correct, with disciplined unit handling throughout. The suspected live-herb curing "10× unit error" **does not exist** — the code uses the correct 30–120 % window.

The real defects cluster in three places: **crown-fire spread behavior** (one genuine science error), **fire-ellipse geometry reporting**, and **the recently-added hydrology barriers** (over-containment). Plus one **live UX foot-gun** (wind direction convention) and the top missing factor, **ember spotting**. All fixes are surgical, not structural.

---

## Confirmed defects (prioritized)

### FIX NOW

**1. [HIGH · science] Active-crown class & Crowning Index use the actual model's *surface* ROS, not Scott & Reinhardt crown ROS.** *(both audits, top finding)*
`twin_fire.py` crown_class:844, torching_crowning_index:940, `_surface_for_open_wind`. Van Wagner's active criterion needs the **crown** spread rate `Ractive ≥ 3.0/CBD`; S&R (RP-29 eq 7) compute it as `Ractive = 3.34·(R10)₄₀%` — surface ROS of **fuel model 10** at **0.40× open wind**, ×3.34. The code instead uses the actual cell's fuel model, the sheltered canopy WAF (0.10–0.30), and no 3.34×. Three compounding factors all shrink the estimate → **systematically under-predicts active crowning, over-predicts CI** ("crown-resistant / not reached by 120 mph"). This is the non-conservative (false-safety) direction. **Fix:** a fixed-FM10 + 0.40-WAF crown-ROS helper for the active/CI branch only; keep the actual model for TI/initiation (which is correct).

**2. [MEDIUM · bug] Reported flank ROS is under-stated by ~L/B.** *(workflow only)*
`fire_scenario.py` `_ros_ellipse_at`:402 — `flank = head*(1−ecc)` is the ellipse's polar radius at 90°, not the true flank spread rate. **Fix (one line):** `flank = head*√((1−ecc)/(1+ecc))`. Mis-reports a headline number in the result card.

**3. [MEDIUM · bug] Fire ellipse elongation uses the *uncapped* effective wind while head ROS is wind-limited.** *(workflow only)*
`fire_scenario.py`:616–618 → `ellipse_lw`; the `0.9·I_R` cap lives only in `rothermel_ros`. In high-wind/low-intensity runs the burned-area *shape* is internally inconsistent with the head rate. **Fix (one line):** clamp `eff_wind` to `0.9·I_R` before `ellipse_lw`.

**4. [LOW · live foot-gun] `wind_dir` input is the *downwind* (spread) azimuth, opposite the meteorological "wind from" convention.** *(workflow only)*
`fire_scenario.py`:617; UI `index.html:206` "Dir…deg", `--wind-dir`/MCP unlabeled. The math is right and disclosed in *output* notes, but a user entering a weather-service "N wind = 0" bearing drives spread **180° wrong**. **Fix:** relabel input "Downwind dir" (or accept "wind from" and add 180° internally).

### FIX SOON

**5. [HIGH · missing] No ember/firebrand spotting; hydrology barriers are perfect firebreaks.** *(both audits)*
`fire_scenario.py`:619–630. The only propagation is MTT over contiguous surface ROS — any continuous barrier (lake, 5 cm depression, stream centerline, the 60 m ignition-snap gap) is an absolute firebreak. For a "what could burn near my house" screen, spotting is the dominant barrier-breach and home-ignition mechanism, and the required inputs (fireline intensity, 10 m wind, crown-class cells) already exist. **Fix:** an Albini 1983/1979 spot-distance ring + a wind-keyed "barrier may be crossed by spotting" warning. (Not out of scope; a distance ring, not a full transport model.)

**6. [MEDIUM · my hydrology feature] No-width stream centerlines are complete hard barriers under normal/dry drought.** *(workflow only)*
`hydro_fire.py`:737–742. *Every* stream in this twin lacks a width attribute, so all centerlines rasterize as ~3 m absolute ROS=0 walls (~two 1 km lines bisecting the parcel under the common spring presets). A 3 m brook is not a reliable firebreak for a 15 mph spring fire. **Fix:** downgrade the no-width centerline from ROS=0 to the existing strong wet-corridor damper (score 0.70–0.90). *(The drought-gating that drops it under severe/extreme is a good, non-naive choice — keep it.)*

**7. [MEDIUM · my hydrology feature] Ponding barrier is not drought-gated.** *(workflow only)*
`hydro_fire.py`:624–632 — `ponding ≥ 0.05 m → barrier` fires unconditionally across all drought classes, inconsistent with the wetland drought-scaling elsewhere in the same module (and ponding is a static LiDAR depression-fill *potential*, not standing water). **Fix:** drought-gate it like the wetland barriers.

**8. [MEDIUM · my hydrology feature] Hydrology moisture uplift doesn't re-run dynamic herbaceous curing.** *(Codex only; verified in code)*
`fire_scenario.py`:607 builds the fuelbed from the *pre-hydrology* scenario `live_herb`; :618 runs ROS on the hydrology-adjusted moisture. So in wet GR/GS cells the ROS is correctly damped but the live→dead load transfer still uses the dry value (too much fine dead fuel). Limited impact on this timber parcel; real for grass/shrub twins. **Fix:** build the fuelbed from the per-cell hydrology-adjusted `live_herb`.

**9. [MEDIUM · missing] Crown fire is a passive overlay that never accelerates spread/arrival/flame.** *(both audits)*
`fire_scenario.py`:623–630; `flame_length`'s crown branch (`twin_fire.py:812`) is dead code (no caller passes `crown_mask`). A cell can be "active crown" while its arrival time and flame length stay surface values (crown fires spread ~2–5× faster). **Fix:** propagate crown ROS where class ≥ active (reuses #1's FM10 helper) + the Thomas flame branch; **at minimum disclose** the surface-only limitation. (Running surface spread with a crown overlay is a recognized Tier-1 posture — hence medium — but it's undisclosed.)

**10. [LOW · honesty] Crown class/CI + a "crown-resistant below 120 mph" UI line are attributed to Scott & Reinhardt / Van Wagner without disclosing the surface-ROS proxy.** *(both audits)*
`analyze_fuels.py:476,460`; `wildfire.js:766`. Contingent on #1: until fixed, remove the false-safety line and reserve the S&R attribution for I₀/`R'active`/TI (which are correct). Codex adds: also add a **conifer applicability mask** (the derived-fuel path already knows evergreen fraction) so hardwood cells with LANDFIRE CBH/CBD aren't flagged as crowning.

**11. [P2 · Codex] `exposure` (open/shaded) is accepted then ignored** in `dead_moisture` — open grass and shaded litter get identical moisture. Implement or remove from the interface.

**12. Disclosure lines in `result.notes`:** label the footprint "unsuppressed potential"; warn when `duration_min` ≫ one burning period (constant peak-of-day weather over-predicts multi-hour spread); note "wind is spatially uniform."

### CONSIDER (latent / robustness)

- **`_is_nwi_open_water` operator-precedence bug** (`hydro_fire.py:418`) — collapses to an over-broad `"OW" in text` substring test; "SHALLOW/MEADOW/WILLOW" would promote a wetland damper to a barrier. Correct on current data; match the `OW` **code token**. *(workflow)*
- **Latent CRS mis-detection** for a WGS84-only vector with no `atlas/local/` variant (`hydro_fire.py:214`) — add an explicit scene-local marker (don't switch to SRS-based reprojection). Not live today. *(both)*
- `arrival_time` 8-neighbor Dijkstra has a bounded ~8 % octile bias (accepted for Tier-1; tighten docstring or add 16-neighbor moves). GSI woody floor hardcoded 60 % (parameterize by NFDRS climate class before promoting GSI). Thomas flame exponent 0.67→0.667 (dead code). Ignition bounds use cell-center extent. Crosswalk never emits TU5 (correct for humid Adirondacks; gate for national dry-climate reuse).

---

## What's CORRECT (clean bill — verified line-by-line)

So you know the coverage you can trust:
- **Rothermel surface spread & reaction intensity** vs RMRS-GTR-371 — A, Γ_max, Γ′, two-category I_R, moisture damping (1−2.59r+5.11r²−3.52r³), mineral damping, ξ, ε=exp(−138/σ), Q_ig=250+1116M, live Mx (Albini), C/B/E wind & φ_s slope coefficients, residence 384/σ′, English-internal → ×0.3048. Net-load `g_ij` vs characteristic-moisture `f_ij` weightings correctly **separated** (a common bug, avoided).
- **Every unit conversion** — t/ac→kg/m², 1/ft→1/m, IT-BTU/lb→kJ/kg (2.326), BTU/ft²/min→kW/m² (0.18927), mph→m/min (26.8224), Byram ROS→m/s. No factor wrong, double-applied, or mixing % with fraction. LANDFIRE canopy decode (CBH÷10 m, CBD÷100 kg/m³) exact.
- **Wind/slope/ellipse core formulas**, WAF anchors (0.40/0.10 applied once), Anderson-1983 L/B, focus-polar ROS reducing to head/back correctly.
- **Crown initiation & TI** — I₀=(0.010·CBH·(460+25.9·FMC))^1.5, R'active=3.0/CBD, TI inversion — all exact.
- **Fuel-moisture suite** (7 functions) — Simard EMC, time-lag dead (days→hours consistent), Jolly GSI, CFFDRS foliar moisture — coefficient-for-coefficient; no days/hours or %/fraction errors.
- **FBFM40 table** — all 40 models diffed against GTR-153 Table 7, **zero mismatches**; NB/GR/GS/SH cells never overwritten by the crosswalk.
- **Byram/MTT core** — ROS≤0 = genuine barriers; Byram validated <0.1 % vs BehavePlus; L=0.0775·I^0.46.
- **Hydrology wiring core** — moisture blend only ever **raises** moisture (`np.maximum`), combined by **max**; wet targets sound vs FBFM40 Mx; drought scaling applied to uplift only; grid units consistent. *(The defects above are barrier-policy layered on this sound core.)*

---

## Missing factors (ranked for an Adirondack parcel screen)

**Worth adding:** (1) ember spotting [#5, high]; (2) crown-fire spread acceleration [#9, med]; (3) sub-cell fuel breaks / defensible space — burn `building_footprints`/`roads` into the fuelscape as NB at DEM resolution [med, the top homeowner mitigation the model can't currently see]; (4) a burning-period cap/warning for long runs [low]; (5) duff/ground smoldering [low, region-specific].

**Defensibly out of scope (name as blind spots, don't build):** terrain-modified wind (WindNinja), atmospheric stability / Haines / plume-dominated fire, steep-slope correction, suppression (keep as conservative "unsuppressed potential"), site-specific fuel-bed depth tuning.

---

*Bottom line: the engine's physics is faithful to the canonical literature (RMRS-GTR-371, GTR-153, RP-29, x77-004, GTR-266, Byram 1959, Simard 1968, CFFDRS). Fix the crown active/CI spread proxy, the two ellipse-geometry bugs, the wind-direction label, add a spotting ring + defensible-space burn-in, and correct the hydrology over-containment — and this is a defensible, honestly-scoped, ±class parcel screen.*
