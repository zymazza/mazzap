/**
 * Georeferencing for the digital twin — a thin wrapper over proj4js.
 *
 * The 3D scene is in local meters offset from an origin in a projected CRS
 * (any CRS — the definition comes from data/georef.json, not from code):
 *
 *   world.x =  (easting  - origin_easting)        // +x = east
 *   world.z = -(northing - origin_northing)       // -z = north
 *   world.y =  elevation - minElevation           // up
 *
 * The proj4 string in georef.json defines the projection; the vendored
 * proj4js (public/vendor/proj4.js) does the math. In the browser the
 * definition is fetched from /data/georef.json on first use; in Node (tests)
 * it is read from disk, or injected with VEILGeoref.configure({proj4}).
 */
(function attachGeoref(global) {
  'use strict';

  const IS_NODE = typeof module !== 'undefined' && module.exports
    && typeof window === 'undefined';

  let projDef = null;     // proj4 string for the projected CRS
  let converter = null;   // proj4 converter (projected <-> geographic)

  function proj4lib() {
    if (global.proj4) return global.proj4;
    if (IS_NODE) {
      // eslint-disable-next-line global-require
      const path = require('path');
      // eslint-disable-next-line global-require
      return require(path.join(__dirname, '..', 'vendor', 'proj4.js'));
    }
    throw new Error('proj4 not loaded — include /vendor/proj4.js before georef.js');
  }

  function loadProjDef() {
    if (projDef) return projDef;
    if (IS_NODE) {
      // eslint-disable-next-line global-require
      const fs = require('fs');
      // eslint-disable-next-line global-require
      const path = require('path');
      const dataDir = process.env.TWIN_DATA_DIR
        ? path.resolve(process.env.TWIN_DATA_DIR)
        : path.join(__dirname, '..', '..', 'data');
      const raw = fs.readFileSync(path.join(dataDir, 'georef.json'), 'utf8');
      projDef = JSON.parse(raw).proj4;
    } else {
      // Synchronous on purpose: the definition is a tiny local file and every
      // caller (readout, chat, identify) needs it before the first conversion.
      const xhr = new XMLHttpRequest();
      xhr.open('GET', '/data/georef.json', false);
      xhr.send();
      projDef = JSON.parse(xhr.responseText).proj4;
    }
    if (!projDef) throw new Error('georef.json has no proj4 definition');
    return projDef;
  }

  function getConverter() {
    if (!converter) converter = proj4lib()(loadProjDef());
    return converter;
  }

  /** Override the projection (used by tests / non-default deployments). */
  function configure(options) {
    if (options && options.proj4) {
      projDef = options.proj4;
      converter = null;
    }
  }

  /** Projected (easting, northing in meters) -> { lon, lat } in degrees. */
  function projectedToGeographic(easting, northing) {
    const out = getConverter().inverse([easting, northing]);
    return { lon: out[0], lat: out[1] };
  }

  /** { lon, lat } degrees -> [easting, northing] meters in the projected CRS. */
  function geographicToProjected(lon, lat) {
    return getConverter().forward([lon, lat]);
  }

  /**
   * Human label for the twin's projected CRS, derived from its proj4 string
   * (e.g. "UTM 13N"); falls back to a generic label for non-UTM projections.
   */
  function projectionLabel() {
    const def = loadProjDef();
    const utm = /\+proj=utm\b/.test(def) && def.match(/\+zone=(\d+)/);
    if (utm) return 'UTM ' + utm[1] + (/\+south\b/.test(def) ? 'S' : 'N');
    return 'Easting / Northing';
  }

  /**
   * Build a converter bound to a scene's projected origin + terrain grid.
   * originUtm: [easting, northing, _] from the scene payload.
   */
  function createSceneGeoref(originUtm, minElevation) {
    const ox = Array.isArray(originUtm) ? originUtm[0] : 0;
    const oy = Array.isArray(originUtm) ? originUtm[1] : 0;
    const baseElev = Number.isFinite(minElevation) ? minElevation : 0;
    return {
      /** Three.js world point -> geographic coordinate. */
      worldToGeo(worldX, worldY, worldZ) {
        const easting = ox + worldX;
        const northing = oy - worldZ; // world.z = -localNorth
        const { lon, lat } = projectedToGeographic(easting, northing);
        return {
          lon,
          lat,
          easting,
          northing,
          elevation_m: baseElev + (Number.isFinite(worldY) ? worldY : 0),
        };
      },
    };
  }

  global.VEILGeoref = {
    configure,
    projectedToGeographic,
    geographicToProjected,
    projectionLabel,
    createSceneGeoref,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = global.VEILGeoref;
  }
})(typeof window !== 'undefined' ? window : globalThis);
