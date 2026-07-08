# Viewshed Analysis Research (2024-2026)

Context from this repo: VEIL is a local/offline digital twin. The viewer is Three.js with draped raster/vector layers; the data pipeline is Python/GDAL/numpy; no cloud fetch should be required at view time. The astronomy subsystem pattern is also relevant: keep a local engine, validate it against an external/oracle implementation, and make physical assumptions explicit.

## 1. Algorithms

Single-observer viewshed families, as compared in Haverkort/Toma's survey and implementations (https://arxiv.org/abs/1810.01946):

| Algorithm | Model | Speed | Accuracy / tradeoff | Fit |
|---|---:|---:|---|---|
| R3 / Franklin-Ray | Line of sight from observer to every target cell; samples grid-line intersections with interpolation. | `O(n sqrt(n))`; too slow for repeated 2-6M-cell updates. | Often treated as the "exact" raster LOS model, but still depends on interpolation and sample convention. | Validation/small windows only. |
| R2 | Casts sightlines only to boundary cells; intermediate cells inherit/check along rays. | `O(n)`. | Fast approximation; loses angular coverage detail vs R3. | Browser/web-worker approximation if GPU unavailable; not final masks. |
| XDraw | Concentric layers; constant work per point. | `O(n)`, very fast. | Lowest accuracy in the cited comparison. | Avoid for survey-grade masks; useful only for rough previews. |
| van Kreveld sweep | Radial sweep maintaining active cells/horizon. | `O(n log n)`. | Efficient but uses square-cell/event model, not identical to R3 interpolation. | Good conceptual basis for a numpy engine if implemented carefully. |
| Haverkort/Toma horizon methods | Maintains/merges horizons for R3-style interpolation. | Worst case can be high, but fast in practice because horizons are small. | Best accuracy in their comparison while scaling beyond memory. | Best "pure algorithm" target, but more complex than GDAL. |
| Wang et al. / GDAL | "Generating Viewsheds without Using Sightlines"; dynamic/wavefront-style DEM scan. | Practical, production-proven. | Not R3, but widely used; supports offsets, curvature/refraction, AGL/min-height outputs. | Best current VEIL pipeline baseline/oracle. |
| GPU depth map / shadow map | Render terrain/buildings from observer into a depth atlas/cubemap; compare later samples/fragments. | Real time on loaded mesh. | Image/mesh-resolution limited; depth precision and aliasing issues; not a DEM-cell exact viewshed. | Best in-browser drag preview. |

GDAL `gdal_viewshed` explicitly uses Wang 2000 and requires projected CRS for meaningful results (https://gdal.org/en/stable/programs/gdal_viewshed.html, paper: https://www.asprs.org/wp-content/uploads/pers/2000journal/january/2000_jan_87-90.pdf). GRASS `r.viewshed` uses an efficient external-memory visibility algorithm rather than naive LOS and is a strong cross-check (https://grass.osgeo.org/grass-stable/manuals/r.viewshed.html).

Recommendation by use case:

- Offline Python/GDAL/numpy pipeline: use GDAL's Wang implementation as the production baseline and validation oracle. If a pure-numpy module is required, implement a radial horizon/sweep engine and validate it against `gdal_viewshed`, GRASS `r.viewshed`, and synthetic cases. R3 is too slow for normal production, but useful for tiny fixtures.
- Real-time browser recompute while dragging: use a GPU observer-depth pass over the already-loaded terrain/building mesh, then drape the visible/hidden classification back onto terrain. Exact R3/R2/XDraw-style CPU updates over 2-6M cells are not appropriate on the main thread; a worker-based R2/radial approximation can be a fallback, not the authoritative result.

## 2. Curvature And Refraction

For long-range terrain, curvature/refraction must be part of the data product, not a rendering afterthought. GDAL uses:

```text
Height_corrected = Height_DEM - CurvCoeff * TargetDistance^2 / SphereDiameter
CurvCoeff = 1 - RefractionCoeff
```

GDAL's visible-light default since 3.4 is `RefractionCoeff = 1/7`, so `CurvCoeff = 6/7 ~= 0.85714`; radio work often uses stronger refraction, e.g. `CurvCoeff` around `0.75..0.675` (https://gdal.org/en/stable/programs/gdal_viewshed.html).

ArcGIS's older planar Viewshed uses the same idea with a default refractivity coefficient `0.13` and Earth diameter `12,740,000 m` (https://doc.esri.com/en/arcgis-pro/latest/tool-reference/spatial-analyst/using-viewshed-and-observer-points-for-visibility.html). ArcGIS Geodesic Viewshed instead transforms DEM and observers to a 3D geocentric coordinate system, traces geodesic sightlines, and can use GPU acceleration; its default refractivity coefficient is also `0.13` (https://doc.esri.com/en/arcgis-pro/latest/tool-reference/spatial-analyst/how-viewshed-2-works.html, https://doc.esri.com/en/arcgis-pro/latest/tool-reference/spatial-analyst/viewshed-2.html).

Professional visual-impact guidance is explicit that Earth curvature should be included, that atmospheric refraction reduces the curvature correction, and that assumptions must be recorded. NatureScot's wind-farm guidance gives a survey-style correction formula and says site-specific meteorology is needed for true geodetic precision, but visualizations usually use a standard coefficient (https://www.nature.scot/doc/visual-representation-wind-farms-guidance).

Important convention warning: different tools call different quantities `k`, `refraction`, or `curvature coefficient`. Store VEIL configuration in GDAL terms: `cc = 1 - refraction_coeff`, default `cc = 6/7` for optical visibility.

## 3. Fetch Radius And Analysis Extent

Use an effective Earth radius for screening:

```text
cc = 1 - k_refraction
R_eff = R / cc
drop(d) ~= d^2 / (2 R_eff)
s(h) = R_eff * acos(R_eff / (R_eff + h)) ~= sqrt(2 R_eff h)
Dmax(h, H) = s(h) + s(H)
```

`h` is observer eye height above local ground; `H` is target/terrain height above the target's local tangent/reference surface. The small-height approximation is enough for fetch planning; the actual viewshed still needs DEM LOS. With `cc = 6/7`, a 2 m eye sees a flat 2 m target only about 10.9 km away, but a 100 m ridge can be mutually visible at roughly 43 km and a 1000 m mountain at roughly 127 km.

No authoritative source found a universal "13 mile rule." It is a rule of thumb at best and fails in mountainous terrain. Professional tools decide extent by explicit outer radius, study area, object/observer height, sensitivity of receptors, and performance. ArcGIS recommends using an outer radius to limit computation and gives 25 km as a practical example, not a standard (https://doc.esri.com/en/arcgis-pro/latest/tool-reference/spatial-analyst/viewshed-2.html). NatureScot expects ZTV extents, distance rings, DTM resolution, viewer height, and curvature/refraction assumptions to be documented, not hidden behind a fixed radius (https://www.nature.scot/doc/visual-representation-wind-farms-guidance).

Practical VEIL fetch rule:

- Compute `R_fetch = s(h_obs) + s(H_max) + margin`, where `H_max` is a conservative maximum terrain prominence or object height in the far ring.
- In mountains, derive `H_max` from coarse DEM tiles while expanding outward; stop when no outside tile's max elevation can rise above the current horizon bound, or when a project cap is reached.
- Use a margin of at least one coarse tile plus DEM cell diagonal and atmospheric uncertainty.
- For Adirondack-style terrain, 13 miles is too small for robust ridge/peak masking. Use 50 km as a normal long-range terrain default; use 100-150 km when high peaks or solar/satellite horizon fidelity matter.

## 4. Distant DEM Sources

US, authoritative:

- USGS 3DEP / National Map products are public-domain and scriptable through TNM API (https://tnmaccess.nationalmap.gov/api/v1/). Dataset names include `National Elevation Dataset (NED) 1/3 arc-second` and `National Elevation Dataset (NED) 1 arc-second`.
- Product query pattern: `https://tnmaccess.nationalmap.gov/api/v1/products?datasets=National%20Elevation%20Dataset%20(NED)%201/3%20arc-second&bbox=-74,44,-73,45&prodFormats=GeoTIFF`.
- Current staged GeoTIFF pattern: `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n44w074/USGS_13_n44w074.tif`; 1 arc-second uses `/Elevation/1/TIFF/current/.../USGS_1_...tif`.
- Resolutions: 1/3 arc-second is about 10 m; 1 arc-second is about 30 m. Use 1/3 arc-second near the AOI and 1 arc-second for long-distance rings unless local 1 m/5 m products are already staged.

Global:

- Copernicus DEM on AWS Open Data provides COG DSM tiles, GLO-30 and GLO-90, under Copernicus free-use terms (https://registry.opendata.aws/copernicus-dem/, https://copernicus-dem-30m.s3.amazonaws.com/readme.html).
- Buckets: `s3://copernicus-dem-30m/` and `s3://copernicus-dem-90m/`, region `eu-central-1`, no-sign access. STAC: `https://copernicus-dem-30m-stac.s3.amazonaws.com/` and `https://copernicus-dem-90m-stac.s3.amazonaws.com/`.
- Tile folder format: `Copernicus_DSM_COG_10_N44_00_W074_00_DEM/` for GLO-30 and `Copernicus_DSM_COG_30_N44_00_W074_00_DEM/` for GLO-90. Check each bucket's `tileList.txt`.
- AWS Terrain Tiles are useful web/global coarse terrain, not the survey oracle. Bucket `s3://elevation-tiles-prod/`; Terrarium PNG decoding is `(red * 256 + green + blue / 256) - 32768`; GeoTIFF and Skadi HGT are also available (https://registry.opendata.aws/terrain-tiles/, https://raw.githubusercontent.com/tilezen/joerd/master/docs/formats.md, https://raw.githubusercontent.com/tilezen/joerd/master/docs/attribution.md).

Multi-resolution ring strategy:

- 0-5/10 km: project-local DEM or 3DEP 1/3 arc-second; preserve native resolution for masks.
- 10-50 km: 3DEP 1 arc-second or Copernicus GLO-30; resample into the analysis CRS.
- 50-150 km: GLO-90 or downsampled 1 arc-second/GLO-30; use mainly to bound horizons and distant terrain fade/clip masks.
- Always mosaic/reproject offline into local projected CRS before viewshed. Do not fetch DEM tiles from the viewer.

## 5. Area-Based Visibility And Clipping

"Visible from any point in an AOI" is a cumulative/total viewshed problem. The brute-force exact version is expensive because it runs visibility from many/all observer cells. Tabik et al. describe the total viewshed problem and a multi-GPU data-relocation approach, reporting large speedups over GIS baselines, but it remains a specialized batch computation (https://arxiv.org/abs/2003.02200).

Practical VEIL approach:

- Sample observer points across the AOI: boundary, corners, local high points, and a grid at DEM/coarser spacing.
- Run single-observer viewsheds with the intended observer AGL and OR the masks for "visible from any point"; optionally sum them for observability count.
- Use GDAL `-om ACCUM` where its observer-grid semantics fit, or use GRASS-style batch `r.viewshed` plus raster OR/sum operations.
- For clipping distant terrain, use the union mask only as a conservative culling product: dilate it by at least one far-cell/angular bin, keep near terrain un-clipped, and prefer fade/low-confidence styling over hard removal at the boundary.

Observer height AGL directly expands required extent through the `sqrt(h)` horizon term and also lets observers see over near terrain. AOI union products must be recomputed per height class; do not reuse a 2 m pedestrian mask for a tower, drone, or camera mast.

## 6. Horizon Profiles For Solar And Satellite

Per-azimuth horizon profiles are the right representation for solar and satellite work: for each azimuth, store the maximum terrain elevation angle above the local horizontal.

- GRASS `r.horizon` computes horizon angles in point or raster mode and is designed to feed `r.sun`; examples use azimuth steps and max distance settings, with buffer zones around the area (https://grass.osgeo.org/grass-stable/manuals/r.horizon.html).
- pvlib can fetch PVGIS horizon profiles with `get_pvgis_horizon`, returning horizon elevation indexed by azimuth; pvlib's azimuth convention is clockwise from north (https://pvlib-python.readthedocs.io/en/stable/reference/generated/pvlib.iotools.get_pvgis_horizon.html).
- Solar application: if sun altitude at its azimuth is below the terrain horizon, direct beam/DNI is blocked. Diffuse light should be reduced by sky-view/terrain-shading logic, not simply zeroed with the direct-beam mask.
- Satellite application: compute satellite azimuth/elevation, then compare elevation to the terrain horizon at that azimuth. For GEO satellites, a useful spherical approximation is `E = atan2(cos(psi) - R/r_geo, sqrt(1 - cos(psi)^2))`, where `cos(psi)=cos(lat)*cos(delta_lon)` and `r_geo ~= 42164 km`; terrain then subtracts from that elevation.

## 7. Web Rendering

Modern web viewers usually separate interactive visual feedback from authoritative analysis:

- ArcGIS Maps SDK exposes a client-side ViewshedAnalysis workflow for interactive scenes (https://developers.arcgis.com/javascript/latest/references/core/analysis/ViewshedAnalysis/).
- deck.gl `TerrainLayer` shows the common web pattern for tiled height maps and mesh reconstruction, including Mapzen/Terrarium-style RGB elevation decoding and tiled terrain rendering (https://deck.gl/docs/api-reference/geo-layers/terrain-layer).
- Three.js can implement the same core visibility preview as point-light shadow mapping: render terrain/buildings from the observer into a cubemap or polar/equirectangular depth atlas, then classify terrain fragments or samples by comparing their observer-space depth to the atlas.

For VEIL, render the interactive mask as a draped layer. Keep precomputed authoritative masks as raster drapes exported by the pipeline. For far terrain, use the AOI cumulative mask and distance/resolution rings to fade tiles in/out; avoid hard culling unless the mask is conservative and documented.

## Final Recommendation

Algorithm pair:

- Pipeline: use `gdal_viewshed` / `GDALViewshedGenerate` as the first production implementation and validation oracle, with projected CRS, explicit `-oz`, `-tz`, `-md`, and `-cc 0.857142857` unless a project sets a different optical/radio refraction model. Optional cross-oracles: GRASS `r.viewshed` for algorithm diversity and ArcGIS Geodesic Viewshed for spot checks where available.
- Browser: use GPU observer-depth viewshed over the loaded Three.js terrain/building mesh for drag-time feedback. Debounce and replace/confirm with pipeline masks for saved viewpoints. Do not present browser depth-map output as survey-grade.

Fetch strategy:

- Use `Dmax(h,H)=s(h)+s(H)` with `s(x)=R_eff*acos(R_eff/(R_eff+x))`, `R_eff=R/cc`, plus margin. Derive `H_max` from coarse DEM rings and expand until outside terrain cannot beat the current horizon, or apply a documented project cap.
- Treat the "13 mile" idea as folklore. Defaults should be height/terrain-driven: 50 km for normal mountain-region visual terrain context, 100-150 km for high-peak, solar-horizon, or satellite-line-of-sight products.

DEM strategy:

- US: 3DEP 1/3 arc-second near, 1 arc-second far, fetched offline from TNM/S3 GeoTIFFs.
- Global/fallback: Copernicus GLO-30/GLO-90 COGs; AWS Terrain Tiles only for preview/coarse global context or non-authoritative web terrain.
- Build multi-resolution offline mosaics in the local projected CRS; export compact masks/horizon profiles/drape layers to the viewer.

Validation:

- Synthetic fixtures: flat Earth horizon, curvature/refraction drop, isolated ridge, knife-edge wall, nodata gap, observer/target offset cases.
- External oracle: `gdal_viewshed` for the canonical local output; optional GRASS/ArcGIS comparisons for long-range/geodesic edge cases.
- Record every mask's DEM source/date/resolution, CRS, observer/target heights, `cc`, analysis radius, nodata policy, and whether output is authoritative pipeline or interactive preview.
