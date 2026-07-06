# Evapotranspiration Modeling for the VEIL Digital-Twin Engine

Date: 2026-07-05  
Target repos: `/home/zy/dev/veil` and `/home/zy/dev/snow-road-twin`  
Target twin: Snow Road / Adirondack forest parcel, about 27.25 ha, ungauged, no flux tower


> Design doc: research + defensibility + implementation plan for adding evapotranspiration (reference ET0 and actual ET) to VEIL. Companion source tables: `scripts/`—see the ET fetchers and `derive_et0_daily.py` / `et_water_balance.py`. Generated with web-grounded research, cross-checked across independent research passes.

## Bottom Line

VEIL can defensibly add evapotranspiration now, but not as a precision flux-tower product. With the current local Daymet record, SSURGO AWC, land-cover/canopy layers, and optical imagery, the defensible first product is a daily reference-ET plus root-zone soil-water-balance AET model that closes the long-term and event-scale water balance:

`P = ET + Q + delta S + recharge/residual`

Full FAO-56 / ASCE Penman-Monteith reference ET is not defensible from the current purely local file because wind and humidity are missing. Adding Daymet `vp` fixes vapor pressure, and gridMET or ERA5-Land can supply wind and full reference ET. Surface-energy-balance ET is not defensible from the staged Landsat archive as-is because thermal/LST assets were not staged. The fix is straightforward: the same Microsoft Planetary Computer `landsat-c2-l2` STAC items expose `lwir11` for Landsat 8/9 ST_B10, `lwir` for Landsat 4-7 ST_B6, `qa`/ST_QA, emissivity and atmospheric thermal support assets.

For this humid northern forest, the first implementation should prioritize mass balance and uncertainty honesty over detailed instantaneous energy balance. A distributed NDVI-Kc layer can be useful as a relative spatial modifier, but it should not be sold as independently validated AET.

## 1. Scientific Literature Review: PET / Reference ET and AET / Actual ET

### 1.1 Reference / Potential ET from Meteorology

#### FAO-56 Penman-Monteith

Governing daily equation, FAO-56 grass reference ETo (Allen et al., 1998, FAO Irrigation and Drainage Paper 56, https://www.fao.org/4/X0490E/x0490e00.htm):

```text
ETo = [0.408 Delta (Rn - G) + gamma (900 / (T + 273)) u2 (es - ea)]
      / [Delta + gamma (1 + 0.34 u2)]
```

Inputs: net radiation `Rn`, soil heat flux `G` (daily often near 0), mean air temperature `T`, wind speed at 2 m `u2`, saturation vapor pressure `es`, actual vapor pressure `ea`, vapor-pressure deficit `es - ea`, slope of saturation vapor pressure curve `Delta`, psychrometric constant `gamma`, elevation/pressure, latitude/day-of-year or radiation. Temporal step: hourly, daily, 10-day, monthly; FAO-56 gives daily/monthly details.

Strengths: reference standard; physically combines available energy and aerodynamic demand; compatible with FAO crop coefficients and ASCE standardized ETo/ETr.

Limits: cannot be computed honestly without humidity and wind. FAO-56 includes reduced-data procedures: if humidity data are missing, estimate actual vapor pressure from minimum temperature, preferably with local calibration; if wind is missing, use regional/default wind only when wind is normally light-to-moderate and include extra uncertainty; if radiation is missing, estimate from sunshine or temperature range. Those are fallback estimates, not equivalent to measured inputs.

Reported accuracy: FAO-56 PM is usually treated as the benchmark against lysimeters when weather is complete. ASCE standardized reference ET validation found standardized PM equations gave consistent reference ET across climates when input data are quality controlled (ASCE-EWRI, 2005, standardized reference ET manual; ASCE overview: https://ascelibrary.org/doi/book/10.1061/9780784408056). In missing-data mode, accuracy degrades mainly with wind and vapor-pressure-deficit errors; this is exactly Snow Road's current gap.

Defensibility with current data: not from local Daymet as staged. Daymet has no wind in the current CSV and does not have wind as a single-pixel variable.

#### ASCE-EWRI Standardized Penman-Monteith

Governing form (ASCE-EWRI, 2005):

```text
ETsz = [0.408 Delta (Rn - G) + gamma (Cn / (T + 273)) u2 (es - ea)]
       / [Delta + gamma (1 + Cd u2)]
```

Inputs: same as FAO-56, with constants `Cn`, `Cd` set by reference crop and timestep: short grass ETo or tall alfalfa ETr, hourly or daily. Temporal step: hourly or daily.

Strengths: US irrigation standard; gridMET `pet` and `etr` are ASCE PM-derived reference ET products according to gridMET documentation (https://www.climatologylab.org/gridmet.html).

Limits: same missing wind/humidity problem. At Snow Road, gridMET can provide 4 km `pet`/`etr`, wind, humidity and VPD, but then the claim is "gridded regional reference ET", not "purely local measured ET."

Reported accuracy: ASCE PM is the reference method; error is dominated by forcing quality. gridMET itself warns that wind and solar radiation are inherited/interpolated from coarser NLDAS/NARR-scale information and will miss <4 km microclimates (gridMET docs).

#### Priestley-Taylor

Governing equation (Priestley and Taylor, 1972):

```text
ET_PT = alpha [Delta / (Delta + gamma)] (Rn - G) / lambda
```

Typical `alpha` is about 1.26 for wet, advective-free conditions. Inputs: net radiation or radiation-derived available energy, temperature/elevation for `Delta`/`gamma`; no wind or humidity in the original equilibrium formulation. Temporal step: daily to monthly.

Strengths: robust for humid/wet surfaces when water stress is low and advection is modest; useful when wind/humidity are missing.

Limits: not a general AET method unless stress scalars are added. Can overestimate dry/stressed surfaces and under-handle aerodynamic demand. Forest canopies with interception and snow have additional terms.

Reported accuracy: PT-JPL and PT-family variants are widely used in remote sensing; OpenET describes PT-JPL as a simplified model and notes true equilibrium conditions rarely hold, so alpha varies by environment (OpenET methods, https://etdata.org/methods/). For humid Snow Road, PT is a defensible reference/PET comparison, not a replacement for soil-water AET.

#### Hargreaves-Samani

Governing equation (Hargreaves and Samani, 1985, DOI `10.13031/2013.26773`):

```text
ETo = 0.0023 Ra (Tmean + 17.8) sqrt(Tmax - Tmin)
```

Inputs: extraterrestrial radiation `Ra`, mean/min/max air temperature. Temporal step: daily/monthly.

Strengths: needs only temperature and latitude/date; works as a reduced-data estimate.

Limits: empirical; temperature range is a proxy for radiation/cloudiness and humidity/advection. It often needs regional calibration, especially in humid forests, mountains and coastal/cloudy sites.

Reported accuracy: in humid climates, Hargreaves is commonly worse than radiation/PM methods unless calibrated. Oudin et al. (2005, DOI `10.1016/j.jhydrol.2004.08.026`) found simple temperature-based PET could perform surprisingly well for rainfall-runoff modeling across many catchments, but that metric is streamflow-model performance, not lysimeter-grade reference ET accuracy.

#### Reduced-Data Temperature/Radiation Family

These methods are useful because the current Daymet record has daily `tmin`, `tmax`, `srad` and `dayl`, but no wind/humidity.

| Method | Concise equation | Exact inputs | Step | Strengths | Limits and Snow Road implication | Accuracy framing |
|---|---|---|---|---|---|---|
| Oudin | `PET = Re / (lambda rho) * (T + 5) / 100` for `T > -5 deg C`, else 0 | mean temperature, extraterrestrial radiation, latitude/date | daily | Very parsimonious hydrologic-model PET | Ignores humidity/wind/canopy; good hydrologic forcing candidate, not physical reference ET | Oudin et al. (2005, DOI `10.1016/j.jhydrol.2004.08.026`) reported good rainfall-runoff performance with limited inputs |
| Makkink | `ETo = c (Delta/(Delta+gamma)) Rs/lambda` | solar radiation, temperature/elevation | daily/monthly | Good when measured shortwave exists | No aerodynamic term; empirical coefficient region dependent | Often competitive in humid/radiation-limited conditions; calibrate against gridMET/PM where available |
| Jensen-Haise | `ETo = Rs (a T + b)` | solar radiation, air temperature | daily/monthly | Simple radiation-temperature method | Originally arid irrigated settings; can bias in humid forests | Use only as comparison, not primary |
| Turc | `ET0 = 0.013 T/(T+15) (Rs + 50)` with humidity correction in dry air | temperature, solar radiation, optional RH | 10-day/monthly/daily variants | Good reduced-data humid-climate method | Humidity correction unavailable unless Daymet `vp` added | Useful after `vp` addition |
| Hamon | daylength and saturation vapor density temperature relation | temperature, daylength | daily/monthly | Very low data demand | Too temperature-only; weak radiation/cloud sensitivity | Fallback only |
| Thornthwaite | heat-index monthly PET from mean temperature and daylength | monthly temperature, latitude/daylength | monthly | Historical climatology/Budyko-style context | Poor event/daily physics; ignores radiation, wind, humidity | Do not use for daily scenario antecedent moisture |

Recommended for VEIL Tier 1: compute a small ensemble: FAO-56 reduced-data ETo, Priestley-Taylor, Oudin, and Hargreaves-Samani from local Daymet. Treat gridMET ASCE `pet`/`etr` as an external reference when available. Use the spread as uncertainty.

### 1.2 AET via Soil-Water Balance and Crop Coefficients

#### FAO-56 single crop coefficient

Governing equations:

```text
ETc = Kc ETo
ETc_adj = Ks Kc ETo
Dr_i = Dr_{i-1} - P_eff - I - CR + ETc_adj + DP + RO
TAW = 1000 (theta_FC - theta_WP) Zr
RAW = p TAW
Ks = (TAW - Dr) / (TAW - RAW), bounded 0..1 when Dr > RAW
```

Inputs: reference ETo, land-cover/crop coefficient `Kc`, root depth `Zr`, soil available water content or TAW, depletion fraction `p`, precipitation/effective precipitation, runoff/deep percolation logic, initial root-zone depletion. Temporal step: daily.

Strengths: directly closes a daily water balance; compatible with SSURGO AWC and existing hydrology scenario antecedent moisture. Good for VEIL because the missing hydrologic term is a daily loss and soil-moisture state, not instantaneous eddy covariance.

Limits: crop coefficients for mixed northern hardwood-conifer forest are not as clean as agricultural Kc tables. Forest interception, snow sublimation, dormant-season transpiration and frozen soil need explicit modifiers. Root depth and plant-available water are uncertain.

Reported accuracy: FAO-56 Kc water balances are operationally successful in irrigated systems but depend on local Kc and soil parameters. For ungauged forests, expect annual AET uncertainty in the 15-30% class without flux or catchment validation; report relative timing and soil-moisture state as more defensible than absolute flux.

#### FAO-56 dual crop coefficient

Governing equations:

```text
ETc = (Kcb Ks + Ke) ETo
Kc = Kcb Ks + Ke
```

Inputs: basal crop coefficient `Kcb`, soil evaporation coefficient `Ke`, fraction exposed/wetted soil, precipitation/irrigation, surface evaporation layer depletion, root-zone depletion, ETo. Temporal step: daily.

Strengths: separates transpiration from soil evaporation after rain/snowmelt. This matters because Snow Road is forested and rainy/snowy; using a single Kc would blur canopy/root-zone stress and wet-soil evaporation.

Limits: local `Kcb` for mixed forest requires literature defaults or calibration. Soil evaporation under closed canopy is small in summer but can matter during leaf-off, snowmelt, and disturbed/road areas.

VEIL fit: good Tier 2 model if implemented with conservative defaults: forest `Kcb` seasonal curve from LAI/NDVI/canopy class, `Ke` rain/snowmelt pulses, and stress `Ks` from SSURGO TAW.

#### Budyko long-term AET

Water-energy balance:

```text
P = AET + Q + delta S
AI = PET / P
AET/P = f(AI)
```

Common forms:

```text
Turc-Pike: AET/P = 1 / (1 + (P/PET)^n)^(1/n)
Fu/Choudhury-Yang: AET/P = 1 + AI - (1 + AI^omega)^(1/omega)
```

Inputs: long-term mean precipitation and PET/ETo; optional fitted catchment parameter `omega`. Temporal step: multi-year climatology.

Strengths: sanity check for ungauged catchment annual AET and runoff ratio. It is not a daily model and not a pixel model.

Limits: assumes long-term storage change is near zero and integrates catchment vegetation/soil/topography into one curve. For a 27 ha ungauged parcel, it checks plausibility but cannot validate D8 event routing.

Snow Road position: humid NE forest is energy-limited to seasonally co-limited, with annual P about 1150-1480 mm and expected AET roughly 450-700 mm/yr. That implies ET/P about 0.35-0.55 and leaves large runoff/recharge. If a VEIL model returns annual ET/P near 0.75 for this site, it is probably too wet/high unless external evidence supports it.

#### Complementary relationship / GLEAM family

Bouchet's complementary relationship links actual evaporation, potential evaporation and wet-environment evaporation through atmospheric feedback. Advection-aridity and Granger-Gray methods estimate AET from meteorological variables by assuming potential ET increases as actual ET falls in drying landscapes. GLEAM modernizes this with satellite soil moisture, vegetation observations, Penman potential evaporation and stress factors learned from eddy covariance/sapflow (GLEAM method, https://www.gleam.eu/).

Inputs: radiation, temperature, humidity/VPD, wind, precipitation, soil moisture/vegetation for modern variants. Temporal step: daily to monthly.

Strengths: useful for regional/global AET context and independent estimates where flux towers are absent.

Limits: not parcel scale. GLEAM v4 is 0.1 deg and 1980-2024, excellent for climate context but not for a 27 ha layer.

### 1.3 Remote-Sensing AET: Optical / Vegetation-Index, Thermal-Free

#### Kc-NDVI / reflectance-based crop coefficient

Governing form:

```text
Kcb = a NDVI + b              or              Kcb = f(NDVI, fc, LAI)
AET = (Kcb Ks + Ke) ETo
```

Inputs: surface reflectance red/NIR for NDVI or EVI; cloud/snow masking; ETo; soil water stress or assumed unstressed vegetation; land-cover-specific coefficients. Temporal step: satellite overpass, then daily interpolation using ETo.

Strengths: uses the already staged Landsat and Sentinel-2 optical archive; gives spatial phenology/canopy modifiers at 10-30 m; thermal not required.

Limits: NDVI saturates in closed forests. It senses greenness/canopy density, not stomatal closure or wet canopy evaporation. In this parcel, optical NDVI-Kc is defensible as a spatial/seasonal modifier on the soil-water-balance model, not as stand-alone AET.

#### SIMS

SIMS is a reflectance-based crop-coefficient model. OpenET states SIMS relies on surface reflectance and crop type to compute ET as a function of canopy density, and that it is currently implemented for croplands; it also notes SIMS added FAO-56-style soil evaporation after precipitation to reduce winter/wet-period low bias (OpenET methods, https://etdata.org/methods/).

Snow Road implication: not a direct fit because the site is mixed forest, not cropland. The concept, not the crop-specific implementation, is useful.

#### PT-JPL

PT-JPL uses Priestley-Taylor potential ET constrained by vegetation and moisture scalars from remote sensing and meteorology (Fisher et al., 2008, DOI `10.1016/j.rse.2007.08.025`). OpenET's PT-JPL implementation uses Landsat surface reflectance and thermal radiation for radiation and canopy/moisture variables, plus gridded weather/reference ET (OpenET methods).

Snow Road implication: locally staged optical-only data are insufficient for the full OpenET PT-JPL implementation if thermal/radiation terms are required, but a simplified optical/radiation PT stress model could be a research layer.

#### MOD16

MOD16 uses Penman-Monteith logic with MODIS vegetation dynamics, albedo, land cover and meteorological reanalysis inputs. The NASA product page says MOD16A2 v6.1 provides ET, latent heat, PET and potential LE at 500 m 8-day composites (DOI `10.5067/MODIS/MOD16A2.061`, https://www.earthdata.nasa.gov/data/catalog/lpcloud-mod16a2-061).

Snow Road implication: one or a few 500 m pixels over the AOI. Useful seasonal context, not a primary parcel model.

#### OpenET vegetation-index members

OpenET uses an ensemble including DisALEXI, eeMETRIC, geeSEBAL, PT-JPL, SIMS and SSEBop. It states SIMS is reflectance/crop-coefficient based and that most other models use full or simplified surface energy balance with Landsat optical and thermal inputs. It reports 30 m field-scale ET and public API access with quotas (https://etdata.org/api/, https://etdata.org/methods/).

Snow Road implication: OpenET is valuable as an external check in the US, but OpenET's own known-issues page warns monthly ensemble ET is biased high in evergreen and mixed forests by about 1.05-1.35, with ensemble bias factor about 1.20-1.25, and recommends a 0.80-0.85 forest multiplier when no water-balance information is available (https://etdata.org/accuracy-known-issues/). That makes it a validation reference with caveats, not ground truth.

### 1.4 Remote-Sensing AET: Surface Energy Balance, Thermal Required

Surface energy balance starts from:

```text
Rn - G = H + LE
ET = LE / lambda
```

Inputs common to SEBAL, METRIC, SSEBop, ALEXI/DisALEXI and SEBS: land surface temperature (LST), surface reflectance/albedo, vegetation index or LAI, emissivity, elevation, net radiation, weather/reference ET, and quality masks. Many methods need hot/cold anchor pixels or regional calibration.

| Family | Core idea | Why thermal is essential | Small-AOI limits |
|---|---|---|---|
| SEBAL | Solve energy balance; calibrate sensible heat with hot/cold endmembers (Bastiaanssen et al., 1998, DOI `10.1016/S0924-2716(98)00028-7`) | LST controls sensible heat gradient and evaporative fraction | A 27 ha forest parcel may not contain valid wet/dry anchor pixels; forest shadows/clouds/snow complicate |
| METRIC | SEBAL variant internally calibrated to alfalfa reference ET (Allen et al., 2007, DOI `10.1061/(ASCE)0733-9437(2007)133:4(380)`) | LST plus reference ET calibrates H/LE | Anchor selection and advective assumptions are scene/regional, not parcel-only |
| SSEBop | Operational simplified surface energy balance using LST relative to cold reference / dT (Senay et al., 2013, DOI `10.1002/wrcr.20371`) | ET fraction is derived from thermal contrast | Good broad product; local implementation still needs thermal and calibrated parameters |
| ALEXI/DisALEXI | Two-source energy balance from morning LST rise, disaggregated to Landsat | Thermal temporal signal drives H/LE partition | Needs GOES/regional context and Landsat thermal; not a simple local-only script |
| SEBS | Energy balance with roughness and atmospheric stability (Su, 2002, DOI `10.5194/hess-6-85-2002`) | LST is the surface-air thermal contrast basis | Meteorology and roughness uncertainty dominate over small forest AOI |

With current staged Landsat assets (`blue`, `green`, `red`, `nir08`, `swir16` only), these are not runnable. Adding Landsat Collection 2 LST assets makes research prototypes possible, but still not eddy-covariance-grade AET.

### 1.5 Cold-Region / Forest Specifics

#### Canopy interception evaporation

Gash/Rutter-type interception models treat rainfall or snowfall captured by canopy storage as evaporating before reaching soil:

```text
P = throughfall + stemflow + interception loss
I_loss = f(canopy storage capacity, canopy cover, storm duration, evaporation rate)
```

Inputs: precipitation phase/intensity, canopy cover/LAI, canopy storage capacity, meteorology during wet canopy periods. Step: event or daily.

Snow Road: forest interception is not optional. It removes water before it reaches SSURGO root-zone storage or SCS-CN runoff. A minimal daily model can use canopy cover from LANDFIRE/LiDAR and seasonally varying storage capacity, with higher uncertainty in snow.

#### Snow sublimation and dormant-season ET

Snow sublimation depends on radiation, humidity/VPD, wind, temperature and exposed snow/canopy snow. Daymet SWE gives snow state but current local data lack wind/humidity. With Daymet `vp` and gridMET/ERA5 wind, VEIL can approximate sublimation; without wind it should keep a conservative dormant-season ET/sublimation term and expose uncertainty.

#### Energy-limited humid NE forests

The Adirondack site is humid and snow-affected. Annual precipitation is high enough that annual ET is usually energy-limited or seasonally co-limited, not persistently water-limited. Typical northeastern forest annual ET is commonly on the order of 450-700 mm/yr, with ET/P about 0.35-0.55. Hubbard Brook-style water balances are a useful mental check: high precipitation plus large streamflow/recharge residual; not an arid catchment where ET consumes most P.

VEIL implication: Budyko and TerraClimate/MOD16/OpenET checks should reject ET/P values inconsistent with humid forest water balance unless there is strong evidence.

### 1.6 Validation, Uncertainty and Current SOTA (2020-2026)

Validation hierarchy for this twin:

1. Best: on-site eddy covariance, lysimeter, sapflow, soil moisture and streamflow. None exist.
2. Good catchment closure: stream gauge + precipitation + storage. Snow Road is ungauged.
3. Nearby flux towers / AmeriFlux / FLUXNET ecological analogs. Useful, not collocated.
4. Gridded product intercomparison: gridMET/Daymet/ERA5 reference ET, TerraClimate AET/PET, MOD16, GLEAM, OpenET. Useful context, not ground truth.
5. Internal hydrologic consistency: annual P, modeled ET, runoff/recharge residual, soil moisture seasonality, wetness patterns and observed wetlands/seeps.

OpenET's current published accuracy page reports cropland ensemble performance against 151 flux towers and four weighing lysimeters: water-year RMSE 121.87 mm (12.3%), growing-season RMSE 93.79 mm (15.5%), monthly RMSE 20.44 mm (22.4%), and daily overpass-day RMSE 1.09 mm (31.1%). It also warns natural land-cover accuracy is more variable and forests have high bias (https://etdata.org/accuracy-known-issues/). That is the right standard for honest reporting: separate cropland performance from forest performance.

Machine-learning products and emulators such as FLUXCOM, FluxSat, GLEAM4 stress learning and OpenET latency ML improvements are valuable regional/global context. They do not remove the need for local validation. For VEIL reports, use phrases like:

- "daily ET estimate from a soil-water-balance model, uncertainty likely +/-20-35% annually without local calibration"
- "relative soil moisture and antecedent wetness more reliable than absolute ET"
- "energy-balance ET candidate, not validated parcel truth"

## 2. Defensibility with Current Data

| Candidate capability | Verdict | Specific reason |
|---|---|---|
| (a) Reference ET from local Daymet using temperature/radiation methods; gridMET `pet`/`etr` cross-check | Defensible with caveats | Daymet has `tmin`, `tmax`, `srad`, `dayl`; can compute Oudin, Hargreaves-Samani, Priestley-Taylor and FAO-56 reduced-data ETo. It lacks wind and current `vp`, so call it reduced-data reference ET. gridMET ASCE `pet`/`etr` can cross-check at 4 km. |
| (b) Full Penman-Monteith from purely local data | Not defensible | Current local Daymet CSV has no wind and no humidity. Adding Daymet `vp` supplies vapor pressure but still no wind. |
| (c) FAO-56 soil-water-balance AET integrated as missing ET term in SCS-CN + D8 hydrology, with Budyko long-term check | Defensible with caveats | SSURGO AWC/HSG/restrictive depths, Daymet P/SWE/temp/radiation, terrain routing and canopy/land cover exist. Need explicit uncertainty for Kc/root depth/interception/sublimation. Budyko is a plausibility check, not validation. |
| (d) NDVI-Kc spatial/temporal AET from local optical Landsat + Sentinel archive | Defensible with caveats | Optical archive supports NDVI/phenology. Closed forest NDVI saturates and optical data do not directly sense water stress. Use as a spatial/seasonal modifier on water balance, not independent AET truth. |
| (e) Energy-balance ET from local Landsat as staged | Not defensible | Staged assets omit thermal/LST. SEBAL/METRIC/SSEBop/SEBS/ALEXI need LST. |

## 3. Can We Fill the Gaps with Public Data?

Yes. Both known gaps can be filled with public data. The full source comparison tables are also in `ET_SOURCES_TABLE.md`.

### 3.1 Gap A: Wind and Humidity

#### Daymet `vp`

Daymet's single-pixel tool lists `ALL DAYL PRCP SRAD SWE TMAX TMIN VP`, and its example API request includes `vp` (https://daymet.ornl.gov/single-pixel/). Adding `vp` to `VARS` in `packs/adirondack/fetch_climate_forcing.py` will provide vapor pressure for the same Daymet North America grid and period. It does not provide wind. It plugs humidity but not the aerodynamic term.

VEIL fit: trivial; same REST call, same CSV parser with one new column.

#### gridMET

gridMET is daily, about 4 km, CONUS from 1979 to yesterday, with primary variables including temperature, precipitation, shortwave radiation, wind velocity, max/min RH and specific humidity, and derived ASCE Penman-Monteith reference ET and VPD. It is available by direct NetCDF, THREDDS/OPeNDAP and USGS GDP/Zarr (https://www.climatologylab.org/gridmet.html). It is free and no-key for common download paths.

VEIL fit: best US-national gap-fill. Add a point or clipped-grid fetcher in a US-national pack. For Snow Road, one 4 km pixel is weather context; report "gridMET-forced PM," not "onsite PM."

#### NLDAS-2

NLDAS-2 forcing is hourly, 1/8 deg, 1979-present. File A includes 10 m u/v wind, 2 m air temperature, 2 m specific humidity, surface pressure, longwave, bias-corrected shortwave, precipitation and NARR potential evaporation (https://ldas.gsfc.nasa.gov/nldas/v2/forcing). Access is through NASA GES DISC/GRIB/OPeNDAP, commonly with Earthdata Login.

VEIL fit: high-quality hourly forcing and PM benchmark, but heavier than gridMET and coarser.

#### PRISM

PRISM daily AN products include `tdmean`, `vpdmin`, `vpdmax`, `ppt`, `tmin`, `tmax`, `tmean`, with daily data starting 1981 and ending yesterday for CONUS products; units include VPD in hPa and temperature in deg C (PRISM datasets PDF, https://www.prism.oregonstate.edu/documents/PRISM_datasets.pdf). It has no wind.

VEIL fit: humidity/VPD and precip/temp cross-check, not full PM by itself.

#### RTMA / HRRR

RTMA products include CONUS 2.5 km hourly analyses and regional products for Alaska, Hawaii, Puerto Rico and Guam, accessible in GRIB2 from NCEP/NOMADS (https://www.nco.ncep.noaa.gov/pmb/products/rtma/). HRRR is NOAA's 3 km hourly updated model, archived since 2014 in public cloud buckets (https://rapidrefresh.noaa.gov/hrrr/, https://registry.opendata.aws/noaa-hrrr-pds/).

VEIL fit: operational/live simulation, not 45-year climatology.

#### Global: ERA5 / ERA5-Land

ERA5-Land is global land, hourly, 0.1 deg distribution grid, native about 9 km, 1950-present, CC-BY and CDS API access (https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land). It includes the wind/humidity/radiation/precipitation fields needed for PM and native `potential_evaporation`, `total_evaporation`, soil moisture and water-balance terms. ERA5 single levels is global hourly 0.25 deg from 1940-present (https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels).

VEIL fit: best global fallback for NATO/non-US packs. Blockers: CDS account, accepted license, queued requests, GRIB semantics.

#### MERRA-2

MERRA-2 begins in 1980, global, about 50 km latitudinal resolution, and includes meteorological forcing fields (NASA GMAO, https://gmao.gsfc.nasa.gov/gmao-products/merra-2/). It is too coarse for parcel ET but useful as a global cross-check.

#### NOAA ISD stations

NOAA ISD has global hourly/synoptic station observations with wind, temperature, dew point, pressure, precipitation and other elements, from 1901-present but station-dependent; over 20,000 stations are available in Global Hourly and more than 14,000 active stations update daily (https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database).

VEIL fit: local QA/bias check when nearby stations exist; not a gridded complete forcing source.

### 3.2 Gap B: Thermal LST

#### Landsat Collection 2 Level-2 on Microsoft Planetary Computer

The same `landsat-c2-l2` STAC collection already used by `scripts/stage_parcel_timeseries.py` exposes surface temperature assets:

- `lwir11`: Landsat 8/9 ST_B10, common name `lwir11`, scale `0.00341802`, offset `149.0`, unit kelvin, delivered as 30 m raster, thermal source gsd listed as 100 m.
- `lwir`: Landsat 4-7 ST_B6, common name `lwir`, same scale/offset and unit.
- `qa`: Surface Temperature Quality Assessment Band ST_QA, unit kelvin with scale `0.01`.
- `emis`, `emsd`, `atran`, `trad`, `urad`, `drad`, `cdist`, `qa_pixel`, `qa_radsat`, plus reflectance bands.

Source: Planetary Computer STAC collection JSON, https://planetarycomputer.microsoft.com/api/stac/v1/collections/landsat-c2-l2.

Answer: yes, VEIL can get LST for free by adding thermal asset keys to the same existing staging search. This is the most important Gap B fix.

#### ECOSTRESS

ECOSTRESS land surface temperature/emissivity is about 70 m but irregular because it is on the ISS. It is useful for stress/diurnal snapshots but not a 45-year continuous baseline.

#### MODIS MOD11, VIIRS, Sentinel-3 SLSTR, ASTER

MODIS MOD11A2 provides 1 km 8-day global LST from 2000-present (DOI `10.5067/MODIS/MOD11A2.061`, https://www.earthdata.nasa.gov/data/catalog/lpcloud-mod11a2-061). VIIRS VNP21A1D provides global 1 km daily day LST from 2012-present, derived from native 750 m VIIRS (DOI `10.5067/VIIRS/VNP21A1D.002`, https://www.earthdata.nasa.gov/data/catalog/lpcloud-vnp21a1d-002). Sentinel-3 SLSTR is about 1 km thermal, global, useful context. ASTER AST_08 V003 was 90 m global surface kinetic temperature from 2000-03-04 to 2025-12-15 but is deprecated; NASA recommends V004 (V003 page: https://www.earthdata.nasa.gov/data/catalog/lpcloud-ast-08-003).

VEIL fit: all are context or opportunistic supplements. Landsat thermal is the only public thermal source that aligns naturally with the existing 30 m archive and parcel scale.

### 3.3 Ready-Made ET Products

OpenET: 30 m field-scale ET, daily/monthly/annual products, API with free quotas and account registration. Public docs say the Data Explorer gives a rolling 5-6 year archive and the API provides 25+ years in select regions; current methods/accuracy pages describe CONUS validation and six model ensemble but forest bias caveats (https://etdata.org/api/, https://etdata.org/methods/, https://etdata.org/accuracy-known-issues/). Role: validation/cross-check only for Snow Road forest.

USGS SSEBop: useful broad AET product and OpenET member, commonly 1 km operational products or 30 m within OpenET. Role: independent validation/coarse context.

TerraClimate: monthly global terrestrial climate/water-balance at about 4 km from 1950-present, with PET, AET, soil moisture, deficit, runoff, SWE, VPD and climate forcing. Access via THREDDS/OPeNDAP, NetCDF and GEE (https://www.climatologylab.org/terraclimate.html). Role: coarse context and Budyko/water-balance sanity check.

MODIS MOD16A2: 500 m 8-day global ET/PET/LE product (NASA Earthdata page above). Role: coarse seasonal context.

PML_V2: global partitioned ET product with transpiration, soil evaporation and interception. Role: coarse context and component comparison; verify latest release/license before production.

GLEAM: GLEAM4 provides global evaporation components, potential evaporation, soil moisture and stress at 0.1 deg and 1980-2024; it explicitly includes interception, sublimation and soil-moisture constraints (https://www.gleam.eu/). Role: global process benchmark, not parcel layer.

## 4. Generalizability of the Fetchers

### 4.1 US-National Pack

Best US stack:

1. Tier A: Daymet + `vp` for local daily temp/precip/radiation/SWE/vapor pressure; SSURGO/gSSURGO for AWC/HSG; gridMET for ASCE ETo/ETr, wind, RH/specific humidity, VPD and full PM inputs; Landsat C2 L2 thermal via Planetary Computer.
2. Tier B: PRISM VPD/dewpoint/precip/temp cross-check; NLDAS-2 hourly forcing if gridMET is insufficient or hourly physics matters.
3. Tier C: RTMA/HRRR for live/recent simulation mode, not historical climatology.

Coverage caveats:

- gridMET, PRISM and NLDAS are CONUS-focused; they do not fully solve Alaska, Hawaii, Puerto Rico, Guam or territories.
- Daymet covers North America and includes Hawaii/Puerto Rico depending product, making it broader than CONUS but still no wind.
- SSURGO/gSSURGO is US-specific; outside the US use SoilGrids/HWSD/national alternatives.
- Landsat/Sentinel public archives are global.

Low-friction US-national implementation: add Daymet `vp`; add gridMET point/clipped fetcher; add Landsat thermal asset staging. That gets humidity, wind/reference ET and LST without changing the viewer architecture.

### 4.2 Other Countries / Global

Global fallback stack for VEIL's `nato` or other packs:

1. Meteorology: ERA5-Land as Tier C global base for wind, humidity, radiation, precipitation, PET and soil moisture. ERA5 single-levels as fallback/benchmark.
2. Thermal: Landsat Collection 2 thermal globally; MODIS/VIIRS/Sentinel-3 coarse global LST; ECOSTRESS opportunistic.
3. Ready-made ET: TerraClimate, MOD16, GLEAM, PML_V2 as global context, not local truth.
4. Soils/AWC: ISRIC SoilGrids 250 m with pedotransfer functions for plant-available water; FAO HWSD as coarse fallback; EU ESDAC/European Soil Database or national soil agencies as Tier A/B where available.

Tiering:

- Tier A national best-available: national met agency gridded station analyses, national soil maps, national LiDAR/land cover, local flux/water-balance records when present.
- Tier B regional: EU Copernicus/EEA products, Canada national datasets, regional reanalyses.
- Tier C global fallback: ERA5-Land + Landsat/Sentinel/MODIS/VIIRS + SoilGrids + TerraClimate/GLEAM/MOD16.

Real blockers:

- Copernicus CDS requires an account/API key, accepted license and queued downloads.
- Google Earth Engine requires auth and project setup for OpenET/TerraClimate/PML workflows if using GEE.
- Some product licenses/terms restrict redistribution even when free; VEIL should store provenance and license fields.
- National datasets vary widely in API style, language and permission terms.

## 5. Recommended Implementation Plan

### Phase 1: Purely Local Reference ET

Inputs: existing Daymet daily CSV (`prcp`, `tmax`, `tmin`, `srad`, `dayl`, `swe`) plus georef latitude/elevation; optionally add Daymet `vp`.

Changes:

- In `/home/zy/dev/snow-road-twin/packs/adirondack/fetch_climate_forcing.py`, change `VARS` from `prcp,tmax,tmin,swe,srad,dayl` to `prcp,tmax,tmin,swe,srad,dayl,vp`.
- Update `parse_records()` to preserve the original Daymet column names and write `vp` to the canonical CSV.
- Add `scripts/derive_et0_daily.py` in VEIL engine, pack-neutral:
  - read `data/climate/daymet_daily.csv`
  - compute `tmean`, extraterrestrial radiation, estimated/net radiation where possible
  - compute Oudin, Hargreaves-Samani, Priestley-Taylor and FAO-56 reduced-data ETo
  - if `vp` exists, compute actual vapor pressure and VPD; still flag wind missing
  - write `data/et/et0_daily.csv` and `data/et/et0-summary.json`
  - register outputs in twin store through `twin_store.py`

Defensibility: defensible with caveats. Unlocks a local daily atmospheric demand record and climatology. Uncertainty: report method spread and gridMET comparison when available.

### Phase 2: AET Soil-Water Balance and Hydrology Coupling

Inputs: Phase 1 ET0, Daymet P/SWE, SSURGO AWC/HSG/restrictive layer, NLCD/LANDFIRE/canopy cover, terrain cell grid, D8 flow graph.

New module: `scripts/et_water_balance.py`.

Core state per soil/land-cover cell or coarser hydrologic response unit:

```text
TAW = AWC_profile_mm over effective root depth
Dr(t) = root-zone depletion
Ks(t) = water-stress coefficient
Kcb(t) = basal forest coefficient from season/canopy/NDVI
Ke(t) = soil evaporation pulse after rain/snowmelt
I(t) = canopy interception evaporation
AET(t) = (Kcb(t) Ks(t) + Ke(t)) ET0(t) + interception/sublimation term
recharge_residual(t) = P_eff - runoff - AET - delta root-zone storage
```

Integration with existing hydrology:

- `hydro_scenario.py` currently takes `--antecedent dry|normal|wet`. Replace or augment that with computed antecedent root-zone depletion and 5/14/30 day wetness indices from `data/et/soil_water_daily.csv`.
- Map those indices to CN AMC I/II/III continuously rather than only manual dry/normal/wet.
- Keep D8 routing and depression storage unchanged; ET affects how much water is available and how wet soils are before the event.
- Write `data/et/summary.json`, `data/et/local/aet_annual.grid.json` and optional monthly maps in the same raster catalog style as hydrology: image + grid + `bounds_local`.
- Add simulation-layer group `water_balance` or `et` so viewer can drape annual AET, deficit, recharge residual and antecedent moisture.

Defensibility: defensible with caveats. Unlocks the missing water-balance loss term and better event antecedent moisture. Uncertainty: absolute annual AET likely +/-20-35% without local validation; relative timing and wet/dry antecedent state more reliable.

### Phase 3: Gap-Fill Fetchers for Full PM and Thermal

US-national:

- Add `packs/us-national/fetch_gridmet_forcing.py`:
  - point or small AOI extraction for `pet`, `etr`, `vs`, `rmin`, `rmax`, `sph`, `vpd`, optionally `srad`, `tmmn`, `tmmx`, `pr`
  - write `data/climate/gridmet_daily.csv`
  - summarize correlation/bias against Daymet reduced ET0 and precipitation/temp
- Add optional `packs/global/fetch_era5_land_forcing.py`:
  - requires CDS credentials
  - AOI bounding box or nearest grid cell
  - variables: 2 m temperature/dewpoint, 10 m u/v wind, surface pressure, shortwave/longwave, total precipitation, potential evaporation, total evaporation, soil moisture

Thermal:

- Update `scripts/stage_parcel_timeseries.py` Landsat branch:
  - include `lwir11` for L8/L9 and `lwir` for L4/L5/L7 when present
  - include `qa`, `emis`, `emsd`, `atran`, `qa_pixel`
  - record scale/offset and thermal source gsd in the manifest
  - do not backfill all scenes by default; expose `--include-thermal` and `--thermal-only-missing`

Defensibility: full PM becomes defensible as gridded PM when gridMET/ERA5 provides wind/humidity. Energy-balance ET becomes research-defensible only after thermal restaging and QA.

### Phase 4: Distributed Remote-Sensing AET

Inputs: ET0, soil-water balance state, Landsat/Sentinel NDVI/EVI, land-cover/canopy, optional Landsat LST.

Two tracks:

1. `scripts/et_ndvi_kc.py`: optical Kcb modifier from Landsat/Sentinel phenology.
   - Use as a spatial modifier to the daily water balance.
   - Flag closed-canopy NDVI saturation.
2. `scripts/et_energy_balance_candidate.py`: only after Landsat thermal exists.
   - Prototype SSEBop/METRIC-style ET fraction using scene LST, NDVI, albedo, elevation and gridMET/ERA5 ETo.
   - Require AOI-plus-context window for anchor selection; do not choose hot/cold anchors only inside the 27 ha parcel.
   - Report as "candidate energy-balance ET"; compare to water-balance ET, TerraClimate, MOD16/GLEAM/OpenET.

Defensibility: NDVI-Kc is defensible with caveats as a relative modifier. Energy balance is not a production claim until thermal and QA exist and the AOI context is large enough.

### MCP, Store, Journal and Viewer Wiring

Store/journal:

- Use `twin_store.py` to register ET rasters/CSVs as layers with hashes.
- Keep time-series rasters staged under `data/et/` until VEIL chooses a raster time-series store convention, mirroring `data/timeseries_staging`.
- Add pipeline runs for `derive_et0_daily.py`, `et_water_balance.py`, and optional fetchers.

Viewer:

- Add ET/water-balance layers to the simulation catalog, or a new `data/et/et-layers.json` loaded alongside hydrology.
- In `public/simulation.js`, add a "Water balance" panel group under the existing Simulation window rather than a separate app.
- Show annual ET, current/selected antecedent moisture, deficit and recharge residual; avoid implying forecast precision.

MCP tools:

- `et_summary()`: annual/monthly ET0/AET, method spread, ET/P, Budyko/TerraClimate/OpenET checks, uncertainty statement.
- `et_at(point)`: sample AET/deficit/soil moisture/recharge residual and explain the local cell.
- `water_balance(region?)`: aggregate `P`, `ET`, modeled runoff, storage change and residual/recharge over AOI or user polygon.
- `run_scenario` should include the antecedent ET-derived soil state in its result.

### What We Will Not Claim

- No eddy-covariance-grade absolute ET without a flux tower, lysimeter, sapflow, calibrated catchment gauge or strong local validation.
- No full Penman-Monteith from the current local Daymet file alone.
- No energy-balance ET from the staged Landsat archive until thermal/LST assets are restaged.
- Even after thermal restaging, 27 ha is small for anchor-pixel energy balance. Use an AOI-plus-context window and label outputs as candidate estimates.
- Full PM forced by gridMET or ERA5-Land is only as good as 4 km or 9 km gridded wind/humidity over complex forest terrain.
- OpenET, MOD16, TerraClimate and GLEAM are validation/context layers for this parcel, not local truth. OpenET's own docs warn of forest high bias.

## Recommended First Pull Request Scope

1. Add Daymet `vp` to the Adirondack climate fetcher and update summaries.
2. Add `scripts/derive_et0_daily.py` with Oudin, Hargreaves-Samani, Priestley-Taylor and FAO-56 reduced-data ETo.
3. Add `scripts/et_water_balance.py` with daily root-zone depletion, AET, recharge residual and antecedent moisture.
4. Feed antecedent moisture into `hydro_scenario.py`.
5. Add `et_summary`, `et_at` and `water_balance` MCP tools.
6. Add gridMET fetcher as the first gap-fill fetcher; defer ERA5-Land and energy-balance ET to later phases.

That scope is scientifically defensible and uses VEIL's existing architecture: file-backed derived products, store registration, append-only journal, simulation-layer exports, and MCP read tools.

## References and Provider Docs

- Allen, R.G., Pereira, L.S., Raes, D., Smith, M. 1998. FAO Irrigation and Drainage Paper 56. https://www.fao.org/4/X0490E/x0490e00.htm
- ASCE-EWRI. 2005. The ASCE Standardized Reference Evapotranspiration Equation. https://ascelibrary.org/doi/book/10.1061/9780784408056
- Hargreaves, G.H., Samani, Z.A. 1985. Reference crop evapotranspiration from temperature. DOI `10.13031/2013.26773`.
- Oudin, L. et al. 2005. Which potential evapotranspiration input for a lumped rainfall-runoff model? DOI `10.1016/j.jhydrol.2004.08.026`.
- Fisher, J.B. et al. 2008. PT-JPL ET model. DOI `10.1016/j.rse.2007.08.025`.
- Bastiaanssen, W.G.M. et al. 1998. SEBAL. DOI `10.1016/S0924-2716(98)00028-7`.
- Allen, R.G., Tasumi, M., Trezza, R. 2007. METRIC model. DOI `10.1061/(ASCE)0733-9437(2007)133:4(380)`.
- Senay, G.B. et al. 2013. SSEBop. DOI `10.1002/wrcr.20371`.
- Su, Z. 2002. SEBS. DOI `10.5194/hess-6-85-2002`.
- Daymet single-pixel docs: https://daymet.ornl.gov/single-pixel/
- gridMET docs: https://www.climatologylab.org/gridmet.html
- NLDAS-2 forcing docs: https://ldas.gsfc.nasa.gov/nldas/v2/forcing
- PRISM datasets PDF: https://www.prism.oregonstate.edu/documents/PRISM_datasets.pdf
- RTMA product inventory: https://www.nco.ncep.noaa.gov/pmb/products/rtma/
- HRRR docs and AWS registry: https://rapidrefresh.noaa.gov/hrrr/ and https://registry.opendata.aws/noaa-hrrr-pds/
- ERA5-Land CDS: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land
- ERA5 single levels CDS: https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
- MERRA-2 overview: https://gmao.gsfc.nasa.gov/gmao-products/merra-2/
- NOAA ISD: https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database
- Planetary Computer Landsat C2 L2 STAC: https://planetarycomputer.microsoft.com/api/stac/v1/collections/landsat-c2-l2
- OpenET API, methods, accuracy: https://etdata.org/api/ , https://etdata.org/methods/ , https://etdata.org/accuracy-known-issues/
- TerraClimate docs: https://www.climatologylab.org/terraclimate.html
- MODIS MOD11A2: https://www.earthdata.nasa.gov/data/catalog/lpcloud-mod11a2-061
- MODIS MOD16A2: https://www.earthdata.nasa.gov/data/catalog/lpcloud-mod16a2-061
- VIIRS VNP21A1D: https://www.earthdata.nasa.gov/data/catalog/lpcloud-vnp21a1d-002
- ASTER AST_08: https://www.earthdata.nasa.gov/data/catalog/lpcloud-ast-08-003
- GLEAM: https://www.gleam.eu/
