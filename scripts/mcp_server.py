#!/usr/bin/env python3
"""MCP server over a VEIL digital-twin store — stdio transport.

This is the agent-facing surface of the twin: every tool is a thin wrapper
around scripts/twin_query.py (where all logic lives and is tested). The
store is read-only; the only writes are the draw_polygon / draw_point /
clear_drawings tools, which maintain data/annotations.json — ephemeral
orange drawings the 3D viewer polls and renders so an agent can point at
places on the map instead of reciting coordinates. Drawings never touch
the store or the journal.

Run it:
    python3 scripts/mcp_server.py

Register it with Claude Code:
    claude mcp add veil-twin -- python3 /abs/path/to/scripts/mcp_server.py

Conventions every tool follows:
  * Points are {"lat": deg, "lon": deg} or {"x": m, "y": m} in scene-local
    meters (x = east, y = north, the twin's projected CRS minus its origin —
    describe_twin reports which CRS). Results always echo both forms.
  * All distances/areas are meters / square meters. Heights are meters.
  * `region` arguments take exactly one of four shapes:
      {"aoi": true}                                    — the parcel AOI
      {"bbox": [minx, miny, maxx, maxy]}               — scene-local meters
      {"within_m": r, "point": {lat,lon} | {x,y}}      — circle, r in meters
      {"polygon": [[lon,lat], ...] or [[x,y], ...]}    — ring auto-closed
  * Every factual value carries provenance: source / confidence / run_id /
    observed_at from the store's observations, or acquisition / service for
    atlas layers.
  * Errors come back as {"error": ...} objects with the valid alternatives
    listed — never a stack trace.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "live"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import twin_query  # noqa: E402
import live_store  # noqa: E402
from twin_query import TwinQueryError  # noqa: E402

mcp = FastMCP(
    "veil-twin",
    instructions=(
        "A georeferenced 3D digital twin of a real place: terrain, trees and "
        "shrubs, buildings, parcels, streams, roads, plus a local atlas of soils, "
        "geology, land cover, wetlands and species-habitat layers, live telemetry "
        "from field devices and gateways, a terrain-"
        "hydrology model (flow, wetness, ponding, springs/seeps, snowmelt and "
        "storm scenarios), a wildfire fuels/scenario model (fuel model, ROS, "
        "crown potential, arrival/flame/intensity scenario drapes), and any "
        "field-survey uploads. The store is read-only; run_scenario, "
        "run_fire_scenario, the draw_* tools and the layer-view tools "
        "(set_layer_visibility / filter_layer / reset_layer_views) are the only "
        "writers; run_fire_scenario is a store-WRITING scenario/fire pipeline "
        "run, the same run the viewer's Fire pane launches. "
        "Use describe_place for lightweight location/coordinate context; use "
        "describe_twin only when broader inventory counts or run history matter. "
        "Points are {lat,lon} degrees or {x,y} scene-local meters; every result "
        "echoes both. "
        "For live trackers, gateways, and messages, use live_telemetry_snapshot "
        "for current state and live_telemetry_history / live_telemetry_store_summary "
        "for the temporary replay database; live_device entities in the twin store "
        "only exist after live telemetry has been exported/materialized. "
        "For water questions use hydrology_at / hydrology_summary, and run_scenario "
        "for 'what if it …' events. For fire questions use fire_at / fire_summary, "
        "and run_fire_scenario for ignition/weather scenarios; fire scenario layers "
        "are viewer-toggled, while WRC reference drapes are ordinary atlas layers "
        "reachable with set_layer_visibility / filter_layer. For field observations use list_survey_layers "
        "then the survey_* kinds. "
        "When an answer is about a place, show it on the user's live 3D map "
        "instead of reciting coordinates: draw_polygon / draw_point put orange "
        "shapes on it, and set_layer_visibility / filter_layer turn the twin's "
        "own atlas layers on and reveal just the regions that matter (e.g. only "
        "the GAP cells where a species occurs, or one soil class). "
        "reset_layer_views hands layer control back to the user. "
        "For thematic or site-selection questions, first consider what spatial "
        "evidence would ideally answer the question, then inspect list_layers "
        "to see what this twin actually has; layer ids may be unexpected, so "
        "choose from the catalog and use layer_summary on promising layers."),
)

_tq = None


def _query():
    global _tq
    if _tq is None:
        _tq = twin_query.TwinQuery()
    return _tq


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TwinQueryError as e:
        return e.payload


@mcp.tool()
def describe_place() -> dict:
    """Lightweight orientation: the twin's name/id, CRS, coordinate convention,
    queryable extent, and parcel-AOI bounds/area. Use this when you need
    location or coordinate context without inspecting the layer inventory."""
    return _run(_query().describe_place)


@mcp.tool()
def describe_twin() -> dict:
    """Orient yourself: the twin's UTM origin and CRS, the coordinate
    convention, the queryable extent and parcel-AOI bounds (scene-local
    meters and lat/lon), entity counts per kind (alive/total), the full
    pipeline-run history, and layer-catalog counts. Prefer describe_place for
    lightweight orientation; use describe_twin when the broader inventory/run
    history is relevant."""
    return _run(_query().describe_twin)


@mcp.tool()
def find_entities(kind: str, near: dict | None = None,
                  within_m: float | None = None, region: dict | None = None,
                  attr_filters: list[str] | None = None, limit: int = 50) -> dict:
    """Find entities of one kind, spatially and/or by attribute.

    kind: tree | shrub | live_device | building | building_model | parcel | stream | road.
    near + within_m: center {lat,lon}|{x,y}|{"entity_id": id} and a radius in
      METERS (sugar for the within_m region shape; results sorted nearest
      first with distance_m). region: the four-shape region object (see
      server description) — pass either near+within_m or region, not both.
    attr_filters: strings like "height > 20", "type = evergreen",
      "source = lidar" (ops = != > >= < <=; numbers compared numerically,
      strings case-insensitively) evaluated against latest observations.
      Tree attrs: height (m), radius (crown m), z (elevation m), type
      (evergreen|deciduous), species, community, source (lidar|canopy_fill),
      confidence; building_model attrs include name (House, Barn, ...).
    limit: max entities returned (default 50, cap 1000); total_matched is
      always the full count. Each entity returns its position in both
      coordinate systems and all latest attrs with provenance
      (source/confidence/run_id/observed_at). Only alive (non-retired)
      entities are returned."""
    return _run(_query().find_entities, kind, near=near, within_m=within_m,
                region=region, attr_filters=attr_filters, limit=limit)


@mcp.tool()
def get_entity(entity_id: str) -> dict:
    """Full current state of one entity by ID (e.g. "tree:000a8cc17eb6",
    "building_model:<id>"): every latest attribute with provenance, position
    in both coordinate systems, geometry (scene-local GeoJSON for parcels /
    buildings / streams / roads), and the runs that created/retired it."""
    return _run(_query().get_entity, entity_id)


@mcp.tool()
def entity_history(entity_id: str, attr: str | None = None) -> dict:
    """The append-only observation timeline of one entity, oldest first —
    how its attributes changed across pipeline runs (a tree keeps its ID
    across rebuilds; moved stems become new entities by design). attr
    restricts to one attribute (e.g. "height"). Each observation carries
    value, observed_at, run_id + script, source, confidence."""
    return _run(_query().entity_history, entity_id, attr=attr)


@mcp.tool()
def identify_at(point: dict) -> dict:
    """Everything true at a single point — the server-side equivalent of
    clicking the viewer: terrain elevation (m), soil map unit with drainage /
    hydrologic group / slope / farmland class, surficial geology, ecoregions,
    LANDFIRE community, NLCD land cover, wetland and protected-species areas,
    GAP species richness and the list of species with modeled habitat there,
    plus the parcel and any building footprint containing the point. point:
    {"lat","lon"} degrees or {"x","y"} scene-local
    meters. Outside the twin you get a clear outside_extent result. Every
    fact carries the source layer's acquisition provenance."""
    return _run(_query().identify_at, point)


@mcp.tool()
def sample_raster(layer_id: str, point: dict) -> dict:
    """Sample one raster atlas layer at a point: raw cell value plus its
    legend name. Raster layer_ids: landfire_evt_2024, nlcd_2019_landcover,
    mesoscale_soil_grid, human_modification, gap_species_richness. point:
    {"lat","lon"} or {"x","y"} (scene-local meters)."""
    return _run(_query().sample_raster, layer_id, point)


@mcp.tool()
def list_layers(kind: str | None = None) -> dict:
    """The layer catalog: every atlas layer and registered input file with
    its acquisition provenance (local_source_clip vs api_snapshot, service
    URL, fetch time, feature count, status). status="empty" means the layer
    legitimately has nothing on this parcel. kind filters (e.g. "vector",
    "raster", "imagery", "wetlands"); invalid kinds list the valid ones.
    queryable_as marks layers identify_at/sample_raster can read. Entries also
    include natural-language text_metadata when available (description,
    abstract, purpose, notes), inferred themes, query/filter/drape flags, and
    compact field/label/legend previews so you can inspect what exists even
    when ids/names are unfamiliar."""
    return _run(_query().list_layers, kind=kind)


@mcp.tool()
def layer_summary(layer_id: str) -> dict:
    """One layer in depth. Vectors: feature count, geometry types, attribute
    fields, distinct labels. Categorical rasters: dimensions, bounds (both
    coordinate systems), and the legend with per-class cell counts and
    shares (e.g. the LANDFIRE community breakdown). Plus natural-language
    metadata/description when present and the layer's acquisition provenance."""
    return _run(_query().layer_summary, layer_id)


@mcp.tool()
def summarize_region(region: dict) -> dict:
    """What's happening inside a shape — the one call for "summarize this
    area". region: {"aoi": true} | {"bbox":[minx,miny,maxx,maxy] meters} |
    {"within_m": r, "point": {...}} | {"polygon": [[lon,lat],...] or
    [[x,y],...]}. Returns region area (m2), entity counts by kind, tree
    statistics (count, evergreen/deciduous split, mean/max height, summed
    crown area, top species, lidar vs canopy_fill sources), the parcels
    covering it (owner, address, acres), dominant LANDFIRE community and
    NLCD land-cover breakdown with shares, soils present, wetland and
    protected-species overlap, and GAP species-richness range — every block
    with its provenance. Shares are estimated from evenly spaced sample
    points (count and spacing reported)."""
    return _run(_query().summarize_region, region)


@mcp.tool()
def aggregate_entities(kind: str, metric: str, group_by: str | None = None,
                       where: list[str] | None = None,
                       region: dict | None = None) -> dict:
    """Aggregate latest-state values over entities of one kind. metric:
    "count", "crown_area" (summed pi*radius^2 in m2), or
    "<sum|mean|min|max>:<attr>" for numeric attrs, e.g. "mean:height".
    group_by: a categorical attr ("type", "species", "source", "community")
    — e.g. kind=tree, metric=count, group_by=type is the evergreen/deciduous
    split. where: attr_filters strings (see find_entities). region: the
    four-shape region object. Groups come back with entity_count and
    source/run provenance."""
    return _run(_query().aggregate_entities, kind, metric, group_by=group_by,
                where=where, region=region)


@mcp.tool()
def canopy_change(region: dict | None = None,
                  member: str = "member_parcel") -> dict:
    """When did canopy density change: tree count and summed crown area (m2)
    as of EACH pipeline run, in time order, with per-run deltas — computed
    from the append-only observation history, optionally scoped to a region
    (the four-shape object; a polygon makes "the north field" literal).
    member selects the population: member_parcel (default), member_surrounding
    (the terrain apron outside the parcel), or any."""
    return _run(_query().canopy_change, region=region, member=member)


@mcp.tool()
def list_survey_layers() -> dict:
    """The field-survey catalog (Survey companion, docs/survey.md): one entry
    per uploaded QField layer (trails, stream_centerlines, photo_points,
    observations) with its store kind `survey_<layer>`, geometry type, live
    feature count, attribute fields, and whether photos are attached. The
    survey_* kinds are first-class entities: query them with find_entities,
    summarize_region, aggregate_entities, get_entity and identify_at. Returns
    an empty list (with a note) when no survey has been uploaded yet."""
    return _run(_query().list_survey_layers)


@mcp.tool()
def live_telemetry_snapshot(include_hidden: bool = False,
                            prefer_live_api: bool = True) -> dict:
    """Current live telemetry state for field devices and gateway connections.

    Returns the active live snapshot: registered gateways, bridge/process
    status when the VEIL web server is reachable, latest device events,
    latest retained position/motion, display preferences, and freshness
    (active/stale/offline/no_location with age). Use this before querying
    `live_device` entities: the twin store only has `live_device` records
    after a telemetry day/snapshot has been exported to the store.

    include_hidden: include devices hidden in the live UI. prefer_live_api:
    when true, ask the running VEIL HTTP server for bridge status; if it is
    unavailable, the tool reconstructs latest state from the temporary
    telemetry store and registry files."""
    return live_store.telemetry_snapshot(include_hidden=include_hidden,
                                         prefer_live_api=prefer_live_api)


@mcp.tool()
def live_telemetry_history(date: str | None = None,
                           dates: list[str] | None = None,
                           device_ids: list[str] | None = None,
                           kind: str | None = None,
                           since: str | None = None,
                           until: str | None = None,
                           limit: int = 200) -> dict:
    """Read raw events from the temporary live telemetry data store
    (`data/live/telemetry.sqlite`).

    Filters: date or dates (YYYY-MM-DD), device_ids, kind
    (position|message|data|status|media|command), since/until ISO timestamps,
    and limit (cap 2000). Events are returned oldest-first within the limited
    result window. This is the replay/history surface for live inputs that have
    not necessarily been exported into `twin.gpkg`."""
    return live_store.telemetry_history(date=date, dates=dates,
                                        device_ids=device_ids, kind=kind,
                                        since=since, until=until, limit=limit)


@mcp.tool()
def live_telemetry_store_summary() -> dict:
    """Summarize the temporary live telemetry store: recorded days, total event
    and device counts, first/last observation times, counts by day/kind, recent
    devices, and recent exports into the durable twin store."""
    return live_store.telemetry_store_summary()


@mcp.tool()
def export_live_telemetry_to_twin(mode: str = "snapshot",
                                  date: str | None = None,
                                  dates: list[str] | None = None,
                                  device_ids: list[str] | None = None,
                                  at: str | None = None) -> dict:
    """Materialize live telemetry into the durable twin store as `live_device`
    entities. This writes to `data/twin.gpkg` and records an export in
    `data/live/telemetry.sqlite`.

    mode="snapshot" exports each device's latest selected event, optionally as
    of ISO timestamp `at`; mode="day" exports all matching events. Filter by
    date/dates and/or device_ids. Use this only when a live day/snapshot should
    become queryable later through normal entity tools."""
    return live_store.export_to_twin({
        "mode": mode,
        "date": date,
        "dates": dates,
        "device_ids": device_ids,
        "at": at,
    })


@mcp.tool()
def discover_live_connections(transport: str = "serial",
                              timeout: float | None = None) -> dict:
    """Discover local gateway connection targets for live telemetry.

    transport: serial or bluetooth. Returns candidate ports/BLE devices that
    can be registered as gateway connections. Internet/TCP gateways are not
    discoverable; register them with manage_live_gateway using
    transport="internet" and address=<host-or-url>."""
    return live_store.discover_connections(transport=transport, timeout=timeout)


@mcp.tool()
def manage_live_gateway(action: str, gateway_id: str | None = None,
                        name: str | None = None,
                        protocol: str = "meshtastic",
                        transport: str = "bluetooth",
                        address: str | None = None,
                        node_id: str | None = None) -> dict:
    """Register and control live telemetry gateway connections.

    action: register (save gateway), connect (register and start bridge),
    start/restart (start a registered bridge), stop (stop the bridge but keep
    the gateway registration), or remove (stop bridge and remove gateway plus
    current child-device registry entries). protocol defaults to meshtastic.
    transport: bluetooth, serial, or internet. address is a BLE address, serial
    port, or internet host/URL. Bridge process control requires the VEIL web
    server to be running; pure registration can fall back to editing
    `data/live/registry.json`."""
    return live_store.manage_gateway(action=action, gateway_id=gateway_id,
                                     name=name, protocol=protocol,
                                     transport=transport, address=address,
                                     node_id=node_id)


@mcp.tool()
def manage_live_device(action: str, device_id: str,
                       gateway_id: str | None = None,
                       label: str | None = None,
                       visible: bool | None = None,
                       color: str | None = None,
                       command: str | None = None,
                       channel_index: int = 0,
                       hop_limit: int | None = None) -> dict:
    """Manage a tracked live device.

    action="update" changes display preferences (label, visible, color, or
    gateway_id); action="remove" clears the device from the current live
    registry; action="request_position" queues a Meshtastic position request
    through an already-running gateway bridge; action="traceroute" queues a
    traceroute. action="command" uses the explicit command argument
    ("request_position" or "traceroute"). Commands require gateway_id."""
    return live_store.manage_device(action=action, device_id=device_id,
                                    gateway_id=gateway_id, label=label,
                                    visible=visible, color=color,
                                    command=command,
                                    channel_index=channel_index,
                                    hop_limit=hop_limit)


@mcp.tool()
def hydrology_at(point: dict) -> dict:
    """The terrain-hydrology read at one point (the Simulation window's
    click-to-identify, server-side): upslope contributing area, TWI wetness
    percentile, ponding depth, the spring/seep score, and — if a scenario has
    been run — its per-cell runoff and routed flow, plus the SSURGO soil here
    and a plain-language reading matching the viewer. point: {"lat","lon"} or
    {"x","y"} scene-local meters. Needs `npm run analyze-hydrology` to have run."""
    return _run(_query().hydrology_at, point)


@mcp.tool()
def hydrology_summary() -> dict:
    """Property-wide hydrology: the Tier-1 analysis summary (drainage outlet,
    depression/pond storage, hydrologic soil-group fractions, soil map units,
    the top spring/seep candidates with lat/lon, stream/wetland validation)
    plus the last scenario run (water input, runoff/infiltration partition,
    outlet discharge with its ±50% uncertainty band, ponding). The headline
    "what does the water do here" call."""
    return _run(_query().hydrology_summary)


@mcp.tool()
def run_scenario(mode: str = "snowmelt", swe_in: float | None = None,
                 preset: str | None = None, melt_days: float | None = None,
                 rain_in: float | None = None, storm_hours: float | None = None,
                 antecedent: str | None = None, frozen: bool = False) -> dict:
    """Run a snowmelt or rainstorm hydrology scenario and return the result.
    This WRITES — it rewrites the viewer's scenario drape layers and records a
    `scenario` pipeline run in the store (past scenarios stay queryable). Use
    it when the user asks a "what if it …" question. Parameters are clamped
    exactly like the Simulation window. mode="snowmelt": swe_in (snow water
    equivalent, inches 0-40) OR preset ("median"|"p90"|"max" from the 44-year
    climatology); melt_days 0.5-30. mode="rain": storm_hours 0.5-240. rain_in:
    rain (or rain-on-snow) inches 0-15. antecedent: "dry"|"normal"|"wet" soil
    moisture. frozen: frozen-ground floor (higher runoff). After running, draw
    on the map or cite hydrology_summary numbers; the geometry is reliable,
    discharge magnitude is scenario-grade (±50%), not a forecast."""
    return _run(_query().run_scenario, mode=mode, swe_in=swe_in, preset=preset,
                melt_days=melt_days, rain_in=rain_in, storm_hours=storm_hours,
                antecedent=antecedent, frozen=frozen)


@mcp.tool()
def fire_at(point: dict) -> dict:
    """The wildfire read at one point (the Fire pane's click-to-identify,
    server-side): fuel model, reference and slope-adjusted rate of spread,
    crown potential, and — if a fire scenario has been run — arrival time,
    flame length, fireline intensity, and crown class, plus a plain-language
    reading matching the viewer. point: {"lat","lon"} or {"x","y"}
    scene-local meters. Needs `npm run analyze-fuels` to have run."""
    return _run(_query().fire_at, point)


@mcp.tool()
def fire_summary() -> dict:
    """Property-wide wildfire summary: fuel-model breakdown, canopy stats,
    crown-potential fractions, TI/CI baseline, and the last scenario result
    (weather, derived moistures/FMC, ROS at ignition, burned area, and layer
    outputs). The headline "what does fire do here" call."""
    return _run(_query().fire_summary)


@mcp.tool()
def run_fire_scenario(ignition_x: float, ignition_y: float,
                      weather_class: str | None = None,
                      temp_f: float | None = None,
                      rh_min: float | None = None,
                      wind_mph: float | None = None,
                      wind_dir: float | None = None,
                      days_since_rain: float | None = None,
                      drought: str | None = None,
                      exposure: str | None = None,
                      date: str | None = None,
                      duration_min: float | None = None,
                      fmc_override: float | None = None) -> dict:
    """Run a wildfire ignition/weather scenario and return the result. This
    WRITES — it rewrites the viewer's fire scenario drape layers and records a
    `scenario` pipeline run in the store (past scenarios stay queryable). Use
    it when the user asks a fire "what if it starts here under this weather"
    question. `wind_dir`, when supplied, is the downwind / maximum-spread
    azimuth in degrees clockwise from north, not the meteorological wind-from
    bearing. Parameters are clamped exactly like the viewer Fire pane's
    /api/fire-simulate route."""
    return _run(_query().run_fire_scenario, ignition_x=ignition_x,
                ignition_y=ignition_y, weather_class=weather_class,
                temp_f=temp_f, rh_min=rh_min, wind_mph=wind_mph,
                wind_dir=wind_dir, days_since_rain=days_since_rain,
                drought=drought, exposure=exposure, date=date,
                duration_min=duration_min, fmc_override=fmc_override)


@mcp.tool()
def draw_polygon(polygon: list, label: str | None = None) -> dict:
    """Draw an orange polygon on the user's live 3D map — use this whenever
    an answer points at an area (a stand, a wet corner, a recommended site)
    instead of listing coordinates in text. polygon: at least 3 [lon,lat] or
    scene-local [x,y] vertex pairs (auto-detected; ring auto-closed). label:
    short name shown on the map (e.g. "Densest evergreen stand"). Drawings
    are presentation-only annotations — they appear immediately, persist
    until cleared, and never modify the twin. The user can remove them with
    the viewer's "Clear drawings" button."""
    return _run(_query().draw_polygon, polygon, label=label)


@mcp.tool()
def draw_point(point: dict, label: str | None = None) -> dict:
    """Drop an orange marker on the user's live 3D map — use this whenever
    an answer points at a spot (a specific tree, the highest point, where to
    dig) instead of listing coordinates in text. point: {"lat","lon"} degrees
    or {"x","y"} scene-local meters. label: short name shown on the map.
    Presentation-only — appears immediately, persists until cleared, never
    modifies the twin. The user can remove drawings with the viewer's
    "Clear drawings" button."""
    return _run(_query().draw_point, point, label=label)


@mcp.tool()
def recommend_sites(objective: str = "overlook", region: dict | None = None,
                   count: int = 3, min_separation_m: float = 120.0,
                   draw: bool = True, label_prefix: str | None = None,
                   purpose: str | None = None,
                   hard_filters: list | None = None,
                   preferences: list | None = None,
                   avoid: list | None = None,
                   strict: bool = False) -> dict:
    """Recommend multiple good sites inside a region (default: parcel AOI), ranked
    by reusable terrain signals and deterministic spacing, with optional
    constraint gates.

    `objective` is the legacy free-text/preset profile selector. `purpose` is the
    preferred alias for new callers. `hard_filters` are non-negotiable candidate
    predicates such as {"signal":"gap_species","op":"includes","value":"Gray Fox"},
    {"signal":"terrain.slope_deg","op":"<=","value":8},
    {"signal":"hydrology.wetness","op":"<=","value":0.4}, or
    {"signal":"raster_class","layer_id":"nlcd_2019_landcover","op":"in",
    "value":["Deciduous Forest"]}. Candidates and drawings are emitted only after
    hard filters pass final validation; unresolved intent terms draw nothing when
    strict=False and raise a structured error when strict=True.
    """
    return _run(_query().recommend_sites, objective=objective, region=region,
                count=count, min_separation_m=min_separation_m, draw=draw,
                label_prefix=label_prefix, purpose=purpose,
                hard_filters=hard_filters, preferences=preferences, avoid=avoid,
                strict=strict)


@mcp.tool()
def clear_drawings() -> dict:
    """Remove every drawn polygon and point marker from the user's 3D map.
    Call it when the user asks, or before drawing a fresh set for a new
    question so stale shapes don't pile up. Layer-view overrides
    (set_layer_visibility / filter_layer) are left untouched — clear those
    with reset_layer_views."""
    return _run(_query().clear_drawings)


@mcp.tool()
def set_layer_visibility(layer_id: str, visible: bool = True) -> dict:
    """Show or hide one of the twin's atlas map layers on the user's live 3D
    terrain — use this to bring up the layer your answer is about (land cover,
    soils, geology, hydrology, GAP species richness — whatever this twin holds)
    instead of describing it in text. layer_id: a drape-able atlas layer (the
    valid ids are listed by list_layers and echoed in any error). The layer is
    draped onto the terrain so it conforms to topography. visible=False hides
    it again. The override persists until you change it or call
    reset_layer_views."""
    return _run(_query().set_layer_visibility, layer_id, visible=visible)


@mcp.tool()
def filter_layer(layer_id: str, values: list,
                 field: str | None = None) -> dict:
    """Reveal ONLY the selected regions of an atlas layer (and turn the layer
    on) — the precise way to point at "where X is". Everything else in the
    layer is hidden until you clear the filter (call set_layer_visibility, or
    reset_layer_views). Use it for questions like "where could I find wild
    turkey": filter the GAP species-richness layer to that species and the map
    lights up only its modeled habitat.

    layer_id: a drape-able atlas layer. values: the regions to reveal —
      * raster categorical layers (e.g. LANDFIRE land cover): legend class
        names, from layer_summary(layer_id).classes[].name.
      * the GAP species-richness layer: species common-names (field defaults to
        "species"), from layer_summary(...).filterable_species or identify_at.
      * vector layers: the distinct values of `field` (defaults to the feature
        label) — see layer_summary(layer_id).labels / .attribute_fields.
    field: which attribute to match on (vectors only; ignored otherwise).
    Matching is case-insensitive; the result reports matched_values and any
    unmatched ones so you can correct a name. Combine with draw_polygon /
    draw_point when a single spot also helps."""
    return _run(_query().filter_layer, layer_id, values, field=field)


@mcp.tool()
def reset_layer_views() -> dict:
    """Undo every layer override you made with set_layer_visibility /
    filter_layer, handing the twin's layer toggles back to the user. Drawn
    polygons and points are left in place (clear those with clear_drawings).
    Call it when switching topics so stale layer state doesn't linger."""
    return _run(_query().reset_layer_views)


if __name__ == "__main__":
    mcp.run()  # stdio transport
