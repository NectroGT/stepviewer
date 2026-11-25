// electron-builder settings for the "+ PDF to STEP" installer: the normal app (package.json "build")
// plus the packaged converter (../converter/dist/pdf2step-server, made with PyInstaller first).
//   npx electron-builder --win nsis --config builder-full.js
// Same appId as the plain viewer, so installing one over the other upgrades it in place.
const base = require("./package.json").build;

module.exports = {
  ...base,
  extraResources: [
    ...base.extraResources,
    { from: "../converter/dist/pdf2step-server", to: "converter" },
  ],
  nsis: { ...base.nsis, artifactName: "ConnectIQ.STP.Viewer.PDF.to.STEP.Setup.${ext}" },
};
