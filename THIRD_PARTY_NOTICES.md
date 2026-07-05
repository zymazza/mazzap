# Third-party notices

The files vendored under `public/vendor/` are third-party software,
redistributed under their own licenses:

## three.js (`public/vendor/three.min.js`)

Copyright © 2010–2023 Three.js Authors.
Licensed under the MIT License (SPDX: MIT).
<https://github.com/mrdoob/three.js>

## OrbitControls (`public/vendor/OrbitControls.js`)

Adapted from the three.js examples
(`three/examples/jsm/controls/OrbitControls.js`) to a plain script against
the vendored global `THREE` build.
Copyright © 2010–2023 Three.js Authors. Licensed under the MIT License.

## TransformControls (`public/vendor/TransformControls.js`)

Adapted from the three.js examples
(`three/examples/jsm/controls/TransformControls.js`, r163) to a plain script
against the vendored global `THREE` build.
Copyright © 2010–2023 Three.js Authors. Licensed under the MIT License.

## proj4js (`public/vendor/proj4.js`)

Copyright © 2014 Mike Adair, Richard Greenwood, Didier Richard,
Stephen Irons, Olivier Terral and Calvin Metcalf.
Licensed under the MIT License.
<https://github.com/proj4js/proj4js>

---

## Other third-party components

### Leaflet (setup map only)

The new-twin setup page (`public/init.html`) loads **Leaflet 1.9.4** from the
unpkg CDN (with Subresource Integrity hashes) to render the AOI locator map.
It is the only frontend dependency that is not vendored locally, and it is used
only by the optional setup flow — the twin viewer itself fetches nothing from
the network. The setup map also requests basemap tiles from OpenStreetMap and
ArcGIS World Imagery, so the setup page (unlike the viewer) does contact those
services.
Copyright © 2010–2024 Volodymyr Agafonkin, © 2010–2011 CloudMade.
Licensed under the BSD 2-Clause License. <https://leafletjs.com>

### Tree models (`public/assets/tree-library/*.obj`)

The low-poly tree meshes (`beech`, `birch`, `elm`, `fir`, `maple`, `pine`,
`spruce`) are original works created for this project by the author and are
covered by this repository's MIT License. The viewer parses only their geometry
and `usemtl` group names (`public/viewer/vegetation.js`); the vestigial `mtllib`
header line in each `.obj` is ignored, and no companion `.mtl` file is shipped
or required.

---

The full MIT License text is in [LICENSE](LICENSE). Each MIT-licensed project
above is licensed by its own copyright holders under those same terms; Leaflet
is under the BSD 2-Clause License linked above.
