/* Runs the CAD engine (occt-import-js) off the page's main thread, so the page stays
   responsive while a file is read. app.js starts this from a blob: URL, which makes the
   worker inherit the page's Content Security Policy (no outside connections). */
"use strict";
let occt = null;

self.onmessage = async (e) => {
  const m = e.data;
  if (m.init) { // m.init is the site's base URL
    try {
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
    const res = m.ext === "stp" || m.ext === "step" ? occt.ReadStepFile(data, m.params)
      : m.ext === "igs" || m.ext === "iges" ? occt.ReadIgesFile(data, m.params) : occt.ReadBrepFile(data, m.params);
    // hand the big arrays over as typed arrays without copying them
    const transfer = [];
    const typed = (holder, T) => { const a = holder.array = new T(holder.array); transfer.push(a.buffer); };
    if (res && res.meshes) res.meshes.forEach((om) => {
      typed(om.attributes.position, Float32Array);
      if (om.attributes.normal) typed(om.attributes.normal, Float32Array);
      typed(om.index, Uint32Array);
    });
    postMessage({ res }, transfer);
  } catch (err) {
    postMessage({ error: String((err && err.message) || err) });
  }
};
