from __future__ import annotations

from datetime import date

from backend.app.models.domain import Movie, ViewingHistory


def seed_movies() -> list[Movie]:
    return [
        Movie("m-yi-yi", "Yi Yi", 2000, ("Edward Yang",), ("Wu Nien-jen", "Issei Ogata"), ("Drama",), ("Taiwan",), 9.1, 180000, "https://movie.douban.com/subject/1292434/"),
        Movie("m-after-life", "After Life", 1998, ("Hirokazu Kore-eda",), ("Arata Iura", "Erika Oda"), ("Drama", "Fantasy"), ("Japan",), 8.3, 60000, "https://movie.douban.com/subject/1292529/"),
        Movie("m-in-the-mood", "In the Mood for Love", 2000, ("Wong Kar-wai",), ("Tony Leung", "Maggie Cheung"), ("Drama", "Romance"), ("Hong Kong",), 8.8, 700000, "https://movie.douban.com/subject/1291557/"),
        Movie("m-burning", "Burning", 2018, ("Lee Chang-dong",), ("Yoo Ah-in", "Steven Yeun"), ("Drama", "Mystery"), ("South Korea",), 8.0, 300000, "https://movie.douban.com/subject/26842702/"),
        Movie("m-drive-my-car", "Drive My Car", 2021, ("Ryusuke Hamaguchi",), ("Hidetoshi Nishijima", "Toko Miura"), ("Drama",), ("Japan",), 7.9, 210000, "https://movie.douban.com/subject/35235502/", ("Academy Award for Best International Feature Film",)),
        Movie("m-a-separation", "A Separation", 2011, ("Asghar Farhadi",), ("Peyman Moaadi", "Leila Hatami"), ("Drama",), ("Iran",), 8.8, 430000, "https://movie.douban.com/subject/5964718/", ("Academy Award for Best International Feature Film",)),
        Movie("m-the-handmaiden", "The Handmaiden", 2016, ("Park Chan-wook",), ("Kim Min-hee", "Kim Tae-ri"), ("Drama", "Mystery"), ("South Korea",), 8.3, 620000, "https://movie.douban.com/subject/25977027/"),
        Movie("m-before-sunrise", "Before Sunrise", 1995, ("Richard Linklater",), ("Ethan Hawke", "Julie Delpy"), ("Drama", "Romance"), ("United States",), 8.8, 510000, "https://movie.douban.com/subject/1296339/"),
        Movie("m-perfect-days", "Perfect Days", 2023, ("Wim Wenders",), ("Koji Yakusho",), ("Drama",), ("Japan",), 8.2, 90000, "https://movie.douban.com/subject/35956190/"),
    ]


def seed_history() -> list[ViewingHistory]:
    return [
        ViewingHistory("m-yi-yi", date(2024, 1, 5), 5.0, "1080p", "favorite"),
        ViewingHistory("m-in-the-mood", date(2024, 2, 8), 4.5, "1080p", "strong style fit"),
        ViewingHistory("m-burning", date(2024, 6, 18), 3.5, "1080p", "admired more than enjoyed"),
    ]
