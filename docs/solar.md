# Solar siting — model and interfaces

> Status: implemented as a planning-grade Simulation tab and MCP surface.

## What this is

VEIL can now plan fixed solar-panel sites from the same ingredients used by
astronomy and viewshed:

- sun geometry from the local sky/astronomy stack;
- terrain/canopy horizon profiles from `twin_viewshed`;
- vegetation/tree crown footprints from the VEIL vegetation inventory;
- Daymet all-sky shortwave normals, when `data/climate/daymet_daily.csv` exists;
- fixed-panel plane-of-array radiation and PVWatts-style kWh/kWdc estimates.

The output is a site-planning screen, not a bankable engineering report. Terrain
horizon is the strongest signal. The default solar surface is canopy/as-is:
recommended sites must have an open panel footprint when vegetation inventory
data exists, and terrain/canopy horizons are used for shade. `bare_earth` is a
cleared/no-tree scenario. Cloud loss is climatological and uses Daymet daily
all-sky shortwave; twins without climate forcing use a clear-sky fallback and
say so.

## Model

`scripts/twin_solar.py` is the core. It computes a clear-sky hourly shape,
scales it to Daymet monthly all-sky radiation, splits GHI into diffuse/direct
with an Erbs-style diffuse fraction, and transposes to a fixed tilted plane with:

- direct beam killed when sun altitude is below the local horizon azimuth;
- diffuse irradiance reduced by sky-view fraction;
- isotropic sky diffuse plus ground-reflected POA;
- PVWatts-style system losses, simple module-temperature derate, and kWh/kWdc.

`scripts/analyze_solar.py` samples a bounded AOI lattice and writes ordinary
viewer drape layers under `data/solar/`:

- `solar_pv_annual` — annual PV yield, kWh/kWdc/yr;
- `solar_poa_annual` — annual panel radiation, kWh/m2/yr;
- `solar_winter_poa` — November-February panel radiation, kWh/m2;
- `solar_shade_loss` — annual terrain/canopy horizon loss, percent;
- `solar_cloud_loss` — clear-sky loss inferred from Daymet, percent.
- `solar_vegetation_clearance` — nearest crown clearance around the assumed
  panel footprint, metres; negative means clearing is required.

Vegetation clearance uses `vegetation/tree_instances.json` and
`vegetation/shrub_points.json` when present, falling back to store tree/shrub
entities for older or test twins. Each candidate gets a system-size-dependent
panel footprint radius plus maintenance clearance. A recommended best site is
excluded if that footprint intersects a tree/shrub crown taller than 1.5 m.
`solar_at` still returns irradiance for blocked proposed sites, but its
`vegetation` block says `installable=false` and lists the nearest conflicts.

## Interfaces

Simulation tab:

- **Solar layers** toggles the generated heatmaps.
- **Analyze solar layers** runs `analyze_solar.py` through `/api/solar/analyze`.
- **Best sites** shows two separate top-three lists: as-is vegetation-aware
  sites with clear panel footprints, and bare-earth/cleared-potential sites
  ranked without vegetation as a physical blocker.
- **Pick panel site** calls `/api/solar/site`, computes a point-specific horizon,
  and reports tilt, azimuth, annual PV, POA, winter POA, shade loss, cloud loss,
  and vegetation footprint clearance.

MCP tools:

- `solar_at(point, tilt_deg?, azimuth_deg?, system_kw?, surface?, objective?)`
- `solar_profile(point, surface?, system_kw?)`
- `compare_solar_sites(points, surface?, system_kw?, objective?)`
- `recommend_solar_sites(region?, objective?, count?, surface?, system_kw?, demonstrate?)`

Generic `recommend_sites("solar panel ...")` routes to `recommend_solar_sites`.
With `demonstrate=true`, GAIA draws numbered markers on the live map.

## Validation posture

The implementation intentionally stays local/offline. The current state of the
art for production modeling is represented by pvlib/SAM/PVWatts-style
decomposition, transposition, horizon shading, and PV system derates; NSRDB and
PVGIS are the preferred external resource oracles when a user wants a stronger
site report. Relevant references:

- pvlib horizon-shading example: https://pvlib-python.readthedocs.io/en/stable/gallery/shading/plot_simple_irradiance_adjustment_for_horizon_shading.html
- NREL PVWatts technical reference: https://www.osti.gov/biblio/1158421
- NREL SAM photovoltaic model reference: https://www.nrel.gov/docs/fy18osti/67399.pdf
- NSRDB / Physical Solar Model overview: https://www.sciencedirect.com/science/article/pii/S136403211830087X
- PVGIS API and solar/PV service docs: https://joint-research-centre.ec.europa.eu/photovoltaic-geographical-information-system-pvgis/using-pvgis-5/api-non-interactive-service_en
