"""Versioned static asset URLs — /static/ is served immutable for 7 days, so the
query string (file mtime) is what busts the cache when a file changes."""
import os

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_cache: dict = {}


def asset(path: str) -> str:
    v = _cache.get(path)
    if v is None:
        try:
            v = int(os.path.getmtime(os.path.join(_STATIC_DIR, path)))
        except OSError:
            v = 0
        _cache[path] = v
    return f"/static/{path}?v={v}"
