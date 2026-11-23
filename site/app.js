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

  const markGroup = new THREE.Group(); // each open file has its own group, added while its tab is active
  scene.add(markGroup);
  let edgeMat = new THREE.LineBasicMaterial({ color: 0x2b3742, transparent: true, opacity: 0.55 });
  let markColor = new THREE.Color(0xe0560b), accentColor = new THREE.Color(0x1f5fd1);

  let dirty = true;
  const requestRender = () => { dirty = true; };
  controls.addEventListener("change", requestRender);

  function resize() {
    const w = stage.clientWidth, h = stage.clientHeight;
    renderer.setSize(w, h, false);
    const p = panes()[0]; // whole stage, or one half while comparing
    camera.aspect = p.w / Math.max(p.h, 1);
    camera.updateProjectionMatrix();
    layoutPanes();
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
    dimMat.color.set(v("--dim") || "#2f4a63");
    markGroup.children.forEach((m) => m.material && m.material.color && m.material.color.copy(markColor));
    if (faceOverlay) faceOverlay.material.color.copy(accentColor);
    selected.forEach((m) => m.material.emissive.copy(accentColor));
    requestRender();
  }
  new MutationObserver(applyTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // theme button: Light (default) <-> Dark; the icon shows the current mode
  const THEMES = {
    light: { label: "Light mode", icon: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8"/>' },
    dark: { label: "Dark mode", icon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>' },
  };
  const ORDER = ["light", "dark"];
  let theme = store.get("theme", "light"); if (!THEMES[theme]) theme = "light"; // older saved "auto" becomes light
  function setTheme(t) {
    theme = t; store.set("theme", t);
    if (window.stepTheme) window.stepTheme.apply(t);
    const b = $("themeBtn");
    b.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${THEMES[t].icon}</svg>`;
    b.title = THEMES[t].label;
    b.setAttribute("aria-label", `${THEMES[t].label}. Tap for ${t === "light" ? "dark" : "light"} mode.`);
  }
  $("themeBtn").addEventListener("click", () => setTheme(ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]));
  setTheme(theme);

  // ---------- model state ----------
  // docs: one per open file (tab). `meshes` always points at the active file's meshes.
  let docs = [], active = null;
  let meshes = [];          // THREE.Mesh per occt mesh
  let modelBox = new THREE.Box3();
  let modelRadius = 100;
  let mode = "part";
  let selected = [], selectedGroup = null, faceOverlay = null;
  let picks = [];
  let currentFace = null;
  let measureLine = null, labelPos = null;

  function buildDoc(res, file) {
    const doc = { name: file.name, size: file.size, root: res.root, group: new THREE.Group(), meshes: [],
      tree: document.createElement("div"), treeBuilt: false, view: null };
    res.meshes.forEach((om, i) => {
      const g = new THREE.BufferGeometry();
      const f32 = (a) => (a instanceof Float32Array ? a : new Float32Array(a)); // the worker already sends typed arrays
      g.setAttribute("position", new THREE.BufferAttribute(f32(om.attributes.position.array), 3));
      const ix = om.index.array;
      g.setIndex(new THREE.BufferAttribute(ix instanceof Uint32Array ? ix : Uint32Array.from(ix), 1));
      if (om.attributes.normal) g.setAttribute("normal", new THREE.BufferAttribute(f32(om.attributes.normal.array), 3));
      else g.computeVertexNormals();
      // The worker sends bounds, volume/area and outline edges ready-made (om.prep), so the page
      // doesn't freeze working them out. Without a worker they're computed here as before.
      const pr = om.prep;
      if (pr) {
        g.boundingBox = new THREE.Box3(new THREE.Vector3(pr.box[0], pr.box[1], pr.box[2]), new THREE.Vector3(pr.box[3], pr.box[4], pr.box[5]));
        g.boundingSphere = new THREE.Sphere(new THREE.Vector3(pr.sphere[0], pr.sphere[1], pr.sphere[2]), pr.sphere[3]);
      } else { g.computeBoundingBox(); g.computeBoundingSphere(); }
      const c = om.color || (om.brep_faces.find((f) => f.color) || {}).color || [0.72, 0.75, 0.79];
      const mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(c[0], c[1], c[2]), metalness: 0.15, roughness: 0.6, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      });
      const mesh = new THREE.Mesh(g, mat);
      mesh.userData = { index: i, name: om.name || `Body ${i + 1}`, faces: om.brep_faces, faceCache: new Map() };
      if (pr) mesh.userData.stats = { volume: pr.volume, area: pr.area }; // read by meshStats()
      let eg;
      if (pr) {
        eg = new THREE.BufferGeometry(); eg.setAttribute("position", new THREE.BufferAttribute(pr.edges, 3));
        eg.boundingSphere = g.boundingSphere.clone(); // edges lie on the part, so its sphere covers them
      }
      else eg = new THREE.EdgesGeometry(g, 30);
      const edges = new THREE.LineSegments(eg, edgeMat);
      edges.raycast = () => {};
      mesh.add(edges);
      doc.group.add(mesh);
      doc.meshes.push(mesh);
      // memory estimate: model data kept by the page, plus the same again on the graphics chip
      const bytes = (geo) => Object.values(geo.attributes).concat(geo.index || []).reduce((t, a) => t + a.array.byteLength, 0);
      doc.mem = (doc.mem || 0) + 2 * (bytes(g) + bytes(eg));
    });
    return doc;
  }

  // ---------- memory warning ----------
  // With two or more files open, warn once their models together are likely to strain the device.
  // Phones (touch screens) get a lower limit; a phone that reports little memory, lower still.
  const MB = (b) => Math.round(b / 1048576);
  const memText = (b) => (b < 1048576 ? "under 1 MB" : `about ${MB(b)} MB`);
  const memLimit = (() => {
    let mb = matchMedia("(pointer: coarse)").matches ? 300 : 1000;
    if (navigator.deviceMemory && navigator.deviceMemory <= 2) mb = Math.min(mb, 150);
    return mb * 1048576;
  })();
  const memwarn = $("memwarn");
  function checkMemory(fromLoad) {
    const total = docs.reduce((t, d) => t + (d.mem || 0), 0);
    if (docs.length < 2 || total < memLimit) { memwarn.hidden = true; return; }
    if (memwarn.hidden && !fromLoad) return; // dismissed: wait for the next file to be opened
    const big = docs.reduce((a, b) => (b.mem > a.mem ? b : a));
    memwarn.innerHTML = `<div><b>${docs.length} files open, about ${MB(total)} MB of memory.</b>
      On a phone this can make the page reload and lose your open tabs. Close files you're not using.
      <span class="mw-big">Largest: ${esc(big.name)} (${memText(big.mem)})</span></div>
      <button type="button" class="mw-x" aria-label="Dismiss warning">×</button>`;
    memwarn.hidden = false;
  }
  memwarn.addEventListener("click", (e) => { if (e.target.closest(".mw-x")) memwarn.hidden = true; });

  // ---------- compare (two files side by side) ----------
  // While comparing, the canvas is split in two: the active tab (A) and another tab (B). Both
  // use the same view: camB copies the main camera's angle and zoom but centres on B's own model,
  // so two files placed at different coordinates still line up. Viewing only: tapping parts and
  // the Part/Face/Distance modes are off until compare ends.
  let cmp = null, lastActive = null; // cmp = { b: doc }
  const camB = new THREE.PerspectiveCamera(35, 1, 0.1, 1e6);
  function panes() {
    const W = stage.clientWidth, H = stage.clientHeight;
    if (!cmp) return [{ x: 0, y: 0, w: W, h: H }];
    if (H > W) { const h = Math.floor(H / 2); return [{ x: 0, y: 0, w: W, h }, { x: 0, y: h, w: W, h: H - h }]; } // phone upright: top / bottom
    const w = Math.floor(W / 2); return [{ x: 0, y: 0, w, h: H }, { x: w, y: 0, w: W - w, h: H }];          // wide: left / right
  }
  const paneNames = [0, 1].map(() => { const el = document.createElement("div"); el.className = "panename"; el.hidden = true; return el; });
  const paneDiv = document.createElement("div"); paneDiv.className = "panediv"; paneDiv.hidden = true;
  renderer.domElement.after(paneDiv, ...paneNames);
  function layoutPanes() {
    const ps = panes();
    paneDiv.hidden = !cmp; paneNames.forEach((el) => (el.hidden = !cmp));
    if (!cmp) return;
    [active, cmp.b].forEach((d, i) => {
      const el = paneNames[i], p = ps[i];
      el.innerHTML = `<b>${i ? "B" : "A"}</b>${esc(d.name)}`;
      el.style.left = p.x + 12 + "px"; el.style.top = p.y + 10 + "px"; el.style.maxWidth = Math.max(p.w - 90, 80) + "px";
    });
    const b = ps[1];
    Object.assign(paneDiv.style, b.x ? { left: b.x + "px", top: "0px", width: "1px", height: "100%" } : { left: "0px", top: b.y + "px", width: "100%", height: "1px" });
  }
  const visibleBox = (list) => { const b = new THREE.Box3(); list.forEach((m) => { if (m.visible) b.union(m.geometry.boundingBox); }); return b; };
  function enterCompare(b) {
    if (!active || !b || b === active) return;
    if (cut.on) setCut(false);
    clearSelection(); clearMarks();
    if (cmp) scene.remove(cmp.b.group);
    cmp = { b }; b.group.visible = false; scene.add(b.group);
    document.querySelector(".app").classList.add("comparing");
    resize(); frame(); compareReadout(); renderTabs();
  }
  function exitCompare() {
    if (!cmp) return;
    scene.remove(cmp.b.group); cmp.b.group.visible = true; active.group.visible = true;
    cmp = null; renderer.setScissorTest(false);
    document.querySelector(".app").classList.remove("comparing");
    resize(); frame(); showHint(); renderTabs();
  }
  function compareReadout() {
    if (!cmp) return;
    const A = visibleBox(meshes), B = visibleBox(cmp.b.meshes);
    const size = (bx) => (bx.isEmpty() ? null : bx.getSize(new THREE.Vector3()));
    const sa = size(A), sb = size(B);
    const delta = (a, b) => { const d = b - a; return Math.abs(d) < 5e-4 ? "same" : (d > 0 ? "+" : "−") + num(Math.abs(d)); };
    const vol = (list) => list.reduce((t, m) => t + (m.visible ? meshStats(m).volume : 0), 0);
    const count = (list) => list.filter((m) => m.visible).length;
    const rows = !sa || !sb ? `<dt>Note</dt><dd>${!sa ? "A" : "B"} has no parts shown</dd>` : ["x", "y", "z"].map((k) =>
      `<dt>${k.toUpperCase()}</dt><dd>${num(sa[k])} → ${num(sb[k])} (${delta(sa[k], sb[k])})</dd>`).join("");
    setReadout(`${head("B vs A", `${active.name} ↔ ${cmp.b.name}`)}
      <div class="big cmpbig">${sa && sb ? ["x", "y", "z"].map((k) => `<span>Δ${k.toUpperCase()} ${delta(sa[k], sb[k])}</span>`).join("") + `<small>${unit}</small>` : "–"}</div>
      <dl class="kv">${rows}<dt>Parts shown</dt><dd>${count(meshes)} → ${count(cmp.b.meshes)}</dd>
      <dt>Total volume</dt><dd>${fmtVol(vol(meshes))} → ${fmtVol(vol(cmp.b.meshes))}</dd>
      <dt>Weight</dt><dd>${fmtMass(gramsOf(meshes.filter((m) => m.visible)))} → ${fmtMass(gramsOf(cmp.b.meshes.filter((m) => m.visible)))}</dd></dl>`);
  }
  function renderCompare() {
    const [pa, pb] = panes(), H = stage.clientHeight;
    // camera B: same angle, zoom and pan as A, but around B's own centre
    const ca = visibleBox(meshes).getCenter(new THREE.Vector3()), cbx = visibleBox(cmp.b.meshes);
    const cb = cbx.isEmpty() ? ca : cbx.getCenter(new THREE.Vector3());
    const tB = cb.clone().add(controls.target.clone().sub(ca));
    camB.fov = camera.fov; camB.aspect = pb.w / Math.max(pb.h, 1); camB.near = camera.near; camB.far = camera.far;
    camB.up.copy(camera.up); camB.position.copy(tB).add(camera.position.clone().sub(controls.target)); camB.lookAt(tB);
    camB.updateProjectionMatrix(); camB.updateMatrixWorld(); // labels are placed before the render updates it
    renderer.setScissorTest(true);
    [[pa, camera, active, dimLabels], [pb, camB, cmp.b, dimLabelsB]].forEach(([p, cam, doc, labels]) => {
      active.group.visible = doc === active; cmp.b.group.visible = doc === cmp.b;
      drawDims(doc.meshes, cam, p, labels);
      const y = H - p.y - p.h; renderer.setViewport(p.x, y, p.w, p.h); renderer.setScissor(p.x, y, p.w, p.h);
      renderer.render(scene, cam);
    });
    renderer.setScissorTest(false);
    active.group.visible = true; cmp.b.group.visible = false;
  }

  // ---------- tabs ----------
  function activate(doc) {
    if (active === doc) return;
    if (cmp) exitCompare();
    lastActive = active;
    clearSelection(); clearMarks();
    if (active) {
      active.view = { pos: camera.position.clone(), target: controls.target.clone(), up: camera.up.clone(), near: camera.near, far: camera.far };
      scene.remove(active.group); active.tree.remove();
    }
    active = doc; meshes = doc.meshes;
    scene.add(doc.group);
    if (!doc.treeBuilt) { buildTree(doc.root, doc.tree); doc.treeBuilt = true; }
    $("tree").appendChild(doc.tree);
    relabelDims(); syncChecks(); updateMatTags();
    $("fileName").textContent = doc.name;
    const v = doc.view;
    if (v) {
      camera.position.copy(v.pos); camera.up.copy(v.up); camera.near = v.near; camera.far = v.far;
      camera.updateProjectionMatrix(); controls.target.copy(v.target); controls.update();
      updateModelBox();
    } else frame(...VIEWS.iso);
    showHint(); renderTabs(); requestRender();
  }
  function closeDoc(doc) {
    const i = docs.indexOf(doc); if (i < 0) return;
    if (cmp && (doc === active || doc === cmp.b)) exitCompare();
    if (active === doc) {
      clearSelection(); clearMarks();
      scene.remove(doc.group); doc.tree.remove();
      active = null; meshes = [];
    }
    doc.meshes.forEach((m) => { m.geometry.dispose(); m.material.dispose(); m.children.forEach((c) => c.geometry.dispose()); });
    docs.splice(i, 1);
    if (!active) {
      if (docs.length) activate(docs[Math.min(i, docs.length - 1)]);
      else { $("fileName").textContent = "No file"; showStart(); }
    }
    renderTabs(); requestRender(); checkMemory(false);
  }
  function renderTabs() {
    const el = $("tabs");
    el.hidden = docs.length < 2;
    const ab = (d) => (!cmp ? "" : d === active ? '<b class="ab">A</b>' : d === cmp.b ? '<b class="ab">B</b>' : "");
    el.innerHTML = docs.map((d, i) => `<span class="tab${d === active ? " on" : cmp && d === cmp.b ? " onb" : ""}"><button class="tn" type="button" role="tab" aria-selected="${d === active}" data-tab="${i}" title="${esc(d.name)}${d.mem ? ` · ${memText(d.mem)} of memory` : ""}">${ab(d)}${esc(d.name)}</button><button class="tx" type="button" data-close="${i}" aria-label="Close ${esc(d.name)}">×</button></span>`).join("")
      + (docs.length >= 2 ? `<button class="cmp" type="button" data-compare aria-pressed="${!!cmp}">${cmp ? "End compare" : "Compare"}</button>` : "");
    const on = el.querySelector(".on"); if (on && !el.hidden) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  $("tabs").addEventListener("click", (e) => {
    const t = e.target.closest("[data-tab]"), x = e.target.closest("[data-close]"), c = e.target.closest("[data-compare]");
    if (c) {
      if (cmp) exitCompare();
      else enterCompare(lastActive && lastActive !== active && docs.includes(lastActive) ? lastActive : docs.find((d) => d !== active));
    } else if (t) {
      const d = docs[+t.dataset.tab];
      if (!cmp) activate(d);
      else if (d !== active && d !== cmp.b) enterCompare(d); // while comparing, a third tab replaces B
    } else if (x) closeDoc(docs[+x.dataset.close]);
  });

  function updateModelBox(only) {
    modelBox = new THREE.Box3();
    (only || meshes).forEach((m) => { if (m.visible) modelBox.union(m.geometry.boundingBox); });
    if (modelBox.isEmpty()) modelBox.set(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50));
    modelRadius = Math.max(modelBox.getBoundingSphere(new THREE.Sphere()).radius, 1);
  }

  // ---------- overall dimensions ----------
  // Dimension lines for the X, Y and Z size of the shown parts, drawn along the box edges on the
  // side facing the camera (re-chosen as the view turns), with extension lines, arrowheads and a
  // label each. A dimension seen end-on (e.g. Z from the top) is hidden.
  let dimsOn = store.get("dims", "1") === "1";
  const dimMat = new THREE.LineBasicMaterial({ color: 0x2f4a63, depthTest: false, transparent: true, opacity: 0.9 });
  const dimLines = new THREE.LineSegments(new THREE.BufferGeometry(), dimMat);
  dimLines.renderOrder = 8; dimLines.frustumCulled = false;
  scene.add(dimLines);
  const makeLabels = () => ["X", "Y", "Z"].map(() => { const el = document.createElement("div"); el.className = "dimlabel"; el.hidden = true; return el; });
  const dimLabels = makeLabels(), dimLabelsB = makeLabels(); // B: second pane while comparing
  renderer.domElement.after(...dimLabels, ...dimLabelsB);
  const V3 = (x, y, z) => new THREE.Vector3(x, y, z);
  // rect = the pane (in stage pixels) the camera draws into
  const toScreen = (p, cam, rect) => { const q = p.clone().project(cam); return { x: rect.x + (q.x + 1) / 2 * rect.w, y: rect.y + (1 - q.y) / 2 * rect.h, behind: q.z > 1 }; };
  const viewsEl = document.querySelector(".views");
  const viewsBox = () => ({ left: viewsEl.offsetLeft, bottom: viewsEl.offsetTop + viewsEl.offsetHeight });
  function drawDims(list, cam, rect, labels) {
    const box = new THREE.Box3();
    if (dimsOn) list.forEach((m) => { if (m.visible) box.union(m.geometry.boundingBox); });
    const camera = cam, dimLabels = labels; // the code below draws for this pane
    const toScr = (p) => toScreen(p, cam, rect);
    if (box.isEmpty()) { dimLines.visible = false; dimLabels.forEach((l) => (l.hidden = true)); return; }
    const mn = box.min, mx = box.max, c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const sx = camera.position.x >= c.x ? 1 : -1, sy = camera.position.y >= c.y ? 1 : -1;
    const X = sx > 0 ? mx.x : mn.x, Y = sy > 0 ? mx.y : mn.y, Yback = sy > 0 ? mn.y : mx.y;
    const gap = Math.max(size.x, size.y, size.z) * 0.1;
    const dims = [
      // each line is pushed out diagonally from its edge, so it stands clear of the model in
      // straight-on views too (e.g. X under the model in the front view)
      { axis: "X", a: V3(mn.x, Y, mn.z), b: V3(mx.x, Y, mn.z), off: V3(0, sy, -1).normalize(), len: size.x },
      { axis: "Y", a: V3(X, mn.y, mn.z), b: V3(X, mx.y, mn.z), off: V3(sx, 0, -1).normalize(), len: size.y },
      { axis: "Z", a: V3(X, Yback, mn.z), b: V3(X, Yback, mx.z), off: V3(sx, -sy, 0).normalize(), len: size.z },
    ];
    const pos = [], seg = (p, q) => pos.push(p.x, p.y, p.z, q.x, q.y, q.z);
    dims.forEach((d, i) => {
      const label = dimLabels[i];
      const A = d.a.clone().addScaledVector(d.off, gap), B = d.b.clone().addScaledVector(d.off, gap);
      const M = A.clone().lerp(B, 0.5), sa = toScr(A), sb = toScr(B), mid = toScr(M), out = toScr(M.clone().addScaledVector(d.off, gap));
      if (d.len <= 0 || Math.hypot(sb.x - sa.x, sb.y - sa.y) < 28 || sa.behind || sb.behind) { label.hidden = true; return; }
      // extension lines (box edge out a little past the dimension line), the line, arrowheads
      seg(d.a, d.a.clone().addScaledVector(d.off, gap * 1.2));
      seg(d.b, d.b.clone().addScaledVector(d.off, gap * 1.2));
      seg(A, B);
      const dir = B.clone().sub(A).normalize(), h = Math.min(gap * 0.4, d.len * 0.2);
      [[A, dir], [B, dir.clone().negate()]].forEach(([P, t]) => {
        seg(P, P.clone().addScaledVector(t, h).addScaledVector(d.off, h * 0.35));
        seg(P, P.clone().addScaledVector(t, h).addScaledVector(d.off, -h * 0.35));
      });
      const text = `<b>${d.axis}</b>${fmtLen(d.len, unit === "in" ? 3 : 2)}`;
      if (label.dataset.t !== text) { label.innerHTML = text; label.dataset.t = text; }
      // put the label beside the line, on the side away from the model
      label.hidden = false;
      let ox = out.x - mid.x, oy = out.y - mid.y; const ol = Math.hypot(ox, oy) || 1; ox /= ol; oy /= ol;
      const push = Math.abs(ox) * label.offsetWidth / 2 + Math.abs(oy) * label.offsetHeight / 2 + 4;
      const hw = label.offsetWidth / 2 + 6, hh = label.offsetHeight / 2 + 6; // keep it on screen
      const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), Math.max(lo, hi));
      const top = clamp(mid.y + oy * push, rect.y + hh, rect.y + rect.h - hh);
      // stay clear of the FIT/ISO/... buttons on the right edge where the label is level with them
      const vb = viewsBox(), rightEdge = rect.x + rect.w >= stage.clientWidth - 1 && top - hh < vb.bottom ? vb.left - 4 : rect.x + rect.w;
      label.style.left = clamp(mid.x + ox * push, rect.x + hw, rightEdge - hw) + "px";
      label.style.top = top + "px";
    });
    dimLines.geometry.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    dimLines.visible = pos.length > 0;
  }

  // ---------- camera views ----------
  function frame(dir, up) {
    updateModelBox();
    let radius = modelRadius; // while comparing, leave room for the bigger of the two models
    if (cmp) { const bb = visibleBox(cmp.b.meshes); if (!bb.isEmpty()) radius = Math.max(radius, bb.getBoundingSphere(new THREE.Sphere()).radius); }
    const center = modelBox.getCenter(new THREE.Vector3());
    const d = dir ? dir.clone().normalize() : camera.position.clone().sub(controls.target).normalize();
    const fov = THREE.MathUtils.degToRad(camera.fov);
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * camera.aspect));
    const dist = radius / Math.sin(fit / 2) * (dimsOn ? 1.5 : 1.05); // room for the dimension lines and labels
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
    if (v === "dims") {
      dimsOn = !dimsOn; store.set("dims", dimsOn ? "1" : "0");
      b.setAttribute("aria-pressed", String(dimsOn)); requestRender();
    } else if (v === "cut") { if (!cmp) setCut(!cut.on); }
    else if (v === "fit") frame(); else frame(...VIEWS[v]);
  }));

  // ---------- cut plane ----------
  // A plane through the model along X, Y or Z. Model faces and outlines beyond it are clipped
  // away; the inside walls you then look into are drawn flat in the section colour, so a cut
  // part looks solid. The dimension lines are not cut.
  renderer.localClippingEnabled = true;
  const cut = { on: false, axis: store.get("cutAxis", "x"), t: 0.5, flip: false };
  if (!["x", "y", "z"].includes(cut.axis)) cut.axis = "x";
  const cutPlane = new THREE.Plane(), cutPlanes = [cutPlane];
  const capMat = new THREE.MeshBasicMaterial({ color: 0xc8553d, side: THREE.BackSide, clippingPlanes: cutPlanes });
  const cutOutline = new THREE.LineLoop(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0xc8553d, transparent: true, opacity: 0.8, depthTest: false }));
  cutOutline.geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(12), 3));
  cutOutline.visible = false; cutOutline.frustumCulled = false; cutOutline.renderOrder = 7;
  scene.add(cutOutline);
  function cutDoc(doc, on) {
    doc.meshes.forEach((m) => {
      m.material.clippingPlanes = on ? cutPlanes : null;
      m.material.side = on ? THREE.FrontSide : THREE.DoubleSide; // back faces become the section fill
      m.material.needsUpdate = true;
      if (on && !m.userData.cap) { const c = new THREE.Mesh(m.geometry, capMat); c.raycast = () => {}; m.add(c); m.userData.cap = c; }
      if (!on && m.userData.cap) { m.remove(m.userData.cap); m.userData.cap = null; }
    });
  }
  function setCut(on) {
    cut.on = on;
    docs.forEach((d) => cutDoc(d, on));
    edgeMat.clippingPlanes = on ? cutPlanes : null; edgeMat.needsUpdate = true;
    if (faceOverlay) { faceOverlay.material.clippingPlanes = on ? cutPlanes : null; faceOverlay.material.needsUpdate = true; }
    $("cutbar").hidden = !on; cutOutline.visible = on;
    document.querySelector('.views [data-view="cut"]').setAttribute("aria-pressed", String(on));
    syncCutBar(); requestRender();
  }
  function syncCutBar() {
    document.querySelectorAll("#cutbar [data-axis]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.axis === cut.axis)));
    $("cutPos").value = Math.round(cut.t * 1000);
    $("cutFlip").setAttribute("aria-pressed", String(cut.flip));
  }
  function updateCut() { // place the plane and its outline for the shown parts (every render while on)
    const box = visibleBox(meshes); if (box.isEmpty()) return;
    const k = cut.axis, [a, b] = ["x", "y", "z"].filter((q) => q !== k);
    const span = box.max[k] - box.min[k], pos = box.min[k] + span * cut.t;
    const n = new THREE.Vector3(); n[k] = cut.flip ? 1 : -1; // keep the side the normal points to
    cutPlane.normal.copy(n); cutPlane.constant = -n[k] * pos;
    const pad = 0.04 * Math.max(span, box.max[a] - box.min[a], box.max[b] - box.min[b]);
    const arr = cutOutline.geometry.attributes.position.array;
    [[box.min[a] - pad, box.min[b] - pad], [box.max[a] + pad, box.min[b] - pad], [box.max[a] + pad, box.max[b] + pad], [box.min[a] - pad, box.max[b] + pad]]
      .forEach(([u, v], i) => { const p = new THREE.Vector3(); p[k] = pos; p[a] = u; p[b] = v; p.toArray(arr, i * 3); });
    cutOutline.geometry.attributes.position.needsUpdate = true;
  }
  $("cutbar").addEventListener("click", (e) => {
    const ax = e.target.closest("[data-axis]");
    if (ax) { cut.axis = ax.dataset.axis; store.set("cutAxis", cut.axis); }
    else if (e.target.closest("#cutFlip")) cut.flip = !cut.flip;
    else return;
    syncCutBar(); requestRender();
  });
  $("cutPos").addEventListener("input", (e) => { cut.t = e.target.value / 1000; requestRender(); });
  document.querySelector('.views [data-view="dims"]').setAttribute("aria-pressed", String(dimsOn));

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
    if (cut.on) faceOverlay.material.clippingPlanes = cutPlanes;
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

  // Movable panel: drag it (anywhere but its buttons) off the part you're looking at. It stays
  // there, kept inside the 3D view, until the page is reloaded; double-tap it to put it back.
  let rdx = 0, rdy = 0, rdrag = null, lastTap = 0, shownX = 0, shownY = 0; // shown = offset currently drawn
  function clampReadout() {
    if (readout.hidden || (!rdx && !rdy && !shownX && !shownY)) return;
    const s = stage.getBoundingClientRect(), r = readout.getBoundingClientRect();
    const bl = r.left - shownX, bt = r.top - shownY; // where it sits when not moved
    rdx = Math.max(Math.min(rdx, s.right - 4 - (bl + r.width)), s.left + 4 - bl);
    rdy = Math.max(Math.min(rdy, s.bottom - 4 - (bt + r.height)), s.top + 4 - bt);
    shownX = rdx; shownY = rdy;
    readout.style.transform = rdx || rdy ? `translate(${rdx}px, ${rdy}px)` : "";
  }
  readout.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button, a, input, select, label") || (e.pointerType === "mouse" && e.button !== 0)) return;
    rdrag = { id: e.pointerId, x: e.clientX, y: e.clientY, dx: rdx, dy: rdy, moved: false };
    readout.setPointerCapture(e.pointerId);
  });
  readout.addEventListener("pointermove", (e) => {
    const d = rdrag; if (!d || d.id !== e.pointerId) return;
    const mx = e.clientX - d.x, my = e.clientY - d.y;
    if (!d.moved && Math.hypot(mx, my) < 5) return; // small wobble = still a tap
    d.moved = true; readout.classList.add("dragging");
    rdx = d.dx + mx; rdy = d.dy + my; clampReadout();
  });
  const endDrag = (e) => {
    const d = rdrag; if (!d || d.id !== e.pointerId) return;
    rdrag = null; readout.classList.remove("dragging");
    if (d.moved || e.type === "pointercancel") return;
    const now = performance.now();
    if (now - lastTap < 350) { rdx = rdy = shownX = shownY = 0; readout.style.transform = ""; lastTap = 0; } // double-tap: back to the bottom
    else lastTap = now;
  };
  readout.addEventListener("pointerup", endDrag);
  readout.addEventListener("pointercancel", endDrag);
  new ResizeObserver(clampReadout).observe(readout); // content changed size (e.g. Details opened)
  new ResizeObserver(clampReadout).observe(stage);   // screen rotated or resized
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
    if (e.target.closest("[data-clear]")) { if (cmp) { exitCompare(); return; } clearSelection(); clearMarks(); showHint(); }
    const tw = e.target.closest("[data-twins]");
    if (tw) {
      const m = meshes[+tw.dataset.twins], ids = twinsOf(m).map((x) => x.userData.index);
      const grpRow = [...document.querySelectorAll(".tree .row.grp")].find((r) => r.dataset.ids === ids.join(","));
      selectGroup(ids, grpRow ? grpRow.querySelector(".nm").textContent : m.userData.name, grpRow);
      if (drawerOpen()) revealSelected(true);
    }
  });

  function partReadout(mesh) {
    const s = mesh.geometry.boundingBox.getSize(new THREE.Vector3());
    const st = meshStats(mesh);
    setReadout(`${head("Part", mesh.userData.name)}
      <div class="big">${sizeStr(s)} <small>${unit}</small></div>
      ${weightRow([mesh])}
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
      ${weightRow(list, "total")}
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
    const hits = raycaster.intersectObjects(meshes.filter((m) => m.visible), false);
    const hit = cut.on ? hits.find((h) => cutPlane.distanceToPoint(h.point) >= -1e-6) : hits[0]; // skip the cut-away part
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
    if (!meshes.length || cmp) return; // comparing is view-only
    const res = pick(x, y);
    if (!res) { if (mode !== "dist") { clearSelection(); showHint(); } return; }
    const mesh = res.hit.object, fi = faceIndexOf(mesh, res.hit.faceIndex);
    if (mode === "part") { selectMesh(mesh); partReadout(mesh); if (drawerOpen()) revealSelected(true); return; }
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
    relabelDims(); requestRender(); // dimension labels follow the unit
    refreshReadout();
  });
  // re-draw whatever the panel is showing (after a unit or material change)
  function refreshReadout() {
    if (cmp) compareReadout();
    else if (mode === "dist" && picks.length) distReadout();
    else if (selectedGroup) groupReadout(selectedGroup);
    else if (selected.length) partReadout(selected[0]);
    else if (faceOverlay && currentFace) faceReadout(...currentFace);
    else showHint();
  }

  // ---------- weight estimate ----------
  // Weight = volume of the 3D model x density. Each part uses its own material if one was chosen
  // for it (mesh.userData.mat, kept until reload), otherwise the default material (chosen at the
  // top of the Parts list, remembered on the device). It's an estimate: it assumes solid parts.
  const MATERIALS = [
    ["steel", "Steel", 7.85], ["stainless", "Stainless steel", 8.0], ["aluminium", "Aluminium", 2.7],
    ["cast-iron", "Cast iron", 7.2], ["brass", "Brass", 8.5], ["copper", "Copper", 8.96], ["titanium", "Titanium", 4.43],
    ["abs", "ABS plastic", 1.04], ["pla", "PLA plastic", 1.24], ["nylon", "Nylon", 1.14], ["acetal", "Acetal (POM)", 1.41],
    ["wood", "Wood (pine)", 0.5],
  ];
  let material = store.get("material", "steel");
  if (material !== "custom" && !MATERIALS.some((m) => m[0] === material)) material = "steel";
  let customDensity = parseFloat(store.get("density", "7.85")) || 7.85;
  const density = () => (material === "custom" ? customDensity : MATERIALS.find((m) => m[0] === material)[2]);
  const materialName = () => (material === "custom" ? `${customDensity} g/cm³` : MATERIALS.find((m) => m[0] === material)[1]);
  const matByKey = (k) => MATERIALS.find((m) => m[0] === k);
  const partDensity = (m) => { const o = m.userData.mat; return !o ? density() : o.key === "custom" ? o.density : matByKey(o.key)[2]; };
  const partMatName = (m) => { const o = m.userData.mat; return !o ? null : o.key === "custom" ? `${o.density} g/cm³` : matByKey(o.key)[1]; };
  const gramsOf = (list) => list.reduce((t, m) => t + (meshStats(m).volume / 1000) * partDensity(m), 0); // mm³ -> cm³ -> g
  // what a set of parts is set to: "default" (none set), a material key, or "mixed"
  const matOf = (m) => (m.userData.mat ? m.userData.mat.key + (m.userData.mat.key === "custom" ? ":" + m.userData.mat.density : "") : "default");
  function matState(list) { const s = new Set(list.map(matOf)); return s.size === 1 ? [...s][0] : "mixed"; }
  function fmtMass(g) {
    if (unit === "in") { const lb = g / 453.59237; return lb >= 1 ? lb.toFixed(lb >= 100 ? 1 : 2) + " lb" : (lb * 16).toFixed(2) + " oz"; }
    return g >= 1000 ? (g / 1000).toFixed(g >= 100000 ? 1 : 2) + " kg" : g.toFixed(g >= 100 ? 0 : 1) + " g";
  }
  const matOptions = (sel) => MATERIALS.map(([k, n, d]) => `<option value="${k}"${k === sel ? " selected" : ""}>${n} · ${d} g/cm³</option>`).join("")
    + `<option value="custom"${sel === "custom" ? " selected" : ""}>Custom density…</option>`;
  // weight line for one part or a group: total weight + a box that sets the material of those parts
  function weightRow(list, total) {
    const st = matState(list), key = st.split(":")[0];
    const opts = `<option value="default"${st === "default" ? " selected" : ""}>Default (${esc(materialName())})</option>`
      + (st === "mixed" ? `<option value="mixed" selected disabled>Mixed materials</option>` : "") + matOptions(key);
    const dens = key === "custom" ? parseFloat(st.split(":")[1]) || customDensity : null;
    const what = total ? `all ${list.length} parts` : "this part";
    return `<div class="weight"><span>${total ? "Total ≈ " : "≈ "}<b>${fmtMass(gramsOf(list))}</b></span>
      <select data-material aria-label="Material for ${what}" title="Material for ${what}">${opts}</select>
      ${dens ? `<label class="dens"><input data-density type="number" inputmode="decimal" min="0.01" step="0.01" value="${dens}" aria-label="Density for ${what}"> g/cm³</label>` : ""}</div>`;
  }
  readout.addEventListener("change", (e) => {
    const targets = selected; // the part, or all parts of the selected group
    if (e.target.matches("[data-material]")) {
      const v = e.target.value;
      targets.forEach((m) => { m.userData.mat = v === "default" ? null : v === "custom" ? { key: "custom", density: m.userData.mat?.density || customDensity } : { key: v }; });
      refreshReadout(); updateMatTags();
      if (v === "custom") readout.querySelector("[data-density]")?.focus();
    } else if (e.target.matches("[data-density]")) {
      const v = parseFloat(e.target.value);
      if (v > 0) targets.forEach((m) => { m.userData.mat = { key: "custom", density: v }; });
      refreshReadout(); updateMatTags();
    }
  });
  // default material: top of the Parts list
  function syncDefaultMat() {
    $("defMat").innerHTML = matOptions(material);
    $("defDensity").hidden = material !== "custom"; $("defDensity").value = customDensity;
  }
  $("defMat").addEventListener("change", (e) => {
    material = e.target.value; store.set("material", material); syncDefaultMat(); refreshReadout();
    if (material === "custom") $("defDensity").focus();
  });
  $("defDensity").addEventListener("change", (e) => {
    const v = parseFloat(e.target.value);
    if (v > 0) { customDensity = v; store.set("density", String(v)); }
    syncDefaultMat(); refreshReadout();
  });
  syncDefaultMat();
  // small material tag on rows whose parts have their own material
  function updateMatTags() {
    if (!active) return;
    active.tree.querySelectorAll(".row").forEach((row) => {
      const list = row.dataset.ids.split(",").map((i) => meshes[+i]), st = matState(list);
      const tag = st === "default" ? "" : st === "mixed" ? (list.some((m) => m.userData.mat) ? "mixed" : "") : partMatName(list[0]);
      const el = row.querySelector(".mt"); if (el.textContent !== tag) el.textContent = tag;
    });
  }

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
    row.innerHTML = `<span class="caret">${isAsm ? "▶" : ""}</span><input type="checkbox" checked aria-label="Show ${esc(name)}"><span class="nm">${esc(name)}</span><span class="mt"></span>${isAsm ? `<span class="ct">${ids.length}</span>` : ""}<button type="button">Isolate</button>`;
    return row;
  }
  function buildTree(root, tree) {
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
        const nm = names.size === 1 ? [...names][0] : dimsLabel(g[0]);
        const det = document.createElement("details");
        const row = makeRow(nm, g, depth, true);
        row.classList.add("grp");
        if (names.size !== 1) row.dataset.dims = g[0]; // label is a size, so it follows the unit setting
        row.querySelector(".ct").textContent = "×" + g.length;
        det.appendChild(row);
        g.forEach((mi) => det.appendChild(makeRow(meshes[mi].userData.name, [mi], depth + 1, false, mi)));
        parent.appendChild(det);
      });
    };
    if (top) add(top, 0, tree);
    if (!tree.children.length) tree.innerHTML = `<div class="hint" style="padding:14px">No parts in this file.</div>`;
  }
  // e.g. "120 × 40.5 × 6.0 mm" or "4.72 × 1.59 × 0.24 in", largest first
  function dimsLabel(mi) {
    const big = unit === "in" ? 10 : 100;
    return meshes[mi].geometry.boundingBox.getSize(new THREE.Vector3()).toArray()
      .map((v) => (unit === "in" ? v / 25.4 : v)).sort((a, b) => b - a)
      .map((v) => v.toFixed(v >= big ? (unit === "in" ? 1 : 0) : (unit === "in" ? 2 : 1))).join(" × ") + " " + unit;
  }
  function relabelDims() {
    document.querySelectorAll(".tree .row[data-dims]").forEach((row) => {
      const nm = dimsLabel(+row.dataset.dims), el = row.querySelector(".nm");
      if (selectedGroup && selectedGroup.name === el.textContent) selectedGroup.name = nm;
      el.textContent = nm;
      row.querySelector("input").setAttribute("aria-label", `Show ${nm}`);
    });
  }
  function syncChecks() {
    document.querySelectorAll(".tree .row").forEach((row) => {
      const ids = row.dataset.ids.split(",").map(Number);
      const vis = ids.filter((i) => meshes[i].visible).length;
      const cb = row.querySelector("input");
      cb.checked = vis === ids.length; cb.indeterminate = vis > 0 && vis < ids.length;
    });
    if (cmp) compareReadout(); // shown parts changed
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
    if (cmp) return; // comparing is view-only
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
  // Show the selected part's row in the parts list: open the folded lists it sits in and
  // scroll it to the middle. Used when the part was picked outside the list (3D view, links).
  const drawerOpen = () => $("drawer").classList.contains("open");
  function revealSelected(smooth) {
    const row = $("tree").querySelector(".row.sel"); if (!row) return;
    // a group's own row is its <summary>; only open the lists around it, not the group itself
    let d = (row.tagName === "SUMMARY" ? row.parentElement.parentElement : row.parentElement).closest("details");
    for (; d; d = d.parentElement.closest("details")) d.open = true;
    const tree = $("tree"), r = row.getBoundingClientRect(), t = tree.getBoundingClientRect();
    tree.scrollTo({ top: tree.scrollTop + (r.top - t.top) - (t.height - r.height) / 2, behavior: smooth ? "smooth" : "auto" });
  }
  const openDrawer = () => { $("drawer").classList.add("open"); $("scrim").hidden = false; revealSelected(false); };
  const closeDrawer = () => { $("drawer").classList.remove("open"); $("scrim").hidden = true; };
  $("partsBtn").addEventListener("click", openDrawer);
  $("closeDrawer").addEventListener("click", closeDrawer);
  $("scrim").addEventListener("click", closeDrawer);

  // ---------- loading files ----------
  // The CAD engine runs in a Web Worker (worker.js) so the page keeps responding while a file
  // is read. The worker is started from a blob: URL so it inherits this page's Content
  // Security Policy. After each file it is replaced by a fresh one, which frees its memory.
  const BASE = new URL(".", location.href).href;
  let workerUrl = null, enginePromise = null, job = null, mainOcct = null;
  function startEngine() {
    workerUrl ||= fetch("worker.js").then((r) => { if (!r.ok) throw new Error(`worker.js: HTTP ${r.status}`); return r.text(); })
      .then((src) => URL.createObjectURL(new Blob([src], { type: "text/javascript" })));
    return workerUrl.then((url) => new Promise((resolve, reject) => {
      const w = new Worker(url);
      w.onmessage = (e) => { if (e.data.ready) resolve(w); else { w.terminate(); reject(new Error(e.data.initError)); } };
      w.onerror = (e) => { e.preventDefault(); w.terminate(); reject(new Error(e.message || "The CAD engine didn't start.")); };
      w.postMessage({ init: BASE });
    }));
  }
  const engine = () => (enginePromise ||= startEngine());
  function freshEngine() {
    const old = enginePromise; enginePromise = null;
    if (old) old.then((w) => w.terminate(), () => {});
    engine().catch(() => {});
  }
  async function parse(ext, buf, params) {
    let w;
    try { w = await engine(); } catch (err) {
      // no worker support: read on the main thread as before (the page pauses while it reads)
      console.warn("CAD worker unavailable, reading on the main thread.", err);
      const occt = await (mainOcct ||= occtimportjs({ locateFile: (p) => p }));
      await nextPaint();
      const data = new Uint8Array(buf);
      return ext === "stp" || ext === "step" ? occt.ReadStepFile(data, params)
        : ext === "igs" || ext === "iges" ? occt.ReadIgesFile(data, params) : occt.ReadBrepFile(data, params);
    }
    return new Promise((resolve, reject) => {
      job = { w, reject };
      w.onmessage = (e) => { job = null; e.data.error ? reject(new Error(e.data.error)) : resolve(e.data.res); };
      w.onerror = (e) => { e.preventDefault(); job = null; reject(new Error("The CAD engine stopped. The file may be too large for this device.")); };
      w.postMessage({ ext, buf, params }, [buf]);
    });
  }
  function cancelLoad() {
    if (!job) return false;
    const j = job; job = null;
    enginePromise = null; j.w.terminate(); engine().catch(() => {});
    j.reject(Object.assign(new Error("Cancelled"), { cancelled: true }));
    return true;
  }

  function busy(text) { $("overlayText").innerHTML = text; overlay.hidden = false; }
  function done() { overlay.hidden = true; }
  const nextPaint = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));

  // Files are read one after another; each one that opens becomes a tab. Cancel stops the
  // rest of the batch and keeps the files already opened.
  const EXTS = ["stp", "step", "igs", "iges", "brep", "brp"];
  const PARAMS = { linearUnit: "millimeter", linearDeflectionType: "bounding_box_ratio", linearDeflection: 0.0008, angularDeflection: 0.35 };
  let loadSeq = 0, batch = null;
  async function loadFiles(files) {
    if (!files.length) return;
    if (batch) batch.cancelled = true;
    cancelLoad(); // new files replace a batch still being read
    const my = ++loadSeq, b = (batch = { cancelled: false });
    const opened = [], failed = [], n = files.length;
    for (let k = 0; k < n && !b.cancelled; k++) {
      const f = files[k], ext = f.name.toLowerCase().split(".").pop();
      const already = docs.find((d) => d.name === f.name && d.size === f.size);
      if (already) { opened.push(already); continue; }
      if (!EXTS.includes(ext)) {
        failed.push({ name: f.name, title: `${esc(f.name)} isn't a STEP file.`, detail: "Pick a .stp or .step file. IGES and BREP work too.", short: "not a STEP file" });
        continue;
      }
      busy(`<div class="spin"></div><div>Reading ${n > 1 ? `${k + 1} of ${n}: ` : ""}<b>${esc(f.name)}</b>…</div><div class="hint">${(f.size / 1048576).toFixed(1)} MB. Large assemblies can take a minute on a phone.</div><button class="btn" type="button" data-cancel>Cancel</button>`);
      await nextPaint();
      try {
        const buf = await f.arrayBuffer();
        if (b.cancelled) break;
        let res;
        try { res = await parse(ext, buf, PARAMS); } catch (err) { if (!err.cancelled) freshEngine(); throw err; }
        if (my !== loadSeq) return;
        freshEngine();
        if (!res || !res.success || !res.meshes.length) throw new Error("The file opened but has no solid geometry in it.");
        const doc = buildDoc(res, f);
        if (cut.on) cutDoc(doc, true);
        docs.push(doc); opened.push(doc);
      } catch (err) {
        if (my !== loadSeq) return;
        if (err && err.cancelled) break;
        console.error(err);
        const msg = esc((err && err.message) || err);
        failed.push({ name: f.name, title: `Couldn't open ${esc(f.name)}.`, detail: msg, short: msg });
      }
    }
    if (my !== loadSeq) return;
    batch = null;
    if (opened.length) activate(opened[0]);
    if (opened.length) checkMemory(true);
    renderTabs();
    if (failed.length === 1) busy(`<div class="err">${failed[0].title}</div><div class="hint">${failed[0].detail}</div><button class="btn" data-dismiss>OK</button>`);
    else if (failed.length) busy(`<div class="err">Couldn't open ${failed.length} files.</div><div class="hint">${failed.map((x) => `<b>${esc(x.name)}</b>: ${x.short}`).join("<br>")}</div><button class="btn" data-dismiss>OK</button>`);
    else if (docs.length) done();
    else showStart();
  }
  overlay.addEventListener("click", (e) => {
    if (e.target.closest("[data-open]")) $("fileInput").click();
    if (e.target.closest("[data-cancel]")) { if (batch) batch.cancelled = true; cancelLoad(); }
    if (e.target.closest("[data-dismiss]")) { if (docs.length) done(); else showStart(); }
  });

  // first screen: nothing loaded yet
  function showStart() {
    setReadout("");
    busy(`<img class="start-icon" src="icons/logo-160.png" alt="" width="80" height="80">
      <h2 class="start-title">Open a STEP file</h2>
      <div class="hint">Choose one or more .stp or .step files from your phone. Each opens in its own tab. IGES and BREP work too.</div>
      <button class="btn primary start-btn" type="button" data-open>Choose files</button>`);
  }

  $("openBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", (e) => {
    const files = [...e.target.files];
    e.target.value = ""; // so picking the same file again still fires "change"
    loadFiles(files);
  });
  // desktop app (desktop/main.js): files double-clicked in Windows or chosen with File > Open
  if (window.desktop) window.desktop.onOpenFiles((list) => loadFiles(list.map((f) => new File([f.data], f.name))));
  stage.addEventListener("dragover", (e) => { e.preventDefault(); stage.classList.add("drop"); });
  stage.addEventListener("dragleave", () => stage.classList.remove("drop"));
  stage.addEventListener("drop", (e) => {
    e.preventDefault(); stage.classList.remove("drop");
    loadFiles([...e.dataTransfer.files]);
  });

  // ---------- render loop ----------
  function tick() {
    requestAnimationFrame(tick);
    if (controls.update()) dirty = true;
    if (!dirty) return;
    dirty = false;
    const s = camera.position.distanceTo(controls.target) * 0.006;
    markGroup.children.forEach((m) => { if (m.userData.dot) m.scale.setScalar(s); });
    if (cmp) renderCompare();
    else {
      const W = stage.clientWidth, H = stage.clientHeight;
      renderer.setViewport(0, 0, W, H);
      dimLabelsB.forEach((l) => (l.hidden = true));
      drawDims(meshes, camera, { x: 0, y: 0, w: W, h: H }, dimLabels);
      if (cut.on) updateCut();
      renderer.render(scene, camera);
    }
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
  engine().catch((err) => console.warn(err));
})();
