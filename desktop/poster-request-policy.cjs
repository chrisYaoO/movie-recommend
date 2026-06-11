const DOUBAN_POSTER_HOST_PATTERN = /^img\d+\.doubanio\.com$/i;
const DOUBAN_REFERER = "https://movie.douban.com/";

function isDoubanPosterUrl(url) {
  try {
    return DOUBAN_POSTER_HOST_PATTERN.test(new URL(url).hostname);
  } catch {
    return false;
  }
}

function posterRequestHeaders(url, requestHeaders) {
  if (!isDoubanPosterUrl(url)) return requestHeaders;

  const headers = { ...requestHeaders };
  const refererKey = Object.keys(headers).find((key) => key.toLowerCase() === "referer");
  if (refererKey) {
    headers[refererKey] = DOUBAN_REFERER;
  } else {
    headers.Referer = DOUBAN_REFERER;
  }
  return headers;
}

module.exports = {
  DOUBAN_REFERER,
  isDoubanPosterUrl,
  posterRequestHeaders
};
