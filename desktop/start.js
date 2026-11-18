// `npm start`: run the desktop app from source.
// VS Code's terminal sets ELECTRON_RUN_AS_NODE=1, which makes Electron act like plain Node and
// not open a window, so start it without that variable.
const { spawn } = require("node:child_process");
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(require("electron"), [__dirname, ...process.argv.slice(2)], { stdio: "inherit", env });
child.on("exit", (code) => process.exit(code ?? 0));
