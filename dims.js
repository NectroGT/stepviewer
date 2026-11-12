#!/usr/bin/env node
// Check dimensions of STEP/IGES/BREP files.
//
//   node dims.js part.stp                 overall size + per-part sizes
//   node dims.js pre.stp post.stp         also compares the two (e.g. pre vs post machining)
//   node dims.js part.stp --parts=0       overall size only
//
// Uses the Open CASCADE (occt-import-js) build bundled with the
// "3D Model Viewer (Online3DViewer)" VS Code extension, so nothing extra to install.
// Set OCCT_IMPORT_JS=/path/to/occt-import-js.js to use a different copy.

const fs = require('fs');
const path = require('path');
const os = require('os');

function findOcct() {
  if (process.env.OCCT_IMPORT_JS) return process.env.OCCT_IMPORT_JS;
  const extDir = path.join(os.homedir(), '.vscode', 'extensions');
  const hits = fs.existsSync(extDir)
    ? fs.readdirSync(extDir).filter((d) => d.startsWith('nahueltaibo.o3dv-viewer-')).sort()
    : [];
  for (const d of hits.reverse()) {
    const p = path.join(extDir, d, 'dist', 'occt', 'occt-import-js.js');
    if (fs.existsSync(p)) return p;
  }
  try { return require.resolve('occt-import-js'); } catch { /* fall through */ }
  console.error('Could not find occt-import-js. Install the O3DV VS Code extension, ' +
    'run "npm i occt-import-js", or set OCCT_IMPORT_JS.');
  process.exit(1);
}

function emptyBox() {
  return { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
}

function addPoints(box, pos) {
  for (let i = 0; i < pos.length; i += 3) {
    for (let k = 0; k < 3; k++) {
      const v = pos[i + k];
      if (v < box.min[k]) box.min[k] = v;
      if (v > box.max[k]) box.max[k] = v;
    }
  }
}

function size(box) {
  return [0, 1, 2].map((k) => box.max[k] - box.min[k]);
}

// Signed volume of a closed triangle mesh (divergence theorem).
function meshVolume(pos, idx) {
  let v = 0;
  for (let t = 0; t < idx.length; t += 3) {
    const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
    const ax = pos[a], ay = pos[a + 1], az = pos[a + 2];
    const bx = pos[b], by = pos[b + 1], bz = pos[b + 2];
    const cx = pos[c], cy = pos[c + 1], cz = pos[c + 2];
    v += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx);
  }
  return v / 6;
}

function meshArea(pos, idx) {
  let s = 0;
  for (let t = 0; t < idx.length; t += 3) {
    const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
    const ux = pos[b] - pos[a], uy = pos[b + 1] - pos[a + 1], uz = pos[b + 2] - pos[a + 2];
    const vx = pos[c] - pos[a], vy = pos[c + 1] - pos[a + 1], vz = pos[c + 2] - pos[a + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    s += Math.sqrt(cx * cx + cy * cy + cz * cz) / 2;
  }
  return s;
}

function readModel(occt, file) {
  const buf = new Uint8Array(fs.readFileSync(file));
  const ext = path.extname(file).toLowerCase();
  // Fine tessellation so the mesh bounding box sits close to the true geometry.
  const params = { linearUnit: 'millimeter', linearDeflectionType: 'absolute_value',
    linearDeflection: 0.01, angularDeflection: 0.1 };
  let res;
  if (ext === '.stp' || ext === '.step') res = occt.ReadStepFile(buf, params);
  else if (ext === '.igs' || ext === '.iges') res = occt.ReadIgesFile(buf, params);
  else if (ext === '.brep' || ext === '.brp') res = occt.ReadBrepFile(buf, params);
  else throw new Error(`Unsupported file type: ${ext}`);
  if (!res || !res.success) throw new Error(`Failed to read ${file}`);

  const total = emptyBox();
  let volume = 0, area = 0;
  const parts = res.meshes.map((m, i) => {
    const pos = m.attributes.position.array;
    const idx = m.index.array;
    const box = emptyBox();
    addPoints(box, pos);
    addPoints(total, pos);
    const vol = Math.abs(meshVolume(pos, idx));
    const ar = meshArea(pos, idx);
    volume += vol;
    area += ar;
    return { name: m.name || `part ${i + 1}`, box, volume: vol, area: ar };
  });
  return { file, box: total, volume, area, parts };
}

const f = (n, d = 3) => n.toFixed(d);
const sizeStr = (s) => `${f(s[0])} x ${f(s[1])} x ${f(s[2])} mm`;

function report(m, showParts) {
  const s = size(m.box);
  console.log(`\n=== ${path.basename(m.file)} ===`);
  console.log(`  Overall size (X x Y x Z): ${sizeStr(s)}`);
  console.log(`  Min corner: (${m.box.min.map((v) => f(v)).join(', ')})`);
  console.log(`  Max corner: (${m.box.max.map((v) => f(v)).join(', ')})`);
  console.log(`  Volume:       ${f(m.volume / 1000, 3)} cm^3   (${f(m.volume, 1)} mm^3)`);
  console.log(`  Surface area: ${f(m.area, 1)} mm^2`);
  console.log(`  Parts/bodies: ${m.parts.length}`);
  if (showParts && m.parts.length > 1) {
    const limit = 50;
    m.parts.slice(0, limit).forEach((p, i) => {
      console.log(`    ${String(i + 1).padStart(3)}. ${p.name.padEnd(30).slice(0, 30)} ` +
        `${sizeStr(size(p.box))}   vol ${f(p.volume, 1)} mm^3`);
    });
    if (m.parts.length > limit) console.log(`    ... ${m.parts.length - limit} more`);
  }
}

function compare(a, b) {
  const sa = size(a.box), sb = size(b.box);
  console.log(`\n=== Compare: ${path.basename(a.file)}  ->  ${path.basename(b.file)} ===`);
  ['X', 'Y', 'Z'].forEach((ax, k) => {
    const d = sb[k] - sa[k];
    console.log(`  ${ax}: ${f(sa[k]).padStart(10)} -> ${f(sb[k]).padStart(10)} mm   ` +
      `(${d >= 0 ? '+' : ''}${f(d)} mm)`);
  });
  const dv = b.volume - a.volume;
  const pct = a.volume ? (dv / a.volume) * 100 : 0;
  console.log(`  Volume: ${f(a.volume, 1)} -> ${f(b.volume, 1)} mm^3   ` +
    `(${dv >= 0 ? '+' : ''}${f(dv, 1)} mm^3, ${pct >= 0 ? '+' : ''}${f(pct, 2)}%)`);
  if (dv < 0) console.log(`  Material removed: ${f(-dv / 1000, 3)} cm^3`);
}

async function main() {
  const args = process.argv.slice(2);
  const showParts = !args.includes('--parts=0');
  const files = args.filter((a) => !a.startsWith('--'));
  if (files.length === 0) {
    console.log('Usage: node dims.js <file.stp> [other.stp ...] [--parts=0]');
    process.exit(1);
  }
  const occt = await require(findOcct())();
  const models = [];
  for (const file of files) {
    process.stderr.write(`Reading ${path.basename(file)}...\n`);
    const m = readModel(occt, file);
    models.push(m);
    report(m, showParts);
  }
  if (models.length === 2) compare(models[0], models[1]);
  console.log('\nNote: sizes are axis-aligned bounding boxes in the file\'s own coordinates;' +
    ' volume is from the tessellated mesh (accurate to ~0.1%).');
}

main().catch((e) => { console.error(e.message || e); process.exit(1); });
