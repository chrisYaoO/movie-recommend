const path = require("path");

function virtualenvPythonPath(repoRoot, platform = process.platform) {
  return platform === "win32"
    ? path.join(repoRoot, ".venv", "Scripts", "python.exe")
    : path.join(repoRoot, ".venv", "bin", "python");
}

module.exports = { virtualenvPythonPath };
