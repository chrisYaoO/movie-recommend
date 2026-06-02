from __future__ import annotations

import re


def display_person_name(text: str) -> str:
    match = re.search(r"[a-zA-Z]", text)
    if match is None:
        return text.strip()

    split_index = match.start()
    local_part = text[:split_index].strip()
    foreign_part = text[split_index:].strip()
    if not local_part:
        return foreign_part
    if _has_middle_dot(local_part):
        return foreign_part or local_part
    if not re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", local_part):
        return foreign_part or local_part
    return local_part


def display_person_names(values: tuple[str, ...] | list[str]) -> list[str]:
    return [display_person_name(value) for value in values if value and display_person_name(value)]


def display_movie_title(title: str, original_title: str | None = None) -> str:
    clean_title = " ".join(title.split())
    clean_original = " ".join((original_title or "").split())
    if not clean_original:
        return clean_title
    if clean_original.casefold() in clean_title.casefold():
        return clean_title
    if not re.search(r"[a-zA-Z]", clean_original):
        return clean_title
    if not re.search(r"[\u3400-\u9fff]", clean_title):
        return clean_title
    return f"{clean_title} {clean_original}"


def _has_middle_dot(value: str) -> bool:
    return any(marker in value for marker in ("·", "・", "•", ".", "┞"))
