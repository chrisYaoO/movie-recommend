function ensureBackend({ backendUrl, healthUrl, log, startBackend, waitForHttp, existingBackendTimeoutMs = 1000 }) {
  return waitForHttp(healthUrl, existingBackendTimeoutMs)
    .then(() => {
      log(`Reusing existing backend: ${backendUrl}`);
      return backendUrl;
    })
    .catch(() => {
      startBackend();
      return waitForHttp(healthUrl).then(() => backendUrl);
    });
}

module.exports = {
  ensureBackend
};
