const { spawn } = require("child_process");
const path = require("path");

const electronPath = require("electron");
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronPath, [path.join(__dirname, "main.cjs")], {
  cwd: path.resolve(__dirname, ".."),
  env,
  stdio: "inherit",
  windowsHide: false
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code || 0);
});
