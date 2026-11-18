// Runs before the viewer page loads. Gives the page one thing only: a way to receive files the
// desktop app opened for it (double-clicked in Windows, or chosen with File > Open).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  onOpenFiles: (callback) => {
    ipcRenderer.on("open-files", (_event, files) => callback(files));
    ipcRenderer.send("viewer-ready"); // now listening: the app can send files waiting to be opened
  },
});
