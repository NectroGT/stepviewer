/* Runs the CAD engine (occt-import-js) off the page's main thread, so the page stays
   responsive while a file is read. app.js starts this from a blob: URL, which makes the
   worker inherit the page's Content Security Policy (no outside connections). */
"use strict";
let occt = null;

// Bounding box and the bounding sphere around its centre (as three.js computes them).
function bounds(P) {
  const b = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
  for (let i = 0; i < P.length; i += 3) for (let k = 0; k < 3; k++) {
    const v = P[i + k]; if (v < b[k]) b[k] = v; if (v > b[k + 3]) b[k + 3] = v;
  }
  const cx = (b[0] + b[3]) / 2, cy = (b[1] + b[4]) / 2, cz = (b[2] + b[5]) / 2;
  let r2 = 0;
  for (let i = 0; i < P.length; i += 3) { const dx = P[i] - cx, dy = P[i + 1] - cy, dz = P[i + 2] - cz; r2 = Math.max(r2, dx * dx + dy * dy + dz * dz); }
  return { box: b, sphere: [cx, cy, cz, Math.sqrt(r2)] };
}

// Volume (closed meshes) and surface area, same maths as meshStats() in app.js.
function stats(P, I) {
  let vol = 0, area = 0;
  for (let t = 0; t < I.length; t += 3) {
    const a = 3 * I[t], b = 3 * I[t + 1], c = 3 * I[t + 2];
    const ax = P[a], ay = P[a + 1], az = P[a + 2], bx = P[b], by = P[b + 1], bz = P[b + 2], cx = P[c], cy = P[c + 1], cz = P[c + 2];
    vol += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx);
    const ux = bx - ax, uy = by - ay, uz = bz - az, vx = cx - ax, vy = cy - ay, vz = cz - az;
    area += Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) / 2;
  }
  return { volume: Math.abs(vol / 6), area };
}

// Outline edges: a port of three.js EdgesGeometry (r147). An edge is drawn when the faces on
// either side meet at more than `angle` degrees, or when only one face uses it.
function edges(P, I, angle) {
  const precision = 1e4, thresholdDot = Math.cos((angle * Math.PI) / 180);
  const out = [], edgeData = new Map();
  const key = (i) => `${Math.round(P[3 * i] * precision)},${Math.round(P[3 * i + 1] * precision)},${Math.round(P[3 * i + 2] * precision)}`;
  const push = (i, j) => out.push(P[3 * i], P[3 * i + 1], P[3 * i + 2], P[3 * j], P[3 * j + 1], P[3 * j + 2]);
  for (let t = 0; t < I.length; t += 3) {
    const v = [I[t], I[t + 1], I[t + 2]];
    // face normal = (c - b) x (a - b)
    const ax = P[3 * v[0]], ay = P[3 * v[0] + 1], az = P[3 * v[0] + 2];
    const bx = P[3 * v[1]], by = P[3 * v[1] + 1], bz = P[3 * v[1] + 2];
    const cx = P[3 * v[2]], cy = P[3 * v[2] + 1], cz = P[3 * v[2] + 2];
    const ux = cx - bx, uy = cy - by, uz = cz - bz, wx = ax - bx, wy = ay - by, wz = az - bz;
    let nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx;
    const nl = Math.hypot(nx, ny, nz) || 1; nx /= nl; ny /= nl; nz /= nl;
    const h = [key(v[0]), key(v[1]), key(v[2])];
    if (h[0] === h[1] || h[1] === h[2] || h[2] === h[0]) continue; // degenerate triangle
    for (let j = 0; j < 3; j++) {
      const n = (j + 1) % 3, hash = `${h[j]}_${h[n]}`, reverse = `${h[n]}_${h[j]}`;
      const other = edgeData.get(reverse);
      if (other) {
        if (nx * other.nx + ny * other.ny + nz * other.nz <= thresholdDot) push(v[j], v[n]);
        edgeData.set(reverse, null);
      } else if (!edgeData.has(hash)) {
        edgeData.set(hash, { i0: v[j], i1: v[n], nx, ny, nz });
      }
    }
  }
  for (const e of edgeData.values()) if (e) push(e.i0, e.i1);
  return new Float32Array(out);
}

self.onmessage = async (e) => {
  const m = e.data;
  if (m.init) { // m.init is the site's base URL
    try {
      importScripts(new URL("mesh.js", m.init).href);
      importScripts(new URL("occt-import-js.js", m.init).href);
      occt = await occtimportjs({ locateFile: (p) => new URL(p, m.init).href });
      postMessage({ ready: true });
    } catch (err) {
      postMessage({ initError: String((err && err.message) || err) });
    }
    return;
  }
  try {
    const data = new Uint8Array(m.buf);
    const res = self.MeshFiles.EXTS.includes(m.ext) ? await self.MeshFiles.read(m.ext, m.buf)   // STL, OBJ, 3MF, glTF
      : m.ext === "stp" || m.ext === "step" ? occt.ReadStepFile(data, m.params)
      : m.ext === "igs" || m.ext === "iges" ? occt.ReadIgesFile(data, m.params) : occt.ReadBrepFile(data, m.params);
    // hand the big arrays over as typed arrays without copying them
    const transfer = [];
    const typed = (holder, T) => { const a = holder.array = new T(holder.array); transfer.push(a.buffer); };
    if (res && res.meshes) res.meshes.forEach((om) => {
      typed(om.attributes.position, Float32Array);
      if (om.attributes.normal) typed(om.attributes.normal, Float32Array);
      typed(om.index, Uint32Array);
      // heavy per-part work done here instead of on the page (which would freeze it)
      const P = om.attributes.position.array, I = om.index.array;
      om.prep = { ...bounds(P), ...stats(P, I), edges: edges(P, I, 30) };
      transfer.push(om.prep.edges.buffer);
    });
    postMessage({ res }, transfer);
  } catch (err) {
    postMessage({ error: String((err && err.message) || err) });
  }
};
