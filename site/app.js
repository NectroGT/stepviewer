(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const stage = $("stage"), readout = $("readout"), overlay = $("overlay"), label3d = $("label3d");

  // ---------- settings ----------
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };
  let unit = store.get("unit", "mm");
  const fmtLen = (mm, dp) => {
    if (unit === "in") return (mm / 25.4).toFixed(dp ?? 4) + " in";
    return mm.toFixed(dp ?? 3) + " mm";
  };
  const num = (mm) => unit === "in" ? (mm / 25.4).toFixed(4) : mm.toFixed(3);
  const fmtArea = (a) => unit === "in" ? (a / 645.16).toFixed(3) + " in²" : a.toFixed(1) + " mm²";
  const fmtVol = (v) => unit === "in" ? (v / 16387.064).toFixed(3) + " in³" : (v / 1000).toFixed(3) + " cm³";
  const sizeStr = (s) => `${num(s.x)} × ${num(s.y)} × ${num(s.z)}`;

  // ---------- three.js setup ----------
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  stage.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 1e6);
  camera.up.set(0, 0, 1);
  camera.position.set(200, -300, 200);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8892a0, 0.75));
  const sun = new THREE.DirectionalLight(0xffffff, 0.75);
  sun.position.set(0.5, 0.3, 1);
  camera.add(sun);
  scene.add(camera);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.screenSpacePanning = true;

  const modelGroup = new THREE.Group();
  const markGroup = new THREE.Group();
  scene.add(modelGroup, markGroup);
  let edgeMat = new THREE.LineBasicMaterial({ color: 0x2b3742, transparent: true, opacity: 0.55 });
  let markColor = new THREE.Color(0xe0560b), accentColor = new THREE.Color(0x1f5fd1);

  let dirty = true;
  const requestRender = () => { dirty = true; };
  controls.addEventListener("change", requestRender);

  function resize() {
    const w = stage.clientWidth, h = stage.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    requestRender();
  }
  new ResizeObserver(resize).observe(stage);

  function applyTheme() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    scene.background = new THREE.Color(v("--viewport") || "#dde3e9");
    edgeMat.color.set(v("--edge") || "#2b3742");
    markColor.set(v("--mark") || "#e0560b");
    accentColor.set(v("--accent") || "#1f5fd1");
    markGroup.children.forEach((m) => m.material && m.material.color && m.material.color.copy(markColor));
    if (faceOverlay) faceOverlay.material.color.copy(accentColor);
    selected.forEach((m) => m.material.emissive.copy(accentColor));
    requestRender();
  }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", applyTheme);
  new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // ---------- model state ----------
  let meshes = [];          // THREE.Mesh per occt mesh
  let modelBox = new THREE.Box3();
  let modelRadius = 100;
  let mode = "part";
  let selected = [], selectedGroup = null, faceOverlay = null;
  let picks = [];
  let currentFace = null;
  let measureLine = null, labelPos = null;

  function clearModel() {
    clearSelection(); clearMarks();
    meshes.forEach((m) => { m.geometry.dispose(); m.material.dispose(); m.children.forEach((c) => c.geometry.dispose()); });
    modelGroup.clear();
    meshes = [];
  }

  function buildModel(res) {
    clearModel();
    res.meshes.forEach((om, i) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(om.attributes.position.array, 3));
      if (om.attributes.normal) g.setAttribute("normal", new THREE.Float32BufferAttribute(om.attributes.normal.array, 3));
      else g.computeVertexNormals();
      g.setIndex(new THREE.Uint32BufferAttribute(Uint32Array.from(om.index.array), 1));
      g.computeBoundingBox(); g.computeBoundingSphere();
      const c = om.color || (om.brep_faces.find((f) => f.color) || {}).color || [0.72, 0.75, 0.79];
      const mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(c[0], c[1], c[2]), metalness: 0.15, roughness: 0.6, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      });
      const mesh = new THREE.Mesh(g, mat);
      mesh.userData = { index: i, name: om.name || `Body ${i + 1}`, faces: om.brep_faces, faceCache: new Map() };
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(g, 30), edgeMat);
      edges.raycast = () => {};
      mesh.add(edges);
      modelGroup.add(mesh);
      meshes.push(mesh);
    });
    updateModelBox();
    buildTree(res.root);
  }

  function updateModelBox(only) {
    modelBox = new THREE.Box3();
    (only || meshes).forEach((m) => { if (m.visible) modelBox.union(m.geometry.boundingBox); });
    if (modelBox.isEmpty()) modelBox.set(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50));
    modelRadius = Math.max(modelBox.getBoundingSphere(new THREE.Sphere()).radius, 1);
  }

  function showOverallChip() {
    const all = new THREE.Box3();
    meshes.forEach((m) => all.union(m.geometry.boundingBox));
    if (all.isEmpty()) { $("chip").hidden = true; return; }
    const s = all.getSize(new THREE.Vector3());
    $("chipLabel").textContent = `Overall size, ${unit} · ${meshes.length} ${meshes.length === 1 ? "body" : "bodies"}`;
    $("chipSize").textContent = sizeStr(s);
    $("chip").hidden = false;
  }

  // ---------- camera views ----------
  function frame(dir, up) {
    updateModelBox();
    const center = modelBox.getCenter(new THREE.Vector3());
    const d = dir ? dir.clone().normalize() : camera.position.clone().sub(controls.target).normalize();
    const fov = THREE.MathUtils.degToRad(camera.fov);
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * camera.aspect));
    const dist = modelRadius / Math.sin(fit / 2) * 1.05;
    camera.up.copy(up || new THREE.Vector3(0, 0, 1));
    camera.position.copy(center).addScaledVector(d, dist);
    camera.near = dist / 1000; camera.far = dist * 100;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
    requestRender();
  }
  const VIEWS = {
    iso: [new THREE.Vector3(1, -1, 0.8)],
    top: [new THREE.Vector3(0, 0, 1), new THREE.Vector3(0, 1, 0)],
    front: [new THREE.Vector3(0, -1, 0)],
    right: [new THREE.Vector3(1, 0, 0)],
  };
  document.querySelectorAll(".views button").forEach((b) => b.addEventListener("click", () => {
    const v = b.dataset.view;
    if (v === "fit") frame(); else frame(...VIEWS[v]);
  }));

  // ---------- geometry helpers ----------
  function faceIndexOf(mesh, tri) {
    const f = mesh.userData.faces; let lo = 0, hi = f.length - 1;
    while (lo <= hi) {
      const m = (lo + hi) >> 1;
      if (tri < f[m].first) hi = m - 1; else if (tri > f[m].last) lo = m + 1; else return m;
    }
    return -1;
  }

  function eigenSym3(a) { // Jacobi; returns {values, vectors(columns)}
    const A = a.map((r) => r.slice()), V = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    for (let sweep = 0; sweep < 50; sweep++) {
      const off = Math.abs(A[0][1]) + Math.abs(A[0][2]) + Math.abs(A[1][2]);
      if (off < 1e-14) break;
      for (const [p, q] of [[0, 1], [0, 2], [1, 2]]) {
        if (Math.abs(A[p][q]) < 1e-18) continue;
        const th = (A[q][q] - A[p][p]) / (2 * A[p][q]);
        const t = Math.sign(th || 1) / (Math.abs(th) + Math.sqrt(th * th + 1));
        const c = 1 / Math.sqrt(t * t + 1), s = t * c;
        for (let k = 0; k < 3; k++) {
          const akp = A[k][p], akq = A[k][q];
          A[k][p] = c * akp - s * akq; A[k][q] = s * akp + c * akq;
        }
        for (let k = 0; k < 3; k++) {
          const apk = A[p][k], aqk = A[q][k];
          A[p][k] = c * apk - s * aqk; A[q][k] = s * apk + c * aqk;
        }
        for (let k = 0; k < 3; k++) {
          const vkp = V[k][p], vkq = V[k][q];
          V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq;
        }
      }
    }
    return { values: [A[0][0], A[1][1], A[2][2]], vectors: [0, 1, 2].map((j) => new THREE.Vector3(V[0][j], V[1][j], V[2][j])) };
  }

  function solve3(M, b) { // Cramer's rule
    const det = (m) => m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
    const D = det(M); if (Math.abs(D) < 1e-20) return null;
    return [0, 1, 2].map((i) => det(M.map((r, k) => r.map((v, j) => (j === i ? b[k] : v)))) / D);
  }

  // Least-squares cylinder fit (Levenberg-Marquardt over axis tilt, axis position and radius).
  function fitCylinder(pts, axis0) {
    const u = new THREE.Vector3().crossVectors(axis0, Math.abs(axis0.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0)).normalize();
    const w = new THREE.Vector3().crossVectors(axis0, u);
    const o = pts.reduce((s, p) => s.add(p), new THREE.Vector3()).multiplyScalar(1 / pts.length);
    const loc = pts.map((p) => { const d = p.clone().sub(o); return [d.dot(u), d.dot(w), d.dot(axis0)]; });
    // start: circle fit in the plane across axis0
    let Sxx = 0, Sxy = 0, Syy = 0, Sx = 0, Sy = 0, Sxz = 0, Syz = 0, Sz = 0;
    loc.forEach(([x, y]) => { const z = x * x + y * y; Sxx += x * x; Sxy += x * y; Syy += y * y; Sx += x; Sy += y; Sxz += x * z; Syz += y * z; Sz += z; });
    const s0 = solve3([[Sxx, Sxy, Sx], [Sxy, Syy, Sy], [Sx, Sy, loc.length]], [-Sxz, -Syz, -Sz]);
    if (!s0) return null;
    let prm = [0, 0, -s0[0] / 2, -s0[1] / 2, 0];
    prm[4] = Math.sqrt(Math.max(prm[2] ** 2 + prm[3] ** 2 - s0[2], 1e-12));
    const resid = (q) => {
      const a = [q[0], q[1], 1], n = Math.hypot(a[0], a[1], a[2]); a[0] /= n; a[1] /= n; a[2] /= n;
      return loc.map(([x, y, z]) => {
        const dx = x - q[2], dy = y - q[3], dz = z, t = dx * a[0] + dy * a[1] + dz * a[2];
        return Math.hypot(dx - t * a[0], dy - t * a[1], dz - t * a[2]) - q[4];
      });
    };
    const sse = (r) => r.reduce((s, v) => s + v * v, 0);
    let r = resid(prm), cost = sse(r), lambda = 1e-3;
    for (let it = 0; it < 60; it++) {
      const J = [];
      for (let k = 0; k < 5; k++) {
        const h = k < 2 ? 1e-7 : 1e-7 * Math.max(1, Math.abs(prm[4]));
        const q = prm.slice(); q[k] += h;
        const rk = resid(q); J.push(rk.map((v, i) => (v - r[i]) / h));
      }
      const A = [...Array(5)].map((_, p) => [...Array(5)].map((_, q) => J[p].reduce((s, v, i) => s + v * J[q][i], 0)));
      const g = [...Array(5)].map((_, p) => -J[p].reduce((s, v, i) => s + v * r[i], 0));
      let improved = false;
      for (let tries = 0; tries < 8 && !improved; tries++) {
        const Ad = A.map((row, p) => row.map((v, q) => (p === q ? v * (1 + lambda) + 1e-15 : v)));
        const step = solveN(Ad, g); if (!step) break;
        const q = prm.map((v, k) => v + step[k]);
        const rq = resid(q), cq = sse(rq);
        if (cq < cost) { prm = q; r = rq; const dc = cost - cq; cost = cq; lambda = Math.max(lambda / 4, 1e-9); improved = true; if (dc < 1e-18) it = 99; }
        else lambda *= 8;
      }
      if (!improved) break;
    }
    const a = new THREE.Vector3().addScaledVector(u, prm[0]).addScaledVector(w, prm[1]).add(axis0).normalize();
    const point = o.clone().addScaledVector(u, prm[2]).addScaledVector(w, prm[3]);
    return { axis: a, point, radius: Math.abs(prm[4]), rms: Math.sqrt(cost / loc.length) };
  }
  function solveN(A, b) { // Gaussian elimination with partial pivoting
    const n = b.length, M = A.map((row, i) => [...row, b[i]]);
    for (let c = 0; c < n; c++) {
      let p = c; for (let i = c + 1; i < n; i++) if (Math.abs(M[i][c]) > Math.abs(M[p][c])) p = i;
      if (Math.abs(M[p][c]) < 1e-30) return null;
      [M[c], M[p]] = [M[p], M[c]];
      for (let i = c + 1; i < n; i++) { const f = M[i][c] / M[c][c]; for (let j = c; j <= n; j++) M[i][j] -= f * M[c][j]; }
    }
    const x = new Array(n).fill(0);
    for (let i = n - 1; i >= 0; i--) { let s = M[i][n]; for (let j = i + 1; j < n; j++) s -= M[i][j] * x[j]; x[i] = s / M[i][i]; }
    return x;
  }

  function analyzeFace(mesh, fi) {
    if (mesh.userData.faceCache.has(fi)) return mesh.userData.faceCache.get(fi);
    const g = mesh.geometry, P = g.attributes.position.array, N = g.attributes.normal.array, I = g.index.array;
    const { first, last } = mesh.userData.faces[fi];
    const vset = new Set(); let area = 0;
    const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3();
    for (let t = first; t <= last; t++) {
      const i0 = I[3 * t], i1 = I[3 * t + 1], i2 = I[3 * t + 2];
      a.fromArray(P, 3 * i0); b.fromArray(P, 3 * i1); c.fromArray(P, 3 * i2);
      area += b.clone().sub(a).cross(c.clone().sub(a)).length() / 2;
      vset.add(i0); vset.add(i1); vset.add(i2);
    }
    const vs = [...vset];
    const box = new THREE.Box3();
    const pts = vs.map((v) => { const p = new THREE.Vector3().fromArray(P, 3 * v); box.expandByPoint(p); return p; });
    const nrm = vs.map((v) => new THREE.Vector3().fromArray(N, 3 * v).normalize());
    const nbar = nrm.reduce((s, n) => s.add(n), new THREE.Vector3()).normalize();
    const minDot = nrm.reduce((m, n) => Math.min(m, n.dot(nbar)), 1);
    let info = { type: "curved", area, box, faceNo: fi + 1 };

    if (minDot > 0.99995) {
      info = { type: "plane", area, box, normal: nbar, point: pts[0], faceNo: fi + 1 };
    } else {
      const M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
      nrm.forEach((n) => { const v = [n.x, n.y, n.z]; for (let r = 0; r < 3; r++) for (let k = 0; k < 3; k++) M[r][k] += v[r] * v[k]; });
      const e = eigenSym3(M);
      const kmin = e.values.indexOf(Math.min(...e.values));
      const axis0 = e.vectors[kmin].normalize();
      const maxAxial = nrm.reduce((m, n) => Math.max(m, Math.abs(n.dot(axis0))), 0);
      const cyl = maxAxial < 0.1 && pts.length >= 6 ? fitCylinder(pts, axis0) : null;
      if (cyl && cyl.rms < Math.max(0.0001 * cyl.radius, 0.0005)) { // mesh nodes sit on the true surface, so a real cylinder fits almost exactly
        const { axis, radius } = cyl;
        const u = new THREE.Vector3().crossVectors(axis, Math.abs(axis.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0)).normalize();
        const w = new THREE.Vector3().crossVectors(axis, u);
        const ang = [], ax = [];
        let inward = 0, minRadial = 1;
        pts.forEach((p, k) => {
          const d = p.clone().sub(cyl.point);
          const along = d.dot(axis); ax.push(along);
          const radial = d.addScaledVector(axis, -along);
          ang.push(Math.atan2(radial.dot(w), radial.dot(u)));
          const nr = nrm[k].dot(radial) / (radial.length() || 1);
          minRadial = Math.min(minRadial, Math.abs(nr));
          inward += nr < 0 ? 1 : -1;
        });
        const sweep = sweepOf(ang);
        const amin = Math.min(...ax), amax = Math.max(...ax);
        const center = cyl.point.clone().addScaledVector(axis, (amin + amax) / 2);
        // surface normals of a true cylinder point straight out from the axis
        if (minRadial > 0.995) info = { type: "cyl", area, box, axis, center, radius, length: amax - amin,
          sweep, inner: inward > 0, faceNo: fi + 1, verts: vs };
      }
    }
    mesh.userData.faceCache.set(fi, info);
    return info;
  }

  // Angle covered by points around an axis. The mesh places points at most ~20° apart on a
  // circle, so a largest gap below that means the circle is closed.
  function sweepOf(ang) {
    ang.sort((a, b) => a - b);
    let gap = ang[0] + 2 * Math.PI - ang[ang.length - 1];
    for (let k = 1; k < ang.length; k++) gap = Math.max(gap, ang[k] - ang[k - 1]);
    return gap < 0.4 ? 360 : 360 - THREE.MathUtils.radToDeg(gap);
  }

  function faceBoxes(mesh) {
    if (mesh.userData.faceBoxes) return mesh.userData.faceBoxes;
    const P = mesh.geometry.attributes.position.array, I = mesh.geometry.index.array, v = new THREE.Vector3();
    return (mesh.userData.faceBoxes = mesh.userData.faces.map(({ first, last }) => {
      const b = new THREE.Box3();
      for (let k = 3 * first; k < 3 * (last + 1); k++) b.expandByPoint(v.fromArray(P, 3 * I[k]));
      return b;
    }));
  }

  // STEP files usually split a hole into two or more faces; join coaxial faces of the same radius.
  function holeGroup(mesh, fi, f) {
    const P = mesh.geometry.attributes.position.array;
    const probe = f.box.clone().expandByScalar(2 * f.radius + f.length);
    const faces = [fi];
    faceBoxes(mesh).forEach((b, j) => {
      if (j === fi || !b.intersectsBox(probe)) return;
      const g = analyzeFace(mesh, j);
      if (g.type !== "cyl" || g.inner !== f.inner) return;
      if (Math.abs(g.radius - f.radius) > Math.max(0.001 * f.radius, 0.003)) return;
      if (Math.abs(g.axis.dot(f.axis)) < 0.9999) return;
      const w = g.center.clone().sub(f.center); w.addScaledVector(f.axis, -w.dot(f.axis));
      if (w.length() > Math.max(0.002 * f.radius, 0.01)) return;
      faces.push(j);
    });
    const axis = f.axis;
    const u = new THREE.Vector3().crossVectors(axis, Math.abs(axis.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0)).normalize();
    const w = new THREE.Vector3().crossVectors(axis, u);
    const ang = [], ax = [], p = new THREE.Vector3();
    faces.forEach((j) => analyzeFace(mesh, j).verts.forEach((vi) => {
      const d = p.fromArray(P, 3 * vi).sub(f.center); const along = d.dot(axis); ax.push(along);
      d.addScaledVector(axis, -along); ang.push(Math.atan2(d.dot(w), d.dot(u)));
    }));
    const amin = Math.min(...ax), amax = Math.max(...ax);
    return { ...f, faces, sweep: sweepOf(ang), length: amax - amin,
      center: f.center.clone().addScaledVector(axis, (amin + amax) / 2) };
  }

  function meshStats(mesh) {
    if (mesh.userData.stats) return mesh.userData.stats;
    const P = mesh.geometry.attributes.position.array, I = mesh.geometry.index.array;
    let vol = 0, area = 0;
    for (let t = 0; t < I.length; t += 3) {
      const a = 3 * I[t], b = 3 * I[t + 1], c = 3 * I[t + 2];
      const ax = P[a], ay = P[a + 1], az = P[a + 2], bx = P[b], by = P[b + 1], bz = P[b + 2], cx = P[c], cy = P[c + 1], cz = P[c + 2];
      vol += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx);
      const ux = bx - ax, uy = by - ay, uz = bz - az, vx = cx - ax, vy = cy - ay, vz = cz - az;
      area += Math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) / 2;
    }
    return (mesh.userData.stats = { volume: Math.abs(vol / 6), area });
  }

  // ---------- selection & marks ----------
  function clearSelection() {
    selected.forEach((m) => m.material.emissive.setRGB(0, 0, 0));
    selected = []; selectedGroup = null;
    if (faceOverlay) { faceOverlay.geometry.dispose(); faceOverlay.material.dispose(); faceOverlay.parent.remove(faceOverlay); faceOverlay = null; }
    currentFace = null;
    document.querySelectorAll(".row.sel").forEach((r) => r.classList.remove("sel"));
    requestRender();
  }
  function selectMeshes(list, row) {
    clearSelection();
    selected = list;
    list.forEach((m) => { m.material.emissive.copy(accentColor); m.material.emissiveIntensity = 0.35; });
    if (row) row.classList.add("sel");
    requestRender();
  }
  function selectMesh(mesh) {
    selectMeshes([mesh], document.querySelector(`.row[data-mesh="${mesh.userData.index}"]`));
  }
  function selectGroup(ids, name, row) {
    ids.forEach((i) => { meshes[i].visible = true; });
    syncChecks();
    selectMeshes(ids.map((i) => meshes[i]), row);
    selectedGroup = { ids, name };
    groupReadout(selectedGroup);
  }

  // Bodies with the same triangle count, the same dimensions (in any orientation) and the
  // same volume are treated as identical copies, e.g. the teeth on a comb or repeated bolts.
  function sigOf(mesh) {
    if (mesh.userData.sig) return mesh.userData.sig;
    const d = mesh.geometry.boundingBox.getSize(new THREE.Vector3()).toArray().map((v) => v.toFixed(2)).sort();
    return (mesh.userData.sig = `${mesh.geometry.index.count}|${d.join("x")}|${meshStats(mesh).volume.toPrecision(5)}`);
  }
  const twinsOf = (mesh) => meshes.filter((m) => sigOf(m) === sigOf(mesh));
  function highlightFace(mesh, fis) {
    clearSelection();
    const src = mesh.geometry.index.array, parts = [];
    [].concat(fis).forEach((fi) => { const { first, last } = mesh.userData.faces[fi]; parts.push(src.slice(3 * first, 3 * (last + 1))); });
    const I = new Uint32Array(parts.reduce((n, a) => n + a.length, 0));
    parts.reduce((o, a) => (I.set(a, o), o + a.length), 0);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", mesh.geometry.attributes.position);
    g.setIndex(new THREE.Uint32BufferAttribute(I, 1));
    faceOverlay = new THREE.Mesh(g, new THREE.MeshBasicMaterial({
      color: accentColor, transparent: true, opacity: 0.6, side: THREE.DoubleSide,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2, depthWrite: false,
    }));
    faceOverlay.raycast = () => {};
    mesh.add(faceOverlay);
    requestRender();
  }
  function clearMarks() {
    markGroup.children.slice().forEach((m) => { m.geometry.dispose(); m.material.dispose(); markGroup.remove(m); });
    measureLine = null; labelPos = null; label3d.hidden = true; picks = [];
    requestRender();
  }
  const dotGeo = new THREE.SphereGeometry(1, 16, 12);
  function addDot(p) {
    const m = new THREE.Mesh(dotGeo.clone(), new THREE.MeshBasicMaterial({ color: markColor, depthTest: false }));
    m.position.copy(p); m.renderOrder = 10; m.userData.dot = true;
    markGroup.add(m);
  }
  function addLine(p1, p2) {
    const g = new THREE.BufferGeometry().setFromPoints([p1, p2]);
    measureLine = new THREE.Line(g, new THREE.LineBasicMaterial({ color: markColor, depthTest: false }));
    measureLine.renderOrder = 9;
    markGroup.add(measureLine);
  }

  // ---------- readouts ----------
  let detailsOpen = store.get("details", "0") === "1";
  function setReadout(html) {
    readout.innerHTML = html; readout.hidden = !html;
    const more = readout.querySelector("[data-more]");
    if (more && !readout.querySelector(".kv")) more.remove();
    syncDetails();
  }
  function syncDetails() {
    readout.classList.toggle("open", detailsOpen);
    const more = readout.querySelector("[data-more]");
    if (more) { more.setAttribute("aria-expanded", String(detailsOpen)); more.textContent = detailsOpen ? "Less" : "Details"; }
  }
  function hintFor(m) {
    return {
      part: "Tap a part to see its size. Drag to turn, pinch to zoom, two fingers to pan.",
      face: "Tap a face to see its size, or a hole to get its diameter.",
      dist: "Tap two points. Corners snap. Tap two flat faces for thickness, or two holes for centre distance.",
    }[m];
  }
  function showHint() { setReadout(`<div class="hint">${hintFor(mode)}</div>`); }
  const head = (kind, name) => `<div class="head"><span class="kind">${kind}</span><span class="name">${esc(name)}</span><button class="more" type="button" data-more>Details</button><button class="x" aria-label="Clear" data-clear>×</button></div>`;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  readout.addEventListener("click", (e) => {
    if (e.target.closest("[data-more]")) { detailsOpen = !detailsOpen; store.set("details", detailsOpen ? "1" : "0"); syncDetails(); return; }
    if (e.target.closest("[data-clear]")) { clearSelection(); clearMarks(); showHint(); }
    const tw = e.target.closest("[data-twins]");
    if (tw) {
      const m = meshes[+tw.dataset.twins], ids = twinsOf(m).map((x) => x.userData.index);
      const grpRow = [...document.querySelectorAll(".tree .row.grp")].find((r) => r.dataset.ids === ids.join(","));
      selectGroup(ids, grpRow ? grpRow.querySelector(".nm").textContent : m.userData.name, grpRow);
    }
  });

  function partReadout(mesh) {
    const s = mesh.geometry.boundingBox.getSize(new THREE.Vector3());
    const st = meshStats(mesh);
    setReadout(`${head("Part", mesh.userData.name)}
      <div class="big">${sizeStr(s)} <small>${unit}</small></div>
      <dl class="kv">${twinsOf(mesh).length > 1 ? `<dt>Identical in file</dt><dd><button class="link" type="button" data-twins="${mesh.userData.index}">Select all ${twinsOf(mesh).length}</button></dd>` : ""}<dt>Volume</dt><dd>${fmtVol(st.volume)}</dd><dt>Surface area</dt><dd>${fmtArea(st.area)}</dd>
      <dt>Faces</dt><dd>${mesh.userData.faces.length}</dd></dl>`);
  }

  function groupReadout({ ids, name }) {
    const list = ids.map((i) => meshes[i]);
    const box = new THREE.Box3(); list.forEach((m) => box.union(m.geometry.boundingBox));
    const vol = list.reduce((t, m) => t + meshStats(m).volume, 0);
    const same = list.every((m) => sigOf(m) === sigOf(list[0]));
    const each = list[0].geometry.boundingBox.getSize(new THREE.Vector3());
    setReadout(`${head(same ? `${list.length} identical` : "Group", name)}
      <div class="big">${sizeStr(box.getSize(new THREE.Vector3()))} <small>${unit}</small></div>
      <dl class="kv"><dt>Parts</dt><dd>${list.length}</dd>${same ? `<dt>Each part</dt><dd>${sizeStr(each)}</dd>` : ""}
      <dt>Total volume</dt><dd>${fmtVol(vol)}</dd></dl>`);
  }

  function describeFace(f) {
    if (f.type === "cyl") {
      const full = f.sweep > 350;
      const what = full ? (f.inner ? "Hole" : "Shaft / boss") : (f.inner ? "Inside radius" : "Outside radius");
      const main = full ? `Ø ${fmtLen(2 * f.radius)}` : `R ${fmtLen(f.radius)}`;
      return { what, main, rows: [["Radius", fmtLen(f.radius)], ["Diameter", fmtLen(2 * f.radius)], ["Depth / length", fmtLen(f.length)],
        ["Arc", full ? "full 360°" : f.sweep.toFixed(1) + "°"], ["Axis", vecStr(f.axis)], ["Centre", ptStr(f.center)]] };
    }
    const s = f.box.getSize(new THREE.Vector3());
    if (f.type === "plane") {
      return { what: "Flat face", main: fmtArea(f.area), rows: [["Extent X · Y · Z", `${num(s.x)} · ${num(s.y)} · ${num(s.z)}`], ["Normal", vecStr(f.normal)]] };
    }
    return { what: "Curved face", main: fmtArea(f.area), rows: [["Extent X · Y · Z", `${num(s.x)} · ${num(s.y)} · ${num(s.z)}`]] };
  }
  const vecStr = (v) => `${v.x.toFixed(3)}, ${v.y.toFixed(3)}, ${v.z.toFixed(3)}`;
  const ptStr = (p) => `${num(p.x)}, ${num(p.y)}, ${num(p.z)}`;

  function faceReadout(mesh, f) {
    const d = describeFace(f);
    setReadout(`${head(d.what, mesh.userData.name + (f.faces && f.faces.length > 1 ? ` · ${f.faces.length} faces` : " · face " + f.faceNo))}
      <div class="big">${d.main}</div>
      <dl class="kv">${d.rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}
      <dt>Area</dt><dd>${fmtArea(f.area)}</dd></dl>`);
  }

  function distReadout() {
    if (picks.length === 1) {
      setReadout(`${head("Distance", "Point 1 set")}<div class="hint">Tap the second point.</div>`);
      return;
    }
    const [A, B] = picks;
    const dv = B.point.clone().sub(A.point);
    let special = null;
    const fa = A.face, fb = B.face;
    if (fa && fb && fa.type === "plane" && fb.type === "plane" && Math.abs(fa.normal.dot(fb.normal)) > 0.9995) {
      const gap = Math.abs(fb.point.clone().sub(fa.point).dot(fa.normal));
      if (gap > 1e-4) special = ["Face to face (parallel)", gap]; // same plane: fall back to point to point
    } else if (fa && fb && fa.type === "cyl" && fb.type === "cyl" && Math.abs(fa.axis.dot(fb.axis)) > 0.9995) {
      const w = fb.center.clone().sub(fa.center); w.addScaledVector(fa.axis, -w.dot(fa.axis));
      special = ["Centre to centre", w.length()];
    } else if (fa && fb && ((fa.type === "cyl" && fb.type === "plane") || (fa.type === "plane" && fb.type === "cyl"))) {
      const cy = fa.type === "cyl" ? fa : fb, pl = fa.type === "plane" ? fa : fb;
      if (Math.abs(cy.axis.dot(pl.normal)) < 0.0005) special = ["Hole centre to face", Math.abs(cy.center.clone().sub(pl.point).dot(pl.normal))];
    }
    const pp = dv.length();
    label3d.textContent = fmtLen(special ? special[1] : pp, unit === "in" ? 4 : 2);
    setReadout(`${head("Distance", special ? special[0] : "Point to point")}
      <div class="big">${fmtLen(special ? special[1] : pp)}</div>
      <dl class="kv">${special ? `<dt>Point to point</dt><dd>${fmtLen(pp)}</dd>` : ""}
      <dt>ΔX</dt><dd>${fmtLen(Math.abs(dv.x))}</dd><dt>ΔY</dt><dd>${fmtLen(Math.abs(dv.y))}</dd><dt>ΔZ</dt><dd>${fmtLen(Math.abs(dv.z))}</dd></dl>`);
  }

  // ---------- tapping ----------
  const raycaster = new THREE.Raycaster();
  function pick(clientX, clientY) {
    const r = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.intersectObjects(meshes.filter((m) => m.visible), false)[0];
    return hit ? { hit, ndc, rect: r } : null;
  }
  function snapPoint(res, clientX, clientY) {
    const { hit, rect } = res, mesh = hit.object, P = mesh.geometry.attributes.position.array;
    let best = null, bestD = 28;
    [hit.face.a, hit.face.b, hit.face.c].forEach((vi) => {
      const p = new THREE.Vector3().fromArray(P, 3 * vi);
      const s = p.clone().project(camera);
      const sx = (s.x + 1) / 2 * rect.width + rect.left, sy = (1 - s.y) / 2 * rect.height + rect.top;
      const d = Math.hypot(sx - clientX, sy - clientY);
      if (d < bestD) { bestD = d; best = p; }
    });
    return best || hit.point.clone();
  }
  function onTap(x, y) {
    if (!meshes.length) return;
    const res = pick(x, y);
    if (!res) { if (mode !== "dist") { clearSelection(); showHint(); } return; }
    const mesh = res.hit.object, fi = faceIndexOf(mesh, res.hit.faceIndex);
    if (mode === "part") { selectMesh(mesh); partReadout(mesh); return; }
    let f = fi >= 0 ? analyzeFace(mesh, fi) : null;
    if (f && f.type === "cyl") f = holeGroup(mesh, fi, f);
    if (mode === "face") { if (f) { highlightFace(mesh, f.faces || fi); currentFace = [mesh, f]; faceReadout(mesh, f); } return; }
    // distance
    if (picks.length >= 2) clearMarks();
    const p = snapPoint(res, x, y);
    picks.push({ point: p, face: f, mesh });
    addDot(p);
    if (picks.length === 2) { addLine(picks[0].point, picks[1].point); labelPos = picks[0].point.clone().add(picks[1].point).multiplyScalar(0.5); label3d.hidden = false; }
    distReadout();
    requestRender();
  }
  let down = null;
  renderer.domElement.addEventListener("pointerdown", (e) => {
    down = down ? { multi: true } : { x: e.clientX, y: e.clientY, t: performance.now(), id: e.pointerId };
  });
  renderer.domElement.addEventListener("pointerup", (e) => {
    const d = down; down = null;
    if (!d || d.multi || d.id !== e.pointerId) return;
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < 8 && performance.now() - d.t < 400) onTap(e.clientX, e.clientY);
  });
  renderer.domElement.addEventListener("pointercancel", () => { down = null; });

  document.querySelectorAll(".mode").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll(".mode").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    clearSelection(); clearMarks(); showHint();
  }));

  $("unitBtn").textContent = unit;
  $("unitBtn").addEventListener("click", () => {
    unit = unit === "mm" ? "in" : "mm"; store.set("unit", unit); $("unitBtn").textContent = unit;
    showOverallChip();
    if (mode === "dist" && picks.length) distReadout();
    else if (selectedGroup) groupReadout(selectedGroup);
    else if (selected.length) partReadout(selected[0]);
    else if (faceOverlay && currentFace) faceReadout(...currentFace);
    else showHint();
  });

  // ---------- parts tree ----------
  function meshesUnder(node) {
    const out = [...node.meshes];
    node.children.forEach((c) => out.push(...meshesUnder(c)));
    return out;
  }
  function makeRow(name, ids, depth, isAsm, meshIdx) {
    const row = document.createElement(isAsm ? "summary" : "div");
    row.className = "row" + (isAsm ? " asm" : "");
    row.style.paddingLeft = (8 + depth * 14) + "px";
    row.dataset.ids = ids.join(",");
    if (meshIdx !== undefined) row.dataset.mesh = meshIdx;
    row.innerHTML = `<span class="caret">${isAsm ? "▶" : ""}</span><input type="checkbox" checked aria-label="Show ${esc(name)}"><span class="nm">${esc(name)}</span>${isAsm ? `<span class="ct">${ids.length}</span>` : ""}<button type="button">Isolate</button>`;
    return row;
  }
  function buildTree(root) {
    const tree = $("tree"); tree.innerHTML = "";
    let top = root;
    while (top && !top.meshes.length && top.children.length === 1 && !top.name) top = top.children[0];
    const add = (node, depth, parent) => {
      const ids = meshesUnder(node);
      if (!ids.length) return;
      const leaf = !node.children.length && node.meshes.length === 1;
      if (leaf) {
        const mi = node.meshes[0];
        parent.appendChild(makeRow(node.name || meshes[mi].userData.name, ids, depth, false, mi));
        if (node.name) meshes[mi].userData.name = node.name;
        return;
      }
      // CAD exporters often name a plain bag of solids "COMPOUND" or "SOLID"; show it as "Bodies" and open it
      const generic = /^(compound|compsolid|solid|shell)$/i.test((node.name || "").trim());
      const det = document.createElement("details");
      det.open = depth < 1 || generic;
      det.appendChild(makeRow(generic ? "Bodies" : node.name || "Assembly", ids, depth, true));
      const leaves = [];
      node.children.forEach((c) => {
        if (!c.children.length && c.meshes.length === 1) {
          if (c.name) meshes[c.meshes[0]].userData.name = c.name;
          leaves.push(c.meshes[0]);
        } else add(c, depth + 1, det);
      });
      leaves.push(...node.meshes);
      addLeaves(leaves, depth + 1, det);
      parent.appendChild(det);
    };
    // list single parts, putting identical copies together under one collapsible row
    const addLeaves = (list, depth, parent) => {
      const groups = new Map();
      list.forEach((mi) => { const k = sigOf(meshes[mi]); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(mi); });
      // groups first (largest first), then single bodies in file order
      const ordered = [...groups.values()].sort((a, b) => (b.length > 1) - (a.length > 1) || (b.length > 1 ? b.length - a.length : 0));
      ordered.forEach((g) => {
        if (g.length < 2) { parent.appendChild(makeRow(meshes[g[0]].userData.name, g, depth, false, g[0])); return; }
        const names = new Set(g.map((i) => meshes[i].userData.name));
        const d = meshes[g[0]].geometry.boundingBox.getSize(new THREE.Vector3()).toArray().sort((a, b) => b - a)
          .map((v) => (v >= 100 ? v.toFixed(0) : v.toFixed(1))).join(" × ");
        const nm = names.size === 1 ? [...names][0] : `${d} mm`;
        const det = document.createElement("details");
        const row = makeRow(nm, g, depth, true);
        row.classList.add("grp");
        row.querySelector(".ct").textContent = "×" + g.length;
        det.appendChild(row);
        g.forEach((mi) => det.appendChild(makeRow(meshes[mi].userData.name, [mi], depth + 1, false, mi)));
        parent.appendChild(det);
      });
    };
    if (top) add(top, 0, tree);
    if (!tree.children.length) tree.innerHTML = `<div class="hint" style="padding:14px">No parts in this file.</div>`;
  }
  function syncChecks() {
    document.querySelectorAll(".tree .row").forEach((row) => {
      const ids = row.dataset.ids.split(",").map(Number);
      const vis = ids.filter((i) => meshes[i].visible).length;
      const cb = row.querySelector("input");
      cb.checked = vis === ids.length; cb.indeterminate = vis > 0 && vis < ids.length;
    });
  }
  function setVisible(ids, on) {
    ids.forEach((i) => { meshes[i].visible = on; });
    if (selected.some((m) => !m.visible)) { clearSelection(); showHint(); }
    syncChecks(); requestRender();
  }
  $("tree").addEventListener("click", (e) => {
    const row = e.target.closest(".row"); if (!row) return;
    const ids = row.dataset.ids.split(",").map(Number);
    if (e.target.matches("input")) { e.stopPropagation(); setVisible(ids, e.target.checked); return; }
    if (e.target.matches("button")) {
      e.preventDefault();
      meshes.forEach((m, i) => { m.visible = ids.includes(i); });
      syncChecks(); frame(); closeDrawer(); return;
    }
    if (row.tagName === "SUMMARY") {
      if (mode !== "part") document.querySelector('.mode[data-mode="part"]').click();
      selectGroup(ids, row.querySelector(".nm").textContent, row);
      return; // the summary still opens/closes its list
    }
    if (row.dataset.mesh !== undefined) {
      const m = meshes[+row.dataset.mesh];
      if (!m.visible) setVisible([+row.dataset.mesh], true);
      if (mode !== "part") document.querySelector('.mode[data-mode="part"]').click();
      selectMesh(m); partReadout(m);
    }
  });
  $("showAll").addEventListener("click", () => { setVisible(meshes.map((_, i) => i), true); frame(); });
  $("hideAll").addEventListener("click", () => setVisible(meshes.map((_, i) => i), false));
  const openDrawer = () => { $("drawer").classList.add("open"); $("scrim").hidden = false; };
  const closeDrawer = () => { $("drawer").classList.remove("open"); $("scrim").hidden = true; };
  $("partsBtn").addEventListener("click", openDrawer);
  $("closeDrawer").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);

  // ---------- loading files ----------
  let occtPromise = null;
  const getOcct = () => (occtPromise ||= occtimportjs({ locateFile: (p) => p }));
  function busy(text) { $("overlayText").innerHTML = text; overlay.hidden = false; }
  function done() { overlay.hidden = true; }
  const nextPaint = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  async function loadBuffer(buf, name) {
    const ext = name.toLowerCase().split(".").pop();
    if (!["stp", "step", "igs", "iges", "brep", "brp"].includes(ext)) {
      busy(`<div class="err">${esc(name)} isn't a STEP file.</div><div class="hint">Pick a .stp or .step file. IGES and BREP work too.</div><button class="btn" data-dismiss>OK</button>`);
      return;
    }
    busy(`<div class="spin"></div><div>Reading <b>${esc(name)}</b>…</div><div class="hint">${(buf.byteLength / 1048576).toFixed(1)} MB. Large assemblies can take a minute on a phone.</div>`);
    await nextPaint();
    try {
      const occt = await getOcct();
      await nextPaint();
      const params = { linearUnit: "millimeter", linearDeflectionType: "bounding_box_ratio", linearDeflection: 0.0008, angularDeflection: 0.35 };
      const data = new Uint8Array(buf);
      const res = ext === "stp" || ext === "step" ? occt.ReadStepFile(data, params)
        : ext === "igs" || ext === "iges" ? occt.ReadIgesFile(data, params) : occt.ReadBrepFile(data, params);
      if (!res || !res.success || !res.meshes.length) throw new Error("The file opened but has no solid geometry in it.");
      buildModel(res);
      $("fileName").textContent = name;
      showOverallChip();
      frame(...VIEWS.iso);
      showHint();
      done();
    } catch (err) {
      console.error(err);
      busy(`<div class="err">Couldn't open ${esc(name)}.</div><div class="hint">${esc(err && err.message || err)}</div><button class="btn" data-dismiss>OK</button>`);
    }
  }
  overlay.addEventListener("click", (e) => {
    if (e.target.closest("[data-open]")) $("fileInput").click();
    if (e.target.closest("[data-dismiss]")) { if (meshes.length) done(); else showStart(); }
  });

  // first screen: nothing loaded yet
  function showStart() {
    setReadout("");
    busy(`<div class="start-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/></svg></div>
      <h2 class="start-title">Open a STEP file</h2>
      <div class="hint">Choose a .stp or .step file from your phone. IGES and BREP work too.</div>
      <button class="btn primary start-btn" type="button" data-open>Choose file</button>
      <div class="hint start-note">Your file is opened on this device and is never uploaded.</div>`);
  }

  $("openBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", async (e) => {
    const f = e.target.files[0]; if (!f) return;
    await loadBuffer(await f.arrayBuffer(), f.name);
    e.target.value = "";
  });
  stage.addEventListener("dragover", (e) => { e.preventDefault(); stage.classList.add("drop"); });
  stage.addEventListener("dragleave", () => stage.classList.remove("drop"));
  stage.addEventListener("drop", async (e) => {
    e.preventDefault(); stage.classList.remove("drop");
    const f = e.dataTransfer.files[0]; if (f) await loadBuffer(await f.arrayBuffer(), f.name);
  });

  // ---------- render loop ----------
  function tick() {
    requestAnimationFrame(tick);
    if (controls.update()) dirty = true;
    if (!dirty) return;
    dirty = false;
    const s = camera.position.distanceTo(controls.target) * 0.006;
    markGroup.children.forEach((m) => { if (m.userData.dot) m.scale.setScalar(s); });
    renderer.render(scene, camera);
    if (labelPos && !label3d.hidden) {
      const p = labelPos.clone().project(camera);
      label3d.style.left = ((p.x + 1) / 2 * stage.clientWidth) + "px";
      label3d.style.top = ((1 - p.y) / 2 * stage.clientHeight) + "px";
    }
  }

  applyTheme();
  resize();
  tick();
  showStart();
  // warm up the CAD engine in the background so the first file opens faster
  getOcct().catch((err) => console.error(err));
})();
