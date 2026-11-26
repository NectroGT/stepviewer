/* Mesh files: STL (binary and text), OBJ, 3MF and glTF / GLB. Read here instead of by the CAD engine, and handed
   to the page in the same form the engine gives (meshes with position / normal / index arrays and "faces" as runs of
   triangles), so turning, measuring, cutting and weights work the same way.

   A mesh has no true faces, only triangles: the triangles are grouped into faces wherever neighbours meet at less
   than 30 degrees (the angle the outline edges are drawn at), so a flat side or a round hole wall is one face.
   Sizes: STL and OBJ carry no units and are read as millimetres; 3MF says its unit; glTF is in metres.
   Used by worker.js and, when workers are unavailable, by the page itself. */
"use strict";
(function (root) {
  const EXTS = ["stl", "obj", "3mf", "gltf", "glb"];
  const FACE_ANGLE = 30;
  const dec = new TextDecoder();

  // ---------- STL ----------
  function readSTL(buf) {
    const dv = new DataView(buf);
    const text = !/^\s*solid\b/.test(dec.decode(new Uint8Array(buf, 0, Math.min(buf.byteLength, 300)))) ? false
      : /\bfacet\b/.test(dec.decode(new Uint8Array(buf, 0, Math.min(buf.byteLength, 2000))));
    if (buf.byteLength >= 84) {
      const n = dv.getUint32(80, true);
      // binary: 80-byte header, count, 50 bytes per triangle (some exporters add bytes at the end; some start the
      // header with "solid" too)
      if (n > 0 && (84 + n * 50 === buf.byteLength || (!text && 84 + n * 50 <= buf.byteLength))) {
        const P = new Float32Array(n * 9);
        for (let t = 0; t < n; t++) {
          const o = 84 + t * 50 + 12;                            // skip the stored normal
          for (let k = 0; k < 9; k++) P[t * 9 + k] = dv.getFloat32(o + 4 * k, true);
        }
        return [{ name: null, P }];                              // the header is usually exporter text, not a name
      }
    }
    const bodies = [];
    let cur = null, v = [];
    for (const line of dec.decode(buf).split(/\r?\n/)) {
      const s = line.trim();
      if (s.startsWith("vertex")) {
        const a = s.split(/\s+/);
        v.push(+a[1], +a[2], +a[3]);
      } else if (s.startsWith("solid")) {
        cur = { name: s.slice(5).trim() || null }; v = []; bodies.push(cur); cur.v = v;
      } else if (!cur && s) {
        throw new Error("This isn't an STL file.");
      }
    }
    return bodies.map((b) => ({ name: b.name, P: Float32Array.from(b.v.slice(0, b.v.length - (b.v.length % 9))) }))
      .filter((b) => b.P.length);
  }

  // ---------- OBJ ----------
  function readOBJ(buf) {
    const V = [], bodies = [];
    let cur = null;
    const start = (name) => { cur = { name, v: [] }; bodies.push(cur); };
    for (const line of dec.decode(buf).split(/\r?\n/)) {
      const s = line.trim();
      if (s.startsWith("v ")) {
        const a = s.split(/\s+/);
        V.push(+a[1], +a[2], +a[3]);
      } else if (s.startsWith("f ")) {
        if (!cur) start(null);
        const idx = s.split(/\s+/).slice(1).map((t) => {
          const i = parseInt(t, 10);
          return i < 0 ? V.length / 3 + i : i - 1;
        });
        for (let k = 1; k + 1 < idx.length; k++) {             // polygons as a fan of triangles
          for (const i of [idx[0], idx[k], idx[k + 1]]) cur.v.push(V[3 * i], V[3 * i + 1], V[3 * i + 2]);
        }
      } else if (s.startsWith("o ") || s.startsWith("g ")) {
        const name = s.slice(2).trim();
        if (!cur || cur.v.length) start(name || null); else cur.name = name || cur.name;
      }
    }
    return bodies.filter((b) => b.v.length).map((b) => ({ name: b.name, P: Float32Array.from(b.v) }));
  }

  // ---------- 3MF (a zip of XML) ----------
  async function unzip(buf) {
    const dv = new DataView(buf), u8 = new Uint8Array(buf);
    let e = buf.byteLength - 22;
    while (e >= 0 && dv.getUint32(e, true) !== 0x06054b50) e--;
    if (e < 0) throw new Error("This isn't a 3MF file (not a zip archive).");
    const count = dv.getUint16(e + 10, true), files = {};
    let p = dv.getUint32(e + 16, true);
    for (let i = 0; i < count; i++) {
      const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
      files[dec.decode(u8.subarray(p + 46, p + 46 + nlen))] =
        { method: dv.getUint16(p + 10, true), size: dv.getUint32(p + 20, true), off: dv.getUint32(p + 42, true) };
      p += 46 + nlen + xlen + clen;
    }
    return {
      names: Object.keys(files),
      async text(name) {
        const f = files[name];
        const s = f.off + 30 + dv.getUint16(f.off + 26, true) + dv.getUint16(f.off + 28, true);
        const data = u8.subarray(s, s + f.size);
        if (f.method === 0) return dec.decode(data);
        if (f.method !== 8) throw new Error("The 3MF file uses a compression this viewer can't read.");
        const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
        return dec.decode(await new Response(stream).arrayBuffer());
      },
    };
  }

  const attrs = (s) => { const o = {}; for (const m of s.matchAll(/([\w:]+)="([^"]*)"/g)) o[m[1]] = m[2]; return o; };
  const UNITS = { micron: 0.001, millimeter: 1, centimeter: 10, inch: 25.4, foot: 304.8, meter: 1000 };

  // 3MF transforms are 3x4, row vectors: p' = p * M (m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32)
  const mat3mf = (s) => (s ? s.trim().split(/\s+/).map(Number) : [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]);
  const then3mf = (a, b) => {                                   // a, then b
    const r = new Array(12);
    for (let i = 0; i < 4; i++) for (let j = 0; j < 3; j++) {
      r[3 * i + j] = a[3 * i] * b[j] + a[3 * i + 1] * b[3 + j] + a[3 * i + 2] * b[6 + j] + (i === 3 ? b[9 + j] : 0);
    }
    return r;
  };

  async function read3MF(buf) {
    const zip = await unzip(buf);
    const name = zip.names.find((n) => /^\/?3D\/3dmodel\.model$/i.test(n)) || zip.names.find((n) => /\.model$/i.test(n));
    if (!name) throw new Error("No 3D model found in the 3MF file.");
    const xml = await zip.text(name);
    const unit = UNITS[(/<(?:\w+:)?model\b[^>]*\bunit="(\w+)"/.exec(xml) || [])[1] || "millimeter"] || 1;
    const objects = {};
    for (const m of xml.matchAll(/<(?:\w+:)?object\b([^>]*)>([\s\S]*?)<\/(?:\w+:)?object>/g)) {
      const a = attrs(m[1]), body = m[2], o = { name: a.name || null };
      if (/<(?:\w+:)?mesh\b/.test(body)) {
        const v = [];
        for (const x of body.matchAll(/<(?:\w+:)?vertex\b([^>]*)\/?>/g)) { const q = attrs(x[1]); v.push(+q.x, +q.y, +q.z); }
        const t = [];
        for (const x of body.matchAll(/<(?:\w+:)?triangle\b([^>]*)\/?>/g)) { const q = attrs(x[1]); t.push(+q.v1, +q.v2, +q.v3); }
        o.mesh = { v, t };
      }
      o.parts = [...body.matchAll(/<(?:\w+:)?component\b([^>]*)\/?>/g)].map((x) => attrs(x[1]));
      objects[a.id] = o;
    }
    const bodies = [];
    const place = (id, M, label, depth = 0) => {
      const o = objects[id];
      if (!o || depth > 16) return;
      if (o.mesh) {
        const { v, t } = o.mesh, P = new Float32Array(t.length * 3);
        for (let k = 0; k < t.length; k++) {
          const x = v[3 * t[k]], y = v[3 * t[k] + 1], z = v[3 * t[k] + 2];
          for (let j = 0; j < 3; j++) P[3 * k + j] = (x * M[j] + y * M[3 + j] + z * M[6 + j] + M[9 + j]) * unit;
        }
        if (P.length) bodies.push({ name: o.name || label, P });
      }
      for (const c of o.parts) place(c.objectid, then3mf(mat3mf(c.transform), M), o.name || label, depth + 1);
    };
    const items = [...xml.matchAll(/<(?:\w+:)?item\b([^>]*)\/?>/g)].map((x) => attrs(x[1]));
    if (items.length) items.forEach((it) => place(it.objectid, mat3mf(it.transform), null));
    else Object.keys(objects).forEach((id) => place(id, mat3mf(), null));
    return bodies;
  }

  // ---------- glTF / GLB ----------
  const COMP = { 5120: [Int8Array, 1], 5121: [Uint8Array, 1], 5122: [Int16Array, 2], 5123: [Uint16Array, 2],
    5125: [Uint32Array, 4], 5126: [Float32Array, 4] };
  const NCOMP = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };

  function b64(s) {
    const bin = atob(s), out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function mat4(node) {                                         // column-major, as glTF stores it
    if (node.matrix) return node.matrix.slice();
    const [tx, ty, tz] = node.translation || [0, 0, 0], [x, y, z, w] = node.rotation || [0, 0, 0, 1];
    const [sx, sy, sz] = node.scale || [1, 1, 1];
    return [(1 - 2 * (y * y + z * z)) * sx, 2 * (x * y + z * w) * sx, 2 * (x * z - y * w) * sx, 0,
      2 * (x * y - z * w) * sy, (1 - 2 * (x * x + z * z)) * sy, 2 * (y * z + x * w) * sy, 0,
      2 * (x * z + y * w) * sz, 2 * (y * z - x * w) * sz, (1 - 2 * (x * x + y * y)) * sz, 0, tx, ty, tz, 1];
  }
  const mul4 = (a, b) => {
    const r = new Array(16);
    for (let c = 0; c < 4; c++) for (let rr = 0; rr < 4; rr++) {
      r[4 * c + rr] = a[rr] * b[4 * c] + a[4 + rr] * b[4 * c + 1] + a[8 + rr] * b[4 * c + 2] + a[12 + rr] * b[4 * c + 3];
    }
    return r;
  };

  function readGLTF(buf, ext) {
    let json, bin = null;
    const dv = new DataView(buf);
    if (ext === "glb" || (buf.byteLength > 12 && dv.getUint32(0, true) === 0x46546c67)) {
      let p = 12;
      while (p + 8 <= buf.byteLength) {
        const len = dv.getUint32(p, true), type = dv.getUint32(p + 4, true);
        if (type === 0x4e4f534a) json = JSON.parse(dec.decode(new Uint8Array(buf, p + 8, len)));
        else if (type === 0x004e4942) bin = buf.slice(p + 8, p + 8 + len);
        p += 8 + len;
      }
      if (!json) throw new Error("This isn't a GLB file.");
    } else json = JSON.parse(dec.decode(buf));
    const needs = (json.extensionsRequired || []).filter((x) => /draco|meshopt|quantization/i.test(x));
    if (needs.length) throw new Error(`The file is compressed (${needs.join(", ")}), which this viewer can't read. Export it without mesh compression.`);
    const buffers = (json.buffers || []).map((b, i) => {
      if (b.uri === undefined) return bin;
      const m = /^data:[^,]*;base64,(.*)$/.exec(b.uri);
      if (m) return b64(m[1]);
      throw new Error(`The file keeps its geometry in a separate file (${b.uri}). Save it as .glb (one file) to open it here.`);
    });
    const accessor = (i) => {
      const a = json.accessors[i], n = NCOMP[a.type], [T, size] = COMP[a.componentType], out = new Float64Array(a.count * n);
      if (a.bufferView === undefined) return out;
      const bv = json.bufferViews[a.bufferView], d = new DataView(buffers[bv.buffer]);
      const stride = bv.byteStride || n * size, base = (bv.byteOffset || 0) + (a.byteOffset || 0);
      const get = { 1: T === Int8Array ? "getInt8" : "getUint8", 2: T === Int16Array ? "getInt16" : "getUint16",
        4: T === Float32Array ? "getFloat32" : "getUint32" }[size];
      for (let k = 0; k < a.count; k++) for (let j = 0; j < n; j++) out[k * n + j] = d[get](base + k * stride + j * size, true);
      return out;
    };
    const bodies = [];
    const visit = (ni, parent, depth = 0) => {
      const node = json.nodes[ni];
      if (!node || depth > 64) return;
      const M = mul4(parent, mat4(node));
      if (node.mesh !== undefined) {
        const mesh = json.meshes[node.mesh], v = [];
        let color = null;
        for (const prim of mesh.primitives) {
          const mode = prim.mode === undefined ? 4 : prim.mode;
          if (mode < 4 || prim.attributes.POSITION === undefined) continue;   // points and lines
          const pos = accessor(prim.attributes.POSITION), cnt = pos.length / 3;
          let idx = prim.indices !== undefined ? accessor(prim.indices) : Float64Array.from({ length: cnt }, (_, k) => k);
          if (mode === 5) {                                     // triangle strip
            const t = []; for (let k = 0; k + 2 < idx.length; k++) t.push(...(k % 2 ? [idx[k + 1], idx[k], idx[k + 2]] : [idx[k], idx[k + 1], idx[k + 2]])); idx = t;
          } else if (mode === 6) {                              // triangle fan
            const t = []; for (let k = 1; k + 1 < idx.length; k++) t.push(idx[0], idx[k], idx[k + 1]); idx = t;
          }
          for (let k = 0; k + 2 < idx.length; k += 3) {
            for (let j = 0; j < 3; j++) {
              const q = idx[k + j], x = pos[3 * q], y = pos[3 * q + 1], z = pos[3 * q + 2];
              v.push(M[0] * x + M[4] * y + M[8] * z + M[12], M[1] * x + M[5] * y + M[9] * z + M[13],
                M[2] * x + M[6] * y + M[10] * z + M[14]);
            }
          }
          const mat = prim.material !== undefined && json.materials ? json.materials[prim.material] : null;
          const bc = mat && mat.pbrMetallicRoughness && mat.pbrMetallicRoughness.baseColorFactor;
          const srgb = (c) => (c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055);   // glTF colours are linear
          if (bc && !color) color = bc.slice(0, 3).map(srgb);
        }
        if (v.length) bodies.push({ name: node.name || mesh.name || null, P: Float32Array.from(v), color });
      }
      (node.children || []).forEach((c) => visit(c, M, depth + 1));
    };
    const I4 = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
    const scene = json.scenes && json.scenes[json.scene || 0];
    let roots = scene ? scene.nodes || [] : null;
    if (!roots) {                                               // no scene: every node no other node lists as a child
      const kids = new Set((json.nodes || []).flatMap((n) => n.children || []));
      roots = (json.nodes || []).map((_, i) => i).filter((i) => !kids.has(i));
    }
    roots.forEach((ni) => visit(ni, I4));
    // glTF is meant to be in metres, Y up. Many CAD exporters (CadQuery among them) write millimetres anyway, so a
    // model over 10 units across is taken as millimetres - a part 10 m long is unlikely here. Turned to Z up.
    let lo = Infinity, hi = -Infinity;
    for (const b of bodies) for (const x of b.P) { if (x < lo) lo = x; if (x > hi) hi = x; }
    const k = hi - lo > 10 ? 1 : 1000;
    for (const b of bodies) {
      const P = b.P;
      for (let i = 0; i < P.length; i += 3) { const y = P[i + 1], z = P[i + 2]; P[i] *= k; P[i + 1] = -z * k; P[i + 2] = y * k; }
    }
    return bodies;
  }

  // ---------- triangles -> the engine's mesh form, with faces ----------
  function toMesh(body, i) {
    const P = body.P, T = P.length / 9;
    // weld identical corners, so neighbouring triangles can be found
    const ids = new Map(), W = new Uint32Array(3 * T), VP = [];
    for (let c = 0; c < 3 * T; c++) {
      const key = `${P[3 * c]},${P[3 * c + 1]},${P[3 * c + 2]}`;
      let id = ids.get(key);
      if (id === undefined) { id = VP.length / 3; ids.set(key, id); VP.push(P[3 * c], P[3 * c + 1], P[3 * c + 2]); }
      W[c] = id;
    }
    const V = VP.length / 3;
    // triangle normals (unit; zero for slivers) and area-weighted ones for shading
    const N = new Float32Array(3 * T), A = new Float32Array(3 * T);
    for (let t = 0; t < T; t++) {
      const a = 3 * W[3 * t], b = 3 * W[3 * t + 1], c = 3 * W[3 * t + 2];
      const ux = VP[b] - VP[a], uy = VP[b + 1] - VP[a + 1], uz = VP[b + 2] - VP[a + 2];
      const vx = VP[c] - VP[a], vy = VP[c + 1] - VP[a + 1], vz = VP[c + 2] - VP[a + 2];
      const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx, l = Math.hypot(nx, ny, nz);
      A[3 * t] = nx; A[3 * t + 1] = ny; A[3 * t + 2] = nz;
      if (l > 0) { N[3 * t] = nx / l; N[3 * t + 1] = ny / l; N[3 * t + 2] = nz / l; }
    }
    // neighbours across each edge (the first two triangles that share it)
    const edgeTri = new Map(), nb = new Int32Array(3 * T).fill(-1);
    for (let t = 0; t < T; t++) for (let j = 0; j < 3; j++) {
      const p = W[3 * t + j], q = W[3 * t + (j + 1) % 3];
      if (p === q) continue;
      const key = p < q ? p * V + q : q * V + p, o = edgeTri.get(key);
      if (o === undefined) edgeTri.set(key, 3 * t + j);
      else if (o >= 0) { nb[3 * t + j] = (o / 3) | 0; nb[o] = t; edgeTri.set(key, -1); }
    }
    // faces: grow over edges where the triangles meet at less than FACE_ANGLE
    const cos = Math.cos((FACE_ANGLE * Math.PI) / 180), face = new Int32Array(T).fill(-1), order = new Uint32Array(T);
    const runs = [];
    let nf = 0, n = 0;
    for (let s = 0; s < T; s++) {
      if (face[s] >= 0) continue;
      const first = n;
      face[s] = nf; order[n++] = s;
      for (let h = first; h < n; h++) {
        const t = order[h];
        for (let j = 0; j < 3; j++) {
          const u = nb[3 * t + j];
          if (u < 0 || face[u] >= 0) continue;
          const d = N[3 * t] * N[3 * u] + N[3 * t + 1] * N[3 * u + 1] + N[3 * t + 2] * N[3 * u + 2];
          const sliver = !(N[3 * t] || N[3 * t + 1] || N[3 * t + 2]) || !(N[3 * u] || N[3 * u + 1] || N[3 * u + 2]);
          if (d >= cos || sliver) { face[u] = nf; order[n++] = u; }
        }
      }
      runs.push({ first, last: n - 1 });
      nf++;
    }
    // output: triangles grouped by face; corners shared within a face (smooth shading there, sharp between faces)
    const stamp = new Int32Array(V).fill(-1), local = new Uint32Array(V);
    const pos = [], nor = [], index = new Uint32Array(3 * T);
    for (let f = 0; f < runs.length; f++) {
      for (let h = runs[f].first; h <= runs[f].last; h++) {
        const t = order[h];
        for (let j = 0; j < 3; j++) {
          const w = W[3 * t + j];
          if (stamp[w] !== f) {
            stamp[w] = f; local[w] = pos.length / 3;
            pos.push(VP[3 * w], VP[3 * w + 1], VP[3 * w + 2]); nor.push(0, 0, 0);
          }
          const o = local[w];
          index[3 * h + j] = o;
          nor[3 * o] += A[3 * t]; nor[3 * o + 1] += A[3 * t + 1]; nor[3 * o + 2] += A[3 * t + 2];
        }
      }
    }
    const NO = Float32Array.from(nor);
    for (let k = 0; k < NO.length; k += 3) {
      const l = Math.hypot(NO[k], NO[k + 1], NO[k + 2]) || 1;
      NO[k] /= l; NO[k + 1] /= l; NO[k + 2] /= l;
    }
    return { name: body.name || `Body ${i + 1}`, color: body.color || null, brep_faces: runs,
      attributes: { position: { array: Float32Array.from(pos) }, normal: { array: NO } }, index: { array: index } };
  }

  async function read(ext, buf) {
    const bodies = ext === "stl" ? readSTL(buf) : ext === "obj" ? readOBJ(buf) : ext === "3mf" ? await read3MF(buf)
      : readGLTF(buf, ext);
    const meshes = bodies.filter((b) => b.P.length >= 9).map(toMesh);
    const root = meshes.length === 1 ? { name: null, meshes: [0], children: [] }
      : { name: null, meshes: [], children: meshes.map((m, i) => ({ name: m.name, meshes: [i], children: [] })) };
    return { success: true, root, meshes };
  }

  root.MeshFiles = { EXTS, read };
})(typeof self !== "undefined" ? self : this);
