/* Shape helpers shared by app.js (page) and worker.js (background worker). */
"use strict";

// Handedness of a part, so a mirror image (left- vs right-hand version) is not taken for an
// identical copy. Copies that are only moved or rotated give the same answer; a mirrored copy
// gives the opposite one. Returns "L", "R", or "?" when the shape is (nearly) symmetric or its
// axes can't be told apart, in which case a mirror image looks the same anyway.
//
// Method: weight each triangle's centre by its area, find the part's three principal axes,
// point the longest two toward the side the part bulges to (third moment > 0), make the third
// axis their cross product, and take the sign of the bulge along that third axis.
self.shapeChirality = function (P, I) {
  const n = I.length / 3;
  if (!n) return "?";
  const w = new Float64Array(n), g = new Float64Array(3 * n);
  let A = 0, cx = 0, cy = 0, cz = 0;
  for (let t = 0; t < n; t++) {
    const a = 3 * I[3 * t], b = 3 * I[3 * t + 1], c = 3 * I[3 * t + 2];
    const ux = P[b] - P[a], uy = P[b + 1] - P[a + 1], uz = P[b + 2] - P[a + 2];
    const vx = P[c] - P[a], vy = P[c + 1] - P[a + 1], vz = P[c + 2] - P[a + 2];
    const area = Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) / 2;
    const gx = (P[a] + P[b] + P[c]) / 3, gy = (P[a + 1] + P[b + 1] + P[c + 1]) / 3, gz = (P[a + 2] + P[b + 2] + P[c + 2]) / 3;
    w[t] = area; g[3 * t] = gx; g[3 * t + 1] = gy; g[3 * t + 2] = gz;
    A += area; cx += area * gx; cy += area * gy; cz += area * gz;
  }
  if (!(A > 0)) return "?";
  cx /= A; cy /= A; cz /= A;
  for (let t = 0; t < n; t++) { g[3 * t] -= cx; g[3 * t + 1] -= cy; g[3 * t + 2] -= cz; }
  let xx = 0, xy = 0, xz = 0, yy = 0, yz = 0, zz = 0;
  for (let t = 0; t < n; t++) {
    const x = g[3 * t], y = g[3 * t + 1], z = g[3 * t + 2], k = w[t];
    xx += k * x * x; xy += k * x * y; xz += k * x * z; yy += k * y * y; yz += k * y * z; zz += k * z * z;
  }
  const { values, vectors } = eigenSym3([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]]);
  const order = [0, 1, 2].sort((p, q) => values[q] - values[p]);
  const [l1, l2, l3] = order.map((i) => values[i]);
  if (!(l1 > 0) || (l1 - l2) / l1 < 0.01 || (l2 - l3) / l1 < 0.01) return "?"; // axes not distinct
  const len = Math.sqrt(l1 / A); // size of the part, so the bulge measure has no units
  const bulge = (v) => {
    let s = 0;
    for (let t = 0; t < n; t++) { const p = g[3 * t] * v[0] + g[3 * t + 1] * v[1] + g[3 * t + 2] * v[2]; s += w[t] * p * p * p; }
    return s / (A * len * len * len);
  };
  let e1 = vectors[order[0]], e2 = vectors[order[1]];
  const b1 = bulge(e1), b2 = bulge(e2);
  if (Math.abs(b1) < 1e-3 || Math.abs(b2) < 1e-3) return "?"; // symmetric along an axis
  if (b1 < 0) e1 = e1.map((v) => -v);
  if (b2 < 0) e2 = e2.map((v) => -v);
  const e3 = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
  const b3 = bulge(e3);
  if (Math.abs(b3) < 1e-3) return "?";
  return b3 > 0 ? "R" : "L";

  // symmetric 3x3 eigen-decomposition (Jacobi); vectors[i] belongs to values[i]
  function eigenSym3(m) {
    const a = m.map((r) => r.slice()), V = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    for (let sweep = 0; sweep < 50; sweep++) {
      if (Math.abs(a[0][1]) + Math.abs(a[0][2]) + Math.abs(a[1][2]) < 1e-14 * (Math.abs(a[0][0]) + Math.abs(a[1][1]) + Math.abs(a[2][2]))) break;
      for (const [p, q] of [[0, 1], [0, 2], [1, 2]]) {
        if (a[p][q] === 0) continue;
        const th = (a[q][q] - a[p][p]) / (2 * a[p][q]);
        const t = Math.sign(th || 1) / (Math.abs(th) + Math.sqrt(th * th + 1));
        const c = 1 / Math.sqrt(t * t + 1), s = t * c;
        for (let k = 0; k < 3; k++) { const kp = a[k][p], kq = a[k][q]; a[k][p] = c * kp - s * kq; a[k][q] = s * kp + c * kq; }
        for (let k = 0; k < 3; k++) { const pk = a[p][k], qk = a[q][k]; a[p][k] = c * pk - s * qk; a[q][k] = s * pk + c * qk; }
        for (let k = 0; k < 3; k++) { const kp = V[k][p], kq = V[k][q]; V[k][p] = c * kp - s * kq; V[k][q] = s * kp + c * kq; }
      }
    }
    return { values: [a[0][0], a[1][1], a[2][2]], vectors: [0, 1, 2].map((j) => [V[0][j], V[1][j], V[2][j]]) };
  }
};
