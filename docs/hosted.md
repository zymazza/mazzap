# Hosted mode

VEIL is local-first by default. Hosted mode adds browser sessions and
per-session twin directories while keeping local mode unchanged.

```bash
VEIL_HOSTED=1 \
VEIL_SESSION_SECRET="$(openssl rand -hex 32)" \
VEIL_HOSTED_ROOT=/srv/veil-hosted \
OPENAI_REQUIRE_USER_KEY=1 \
CHAT_PROVIDER=openai \
HOST=0.0.0.0 \
npm start
```

How it works:

- The server issues a signed `veil_session` HttpOnly cookie.
- The first request creates
  `$VEIL_HOSTED_ROOT/users/<session>/twins/default/data`.
- Until that session has a built twin, `/` serves the same setup UI used by
  `npm run init`. The visitor draws an AOI, chooses optional national layers,
  and the server builds that session's twin.
- `/data/*`, chat MCP tools, annotations, simulation, and geospatial uploads use
  the session twin directory.
- The chat panel still uses bring-your-own OpenAI keys. With
  `OPENAI_REQUIRE_USER_KEY=1`, the server never falls back to an operator key.
- Live telemetry, building placement writes, and survey uploads are disabled in
  hosted mode until they have product-grade account and device ownership rules.

Do not put a personal or private twin in `VEIL_HOSTED_TEMPLATE_DATA_DIR` for a
public deployment. That variable is optional and should only point at a
deliberately shareable starter dataset.

Users can add their own geospatial layers from the Layers sidebar. The upload
endpoint saves all dropped files together, then runs `scripts/add_layer.py`,
which reads GeoTIFF/vector data through GDAL/OGR, reprojects to the twin CRS,
clips to the terrain footprint/AOI grid, emits drape assets, and updates the
Atlas catalog.

For production, put the Node server behind HTTPS, set a stable
`VEIL_SESSION_SECRET`, back up `VEIL_HOSTED_ROOT`, and enforce reverse-proxy
request size/rate limits in addition to `VEIL_LAYER_UPLOAD_MAX_BYTES`.
