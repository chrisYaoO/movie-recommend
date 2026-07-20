from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from backend.app.services.metadata_service import (
    DEFAULT_CHROME_BINARY_PATH,
    DoubanSeleniumDetailAdapter,
)


def _subject_id(value: str) -> str:
    if value.isdigit():
        return value
    raise argparse.ArgumentTypeError("movie_id must contain digits only")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch one movie directly from Douban with Selenium and print JSON."
    )
    parser.add_argument("movie_id", type=_subject_id, help="Douban subject ID")
    parser.add_argument("--chrome-binary-path", default=DEFAULT_CHROME_BINARY_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    parser.add_argument("--headed", action="store_true", help="Show the Chrome window")
    args = parser.parse_args()

    with DoubanSeleniumDetailAdapter(
        timeout_seconds=args.timeout_seconds,
        delay_seconds=args.delay_seconds,
        chrome_binary_path=args.chrome_binary_path,
        headless=not args.headed,
    ) as adapter:
        detail = adapter.fetch(args.movie_id)

    print(json.dumps(asdict(detail), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
