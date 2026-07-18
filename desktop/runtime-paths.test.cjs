const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");
const { virtualenvPythonPath } = require("./runtime-paths.cjs");

test("selects the virtualenv Python path for each desktop platform", () => {
  const repoRoot = path.resolve("repo");

  assert.equal(
    virtualenvPythonPath(repoRoot, "win32"),
    path.join(repoRoot, ".venv", "Scripts", "python.exe")
  );
  assert.equal(
    virtualenvPythonPath(repoRoot, "darwin"),
    path.join(repoRoot, ".venv", "bin", "python")
  );
});
