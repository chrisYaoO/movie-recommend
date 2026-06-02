from __future__ import annotations

from html import unescape
import json
import re
import time
from typing import Protocol
from urllib.request import Request, urlopen

from selenium.common.exceptions import TimeoutException

from backend.app.models.domain import DoubanMovieDetail


DEFAULT_CHROME_BINARY_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class DoubanDetailAdapter(Protocol):
    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        pass


class FakeDoubanDetailAdapter:
    def __init__(self, details_by_subject_id: dict[str, DoubanMovieDetail] | None = None) -> None:
        self.details_by_subject_id = details_by_subject_id or {}
        self.fetches: list[str] = []

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        self.fetches.append(subject_id)
        return self.details_by_subject_id[subject_id]


class DoubanHttpDetailAdapter:
    def __init__(
        self,
        timeout_seconds: float = 10.0,
        delay_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent
        self.last_request_at = 0.0
        self._last_page_source: str | None = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        self._throttle()
        url = f"https://movie.douban.com/subject/{subject_id}/"
        request = Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            html = response.read().decode("utf-8", errors="replace")
        self._last_page_source = html
        self.last_request_at = time.monotonic()
        detail = parse_douban_movie_detail(subject_id, html, url)
        if _is_invalid_detail_title(detail.title):
            raise ValueError("Douban detail page did not contain movie metadata")
        return detail

    @property
    def last_page_source(self) -> str | None:
        return self._last_page_source

    def _throttle(self) -> None:
        if self.last_request_at <= 0:
            return
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)


class DoubanSeleniumDetailAdapter:
    def __init__(
        self,
        timeout_seconds: float = 20.0,
        delay_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        chrome_binary_path: str | None = None,
        headless: bool = True,
        disable_images: bool = True,
        driver_factory=None,
        wait_for_json_ld: bool = True,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.user_agent = user_agent
        self.chrome_binary_path = chrome_binary_path
        self.headless = headless
        self.disable_images = disable_images
        self.driver_factory = driver_factory
        self.wait_for_json_ld = wait_for_json_ld
        self.last_request_at = 0.0
        self._last_page_source: str | None = None
        self.driver = None

    def fetch(self, subject_id: str) -> DoubanMovieDetail:
        self._throttle()
        url = f"https://movie.douban.com/subject/{subject_id}/"
        driver = self._ensure_driver()
        driver.get(url)
        if self.wait_for_json_ld:
            try:
                self._wait_until_detail_loaded(driver)
            except TimeoutException:
                pass
        self.last_request_at = time.monotonic()
        self._last_page_source = driver.page_source

        detail = parse_douban_movie_detail(subject_id, driver.page_source, url)
        if _is_invalid_detail_title(detail.title):
            raise ValueError("Douban detail page did not contain movie metadata")
        return detail

    @property
    def last_page_source(self) -> str | None:
        return self._last_page_source

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_driver(self):
        if self.driver is None:
            if self.driver_factory is not None:
                self.driver = self.driver_factory()
            else:
                self.driver = self._create_driver()
            self.driver.set_page_load_timeout(self.timeout_seconds)
        return self.driver

    def _create_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        if self.chrome_binary_path:
            options.binary_location = self.chrome_binary_path
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument(f"user-agent={self.user_agent}")
        if self.disable_images:
            options.add_experimental_option(
                "prefs",
                {"profile.managed_default_content_settings.images": 2},
            )
        return webdriver.Chrome(options=options)

    def _wait_until_detail_loaded(self, driver) -> None:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, self.timeout_seconds).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'script[type="application/ld+json"]'))
        )

    def _throttle(self) -> None:
        if self.last_request_at <= 0:
            return
        elapsed = time.monotonic() - self.last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)


def parse_douban_movie_detail(subject_id: str, html: str, url: str | None = None) -> DoubanMovieDetail:
    structured = _extract_json_ld_detail(html)
    display_title = _extract_title(html)
    info_text = _extract_info_text(html)
    title = structured.get("title") or display_title or ""
    structured_directors = structured.get("directors") or ()
    html_directors = tuple(_extract_people_by_rel(html, "v:directedBy"))
    return DoubanMovieDetail(
        subject_id=subject_id,
        title=title,
        display_title=display_title,
        original_title=_extract_original_title(info_text),
        aka_titles=_extract_aka_titles(info_text),
        year=structured.get("year") or _extract_year_from_title_or_info(title, info_text),
        directors=structured_directors or html_directors,
        actors=structured.get("actors") or tuple(_extract_people_by_rel(html, "v:starring")),
        genres=structured.get("genres") or tuple(_extract_spans_by_property(html, "v:genre")),
        countries=_extract_labeled_values(info_text, "鍒剁墖鍥藉/鍦板尯"),
        douban_rating=structured.get("douban_rating") or _extract_rating(html),
        douban_vote_count=structured.get("douban_vote_count") or _extract_vote_count(html),
        summary=structured.get("summary") or _extract_summary(html),
        poster_url=structured.get("poster_url") or _extract_poster_url(html),
        url=url or f"https://movie.douban.com/subject/{subject_id}/",
    )


def _is_invalid_detail_title(title: str | None) -> bool:
    if not title:
        return True
    stripped = title.strip()
    return stripped in {"璞嗙摚", "豆瓣", "떴곌", "??"} or bool(stripped) and set(stripped) == {"?"}


def _extract_json_ld_detail(html: str) -> dict:
    match = re.search(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        return {}

    raw_json = re.sub(r"[\x00-\x1f]+", " ", unescape(match.group(1))).strip()
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}

    rating = payload.get("aggregateRating") if isinstance(payload.get("aggregateRating"), dict) else {}
    return {
        "title": _json_text(payload.get("name")),
        "year": _json_year(payload.get("datePublished")),
        "directors": _json_people(payload.get("director")),
        "actors": _json_people(payload.get("actor")),
        "genres": _json_text_tuple(payload.get("genre")),
        "douban_rating": _json_float(rating.get("ratingValue")),
        "douban_vote_count": _json_int(rating.get("ratingCount") or rating.get("reviewCount")),
        "summary": _json_text(payload.get("description")),
        "poster_url": _json_text(payload.get("image")),
    }


def _json_people(value) -> tuple[str, ...]:
    if isinstance(value, dict):
        name = _json_text(value.get("name"))
        return (name,) if name else ()
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, dict):
                name = _json_text(item.get("name"))
            else:
                name = _json_text(item)
            if name:
                names.append(name)
        return tuple(names)
    name = _json_text(value)
    return (name,) if name else ()


def _json_text_tuple(value) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(text for item in value if (text := _json_text(item)))
    text = _json_text(value)
    if text is None:
        return ()
    return tuple(part.strip() for part in re.split(r"[/,]", text) if part.strip())


def _json_text(value) -> str | None:
    if value is None:
        return None
    text = unescape(str(value)).strip()
    return text or None


def _json_year(value) -> int | None:
    text = _json_text(value)
    if text is None:
        return None
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    return int(match.group(1)) if match is not None else None


def _json_float(value) -> float | None:
    text = _json_text(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _json_int(value) -> int | None:
    text = _json_text(value)
    if text is None:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def _extract_title(html: str) -> str | None:
    match = re.search(r'<span[^>]+property=["\']v:itemreviewed["\'][^>]*>(.*?)</span>', html, re.DOTALL)
    if match is None:
        match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    if match is None:
        return None
    return _clean_html_text(match.group(1)).replace("(璞嗙摚)", "").strip()


def _extract_info_text(html: str) -> str:
    match = re.search(r'<div[^>]+id=["\']info["\'][^>]*>(.*?)</div>', html, re.DOTALL)
    if match is None:
        return ""
    return _clean_html_text(match.group(1))


def _extract_people_by_rel(html: str, rel: str) -> list[str]:
    pattern = rf'<a[^>]+rel=["\']{re.escape(rel)}["\'][^>]*>(.*?)</a>'
    return [_clean_html_text(match.group(1)) for match in re.finditer(pattern, html, re.DOTALL)]


def _extract_spans_by_property(html: str, property_name: str) -> list[str]:
    pattern = rf'<span[^>]+property=["\']{re.escape(property_name)}["\'][^>]*>(.*?)</span>'
    return [_clean_html_text(match.group(1)) for match in re.finditer(pattern, html, re.DOTALL)]


def _extract_labeled_values(info_text: str, label: str) -> tuple[str, ...]:
    marker = f"{label}:"
    if marker in info_text:
        value = info_text.split(marker, 1)[1].strip().split(" ", 1)[0]
        return tuple(part.strip() for part in value.split("/") if part.strip())

    match = re.search(r"(?:制片国家/地区|鍒剁墖鍥藉\S*/鍦板尯|\?+/\?+):\s*(\S+)", info_text)
    if match is None:
        return ()
    value = match.group(1)
    return tuple(part.strip() for part in value.split("/") if part.strip())


def _extract_original_title(info_text: str) -> str | None:
    return _extract_labeled_text(info_text, ("原名", "鍘熷悕"))


def _extract_aka_titles(info_text: str) -> tuple[str, ...]:
    value = _extract_labeled_text(info_text, ("又名", "鍙堝悕"))
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split("/") if part.strip())


def _extract_labeled_text(info_text: str, labels: tuple[str, ...]) -> str | None:
    for label in labels:
        marker = f"{label}:"
        if marker not in info_text:
            continue
        value = info_text.split(marker, 1)[1].strip()
        next_label = re.search(r"\s+[^\s:：]{2,12}[:：]", value)
        if next_label is not None:
            value = value[: next_label.start()].strip()
        return value or None
    return None


def _extract_year_from_title_or_info(title: str, info_text: str) -> int | None:
    for text in (title, info_text):
        match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
        if match is not None:
            return int(match.group(1))
    return None


def _extract_rating(html: str) -> float | None:
    match = re.search(r'<strong[^>]+property=["\']v:average["\'][^>]*>(.*?)</strong>', html, re.DOTALL)
    if match is None:
        match = re.search(r'<meta[^>]+itemprop=["\']ratingValue["\'][^>]+content=["\']([^"\']+)["\']', html, re.DOTALL)
    if match is None:
        return None
    try:
        return float(_clean_html_text(match.group(1)))
    except ValueError:
        return None


def _extract_vote_count(html: str) -> int | None:
    match = re.search(r'<span[^>]+property=["\']v:votes["\'][^>]*>(.*?)</span>', html, re.DOTALL)
    if match is None:
        match = re.search(r'<meta[^>]+itemprop=["\']reviewCount["\'][^>]+content=["\']([^"\']+)["\']', html, re.DOTALL)
    if match is None:
        return None
    digits = re.sub(r"\D", "", _clean_html_text(match.group(1)))
    return int(digits) if digits else None


def _extract_summary(html: str) -> str | None:
    match = re.search(r'<span[^>]+property=["\']v:summary["\'][^>]*>(.*?)</span>', html, re.DOTALL)
    if match is None:
        match = re.search(r'<meta[^>]+itemprop=["\']description["\'][^>]+content=["\']([^"\']+)["\']', html, re.DOTALL)
    if match is None:
        return None
    summary = _clean_html_text(match.group(1))
    summary = re.sub(r"^.*?绠€浠媅:锛歖", "", summary)
    return summary or None


def _extract_poster_url(html: str) -> str | None:
    match = re.search(r'<img[^>]+rel=["\']v:image["\'][^>]+src=["\']([^"\']+)["\']', html, re.DOTALL)
    if match is None:
        match = re.search(r'<meta[^>]+itemprop=["\']image["\'][^>]+content=["\']([^"\']+)["\']', html, re.DOTALL)
    if match is None:
        return None
    return unescape(match.group(1))


def _clean_html_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(without_tags).split())
