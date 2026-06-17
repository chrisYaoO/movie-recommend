const test = require("node:test");
const assert = require("node:assert/strict");

const { ensureBackend } = require("./backend-lifecycle.cjs");

test("reuses an already healthy backend instead of starting another process", async () => {
  let startCount = 0;
  const messages = [];

  const url = await ensureBackend({
    backendUrl: "http://127.0.0.1:8000",
    healthUrl: "http://127.0.0.1:8000/openapi.json",
    log: (message) => messages.push(message),
    startBackend: () => {
      startCount += 1;
    },
    waitForHttp: async () => undefined
  });

  assert.equal(url, "http://127.0.0.1:8000");
  assert.equal(startCount, 0);
  assert.deepEqual(messages, ["Reusing existing backend: http://127.0.0.1:8000"]);
});

test("starts a backend when the existing backend health check fails", async () => {
  let startCount = 0;
  let waitCount = 0;

  const url = await ensureBackend({
    backendUrl: "http://127.0.0.1:8000",
    healthUrl: "http://127.0.0.1:8000/openapi.json",
    log: () => undefined,
    startBackend: () => {
      startCount += 1;
    },
    waitForHttp: async () => {
      waitCount += 1;
      if (waitCount === 1) {
        throw new Error("not ready");
      }
    }
  });

  assert.equal(url, "http://127.0.0.1:8000");
  assert.equal(startCount, 1);
  assert.equal(waitCount, 2);
});
