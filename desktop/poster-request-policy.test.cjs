const test = require("node:test");
const assert = require("node:assert/strict");

const {
  DOUBAN_REFERER,
  isDoubanPosterUrl,
  posterRequestHeaders
} = require("./poster-request-policy.cjs");

test("identifies only Douban poster hosts", () => {
  assert.equal(isDoubanPosterUrl("https://img3.doubanio.com/view/photo/poster.webp"), true);
  assert.equal(isDoubanPosterUrl("https://img.example.com/poster.webp"), false);
  assert.equal(isDoubanPosterUrl("not-a-url"), false);
});

test("injects the Douban Referer without changing other headers", () => {
  assert.deepEqual(
    posterRequestHeaders("https://img9.doubanio.com/view/photo/poster.webp", { Accept: "image/webp" }),
    { Accept: "image/webp", Referer: DOUBAN_REFERER }
  );
});

test("replaces an existing case-insensitive Referer header", () => {
  assert.deepEqual(
    posterRequestHeaders("https://img1.doubanio.com/view/photo/poster.webp", { referer: "file://" }),
    { referer: DOUBAN_REFERER }
  );
});

test("leaves non-Douban requests unchanged", () => {
  const headers = { Accept: "image/webp" };
  assert.equal(posterRequestHeaders("https://img.example.com/poster.webp", headers), headers);
});
