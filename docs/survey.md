# Survey companion — the QField write path into the twin store

The Survey companion turns field work into twin-store history: VEIL generates
a QField survey package, you survey on the phone, and uploading the project
folder back writes entities and observations into the store — journaled,
replayable, queryable like everything else.

## The loop

```
npm run build-survey-package          # 1. VEIL generates the package
        │
        ▼
data/surveys/package/survey-package.zip   (also linked from the viewer's
        │                                  "Survey companion" panel)
        ▼
QField on the phone                   # 2. survey: draw trails, drop photo
        │                             #    points, fill the baked-in forms
        ▼
zip the project folder ("Send to…" / compress)
        │
        ▼
viewer "Survey companion" panel       # 3. upload → POST /api/survey-upload
        │
        ▼
data/surveys/incoming/ + uploads.log.jsonl   (durable drop, then synchronous
        │                                     ingest; `npm run export` is the
        ▼                                     deferred fallback)
twin store entities + observations    # 4. survey_<layer>:<uuid> entities
        │
        ▼
data/surveys/<layer>.geojson + survey-layers.json   (viewer drape + identify;
                                                     MCP picks the kinds up
                                                     automatically)
```

## The package

`scripts/build_survey_package.py` writes `data/surveys/package/`:

- **`survey.gpkg`** — four empty layers in the twin's projected CRS:
  `trails` and `stream_centerlines` (lines), `photo_points` and
  `observations` (points, each with a camera attachment field). Common
  fields: `uuid`, `name`, `status`, `notes`, `captured_at`, `accuracy_m`.
- **`project.qgs`** — the QGIS/QField project, generated from the twin's
  georef (no QGIS needed to build it; validated against QGIS 4.x). It bakes
  the form behaviors in:
  - `uuid` — hidden, auto-filled (`regexp_replace(uuid(),'[{}]','')`). This
    is the natural key entity identity is built from; never edit it.
  - `status` — value map **active / retired / removed**, defaults active.
    Retirement is an *explicit field act*: set it on the form. A feature
    missing from an upload is never auto-retired (partial sync is normal).
  - `captured_at` — defaults to `now()` at creation, not updated on edit.
  - `accuracy_m` — defaults to QField's `@position_horizontal_accuracy`.
  - `photo` — attachment widget, camera-enabled, relative paths (QField
    saves into the project folder, typically under `DCIM/`).
- **`basemap.tif`** — the twin's aerial imagery as a GeoTIFF (skipped, with
  a message, if the twin has no georeferenced imagery).

Re-running the build produces a **fresh, empty** package — don't regenerate
it to "update" a package that has un-uploaded field data on the phone.

## Ingest semantics

`scripts/ingest_survey.py` (spawned by the server per upload; also run with
`--pending` by `npm run export` via a cursor over `uploads.log.jsonl`):

- Entity IDs are `survey_<layer>:<uuid>` — identity survives moves, edits,
  and re-uploads. Re-walking a trail updates the same entity's geometry.
- Per feature, ingest diffs **before writing**: an unchanged feature (or an
  already-retired one still sitting in the phone's gpkg) journals nothing.
  Re-uploading the same zip produces only a `run`/`finish_run` pair.
- `status` → `retired`/`removed` retires the entity; back to `active`
  un-retires it. Retired features stay out of the viewer exports but keep
  their full history in the store.
- Geometry changes write the scene-local GeoJSON itself as a `geometry`
  observation (plus `geom_sha1`), so "this trail as of last spring" is a
  plain `history()` / `entity_history` query.
- `observed_at` is `captured_at` normalized to UTC for a feature's first
  ingest; changes found on later uploads get the upload time (QField doesn't
  timestamp edits). **Assumption:** naive `captured_at` values are
  device-local time in the *server's* timezone — fine when phone and server
  live in the same place; revisit if they don't.
- `accuracy_m` is its own observation attribute. The `confidence` column
  stays null — its store-wide semantics are 0–1 quality, not meters.
- Photos are copied to `data/surveys/photos/<sha1[:12]>-<name>` and observed
  as `{path, bytes, sha1, captured_at}` (the building `model_file` pattern).
  The upload must be the **whole project folder zipped** (not just the gpkg)
  so the photo files referenced by the attachment field come along.

## Upload route & auth

`POST /api/survey-upload?name=<outing>` takes the raw zip as the request
body (the panel just `fetch`es the file). The server stores the zip and logs
it before ingest, so a sick Python environment never loses an upload. If a
gitignored `.survey_token` file exists at the repo root, the
`X-Survey-Token` header must match (the panel prompts once and remembers it
in localStorage). The intended network posture is localhost/Tailscale; the
token is the fallback for a LAN bind, not an internet-facing auth story.

## Querying

Survey kinds are ordinary store kinds, so the MCP tools (`find_entities`,
`aggregate_entities`, `summarize_region`) and "Ask G.A.I.A." see them with
no extra wiring. Known v1 gap: `identify_at` walks atlas layers only, so the
chat agent won't see survey features in point-identify results.
