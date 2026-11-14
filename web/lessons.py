"""
web/lessons.py — "Edge Education" lesson rotation for the newsletter.

Lessons live in content/education_lessons.md (falls back to the _DRAFT file
until the reviewed version is saved). Format: "### N. Title" followed by body.
One lesson per edition, rotating by date so every subscriber sees the same one.
"""
import os
import re
from datetime import date
from typing import Optional

_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "content")
_HEAD = re.compile(r"^###\s+(\d+)\.\s+(.+)$", re.M)
_cache: dict = {}


def _load() -> list:
    for name in ("education_lessons.md", "education_lessons_DRAFT.md"):
        path = os.path.join(_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if _cache.get("path") == path and _cache.get("mtime") == mtime:
            return _cache["lessons"]
        text = open(path, encoding="utf-8").read()
        marks = list(_HEAD.finditer(text))
        lessons = []
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            body = text[m.end():end].split("\n---")[0].split("\n## ")[0].strip()
            lessons.append({"n": int(m.group(1)), "title": m.group(2).strip(), "body": " ".join(body.split())})
        _cache.update(path=path, mtime=mtime, lessons=lessons)
        return lessons
    return []


def lesson_for(day: date) -> Optional[dict]:
    lessons = _load()
    return lessons[day.toordinal() % len(lessons)] if lessons else None
