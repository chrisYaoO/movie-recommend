import unittest
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

from backend.app.services.metadata_service import (
    DoubanHttpDetailAdapter,
    DoubanSeleniumDetailAdapter,
    parse_douban_movie_detail,
)


class _FakeHttpResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class _FakeWebDriver:
    def __init__(self, page_source: str) -> None:
        self.page_source = page_source
        self.urls: list[str] = []
        self.page_load_timeout: float | None = None
        self.quit_count = 0

    def set_page_load_timeout(self, timeout: float) -> None:
        self.page_load_timeout = timeout

    def get(self, url: str) -> None:
        self.urls.append(url)

    def quit(self) -> None:
        self.quit_count += 1


class MetadataServiceTest(unittest.TestCase):
    def test_parse_douban_movie_detail_extracts_core_metadata(self) -> None:
        html = """
        <html>
          <head><title>姝ュ饱涓嶅仠 (璞嗙摚)</title></head>
          <body>
            <span property="v:itemreviewed">姝ュ饱涓嶅仠 姝┿亜銇︺倐 姝┿亜銇︺倐</span>
            <div id="info">
              <span><span class="pl">瀵兼紨</span>: <a rel="v:directedBy">Hirokazu Kore-eda</a></span><br>
              <span><span class="pl">涓绘紨</span>: <a rel="v:starring">Hiroshi Abe</a> / <a rel="v:starring">Yui Natsukawa</a></span><br>
              <span property="v:genre">鍓ф儏</span> <span property="v:genre">瀹跺涵</span><br>
              鍒剁墖鍥藉/鍦板尯: 鏃ユ湰<br>
              涓婃槧鏃ユ湡: 2008-06-28(鏃ユ湰)
            </div>
            <strong property="v:average">8.8</strong>
            <span property="v:votes">123456</span>
            <span property="v:summary">A family gathers for a day.</span>
            <img rel="v:image" src="https://img.example/poster.jpg" />
          </body>
        </html>
        """

        detail = parse_douban_movie_detail("2222996", html)

        self.assertEqual("2222996", detail.subject_id)
        self.assertEqual("姝ュ饱涓嶅仠 姝┿亜銇︺倐 姝┿亜銇︺倐", detail.title)
        self.assertEqual("姝ュ饱涓嶅仠 姝┿亜銇︺倐 姝┿亜銇︺倐", detail.display_title)
        self.assertEqual(2008, detail.year)
        self.assertEqual(("Hirokazu Kore-eda",), detail.directors)
        self.assertEqual(("Hiroshi Abe", "Yui Natsukawa"), detail.actors)
        self.assertEqual(("鍓ф儏", "瀹跺涵"), detail.genres)
        self.assertEqual(("鏃ユ湰",), detail.countries)
        self.assertEqual(8.8, detail.douban_rating)
        self.assertEqual(123456, detail.douban_vote_count)
        self.assertEqual("A family gathers for a day.", detail.summary)
        self.assertEqual("https://img.example/poster.jpg", detail.poster_url)

    def test_parse_douban_movie_detail_prefers_json_ld_director_metadata(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "@context": "http://schema.org",
                "@type": "Movie",
                "name": "Still Walking",
                "datePublished": "2008-06-28",
                "image": "https://img.example/p123456.webp",
                "director": [{"@type": "Person", "name": "闆峰痉鍒┞锋柉绉戠壒 Ridley Scott"}],
                "actor": [{"@type": "Person", "name": "Hiroshi Abe"}],
                "genre": ["Drama", "Family"],
                "description": "A family gathers for a day.",
                "aggregateRating": {
                  "@type": "AggregateRating",
                  "ratingValue": "8.8",
                  "ratingCount": "123456"
                }
              }
            </script>
          </head>
          <body><title>blocked fallback title</title></body>
        </html>
        """

        detail = parse_douban_movie_detail("2222996", html, "https://movie.douban.com/subject/2222996/")

        self.assertEqual("Still Walking", detail.title)
        self.assertEqual(2008, detail.year)
        self.assertEqual(("Ridley Scott",), detail.directors)
        self.assertEqual(("Hiroshi Abe",), detail.actors)
        self.assertEqual(("Drama", "Family"), detail.genres)
        self.assertEqual(8.8, detail.douban_rating)
        self.assertEqual(123456, detail.douban_vote_count)
        self.assertEqual("A family gathers for a day.", detail.summary)
        self.assertEqual("https://img.example/p123456.webp", detail.poster_url)
        self.assertEqual("https://movie.douban.com/subject/2222996/", detail.url)

    def test_parse_douban_movie_detail_extracts_bilingual_title_fields(self) -> None:
        html = """
        <html>
          <body>
            <span property="v:itemreviewed">肖申克的救赎 The Shawshank Redemption</span>
            <div id="info">
              原名: The Shawshank Redemption<br>
              又名: 月黑高飞 / 刺激1995<br>
              制片国家/地区: 美国<br>
            </div>
          </body>
        </html>
        """

        detail = parse_douban_movie_detail("1292052", html)

        self.assertEqual("肖申克的救赎 The Shawshank Redemption", detail.title)
        self.assertEqual("肖申克的救赎 The Shawshank Redemption", detail.display_title)
        self.assertEqual("The Shawshank Redemption", detail.original_title)
        self.assertEqual(("月黑高飞", "刺激1995"), detail.aka_titles)

    def test_http_detail_adapter_fetches_desktop_subject_page(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {"name":"Still Walking","director":{"name":"闆峰痉鍒┞锋柉绉戠壒 Ridley Scott"}}
            </script>
          </head>
        </html>
        """

        with patch("backend.app.services.metadata_service.urlopen", return_value=_FakeHttpResponse(html)) as urlopen:
            detail = DoubanHttpDetailAdapter(delay_seconds=0).fetch("2222996")

        request = urlopen.call_args.args[0]
        self.assertEqual("https://movie.douban.com/subject/2222996/", request.full_url)
        self.assertEqual(("Ridley Scott",), detail.directors)

    def test_selenium_detail_adapter_fetches_desktop_page_and_parses_json_ld(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "name": "Thelma &amp; Louise",
                "datePublished": "1991-05-24",
                "director": [{"name": "闆峰痉鍒┞锋柉绉戠壒 Ridley Scott"}],
                "actor": [{"name": "Geena Davis"}],
                "genre": ["鍓ф儏", "鎯婃倸", "鐘姜"],
                "aggregateRating": {"ratingValue": "9.0", "ratingCount": "353101"}
              }
            </script>
          </head>
        </html>
        """
        driver = _FakeWebDriver(html)

        with DoubanSeleniumDetailAdapter(
            timeout_seconds=12,
            delay_seconds=0,
            driver_factory=lambda: driver,
            wait_for_json_ld=False,
        ) as adapter:
            detail = adapter.fetch("1291992")

        self.assertEqual(["https://movie.douban.com/subject/1291992/"], driver.urls)
        self.assertEqual(12, driver.page_load_timeout)
        self.assertEqual("Thelma & Louise", detail.title)
        self.assertEqual(1991, detail.year)
        self.assertEqual(("Ridley Scott",), detail.directors)
        self.assertEqual(("Geena Davis",), detail.actors)
        self.assertEqual(("鍓ф儏", "鎯婃倸", "鐘姜"), detail.genres)
        self.assertEqual(9.0, detail.douban_rating)
        self.assertEqual(353101, detail.douban_vote_count)
        self.assertEqual(1, driver.quit_count)

    def test_selenium_detail_adapter_continues_after_json_ld_wait_timeout(self) -> None:
        html = """
        <html>
          <body>
            <span property="v:itemreviewed">No Json LD Movie</span>
            <div id="info">制片国家/地区: 中国 上映日期: 2021-10-01</div>
            <strong property="v:average">7.1</strong>
            <span property="v:votes">1234</span>
          </body>
        </html>
        """
        driver = _FakeWebDriver(html)

        with patch.object(
            DoubanSeleniumDetailAdapter,
            "_wait_until_detail_loaded",
            side_effect=TimeoutException("no json ld"),
        ):
            with DoubanSeleniumDetailAdapter(
                delay_seconds=0,
                driver_factory=lambda: driver,
                wait_for_json_ld=True,
            ) as adapter:
                detail = adapter.fetch("35030151")

        self.assertEqual("No Json LD Movie", detail.title)
        self.assertEqual(2021, detail.year)

    def test_detail_adapters_reject_generic_douban_title_without_movie_metadata(self) -> None:
        html = "<html><head><title>豆瓣</title></head><body></body></html>"

        with patch("backend.app.services.metadata_service.urlopen", return_value=_FakeHttpResponse(html)):
            with self.assertRaisesRegex(ValueError, "did not contain movie metadata"):
                DoubanHttpDetailAdapter(delay_seconds=0).fetch("1291556")

        driver = _FakeWebDriver(html)
        with DoubanSeleniumDetailAdapter(
            delay_seconds=0,
            driver_factory=lambda: driver,
            wait_for_json_ld=False,
        ) as adapter:
            with self.assertRaisesRegex(ValueError, "did not contain movie metadata"):
                adapter.fetch("1291556")

    def test_detail_adapter_rejects_mojibake_generic_douban_title(self) -> None:
        html = "<html><head><title>떴곌</title></head><body></body></html>"

        with patch("backend.app.services.metadata_service.urlopen", return_value=_FakeHttpResponse(html)):
            with self.assertRaisesRegex(ValueError, "did not contain movie metadata"):
                DoubanHttpDetailAdapter(delay_seconds=0).fetch("1291556")

if __name__ == "__main__":
    unittest.main()
