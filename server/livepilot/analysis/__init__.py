"""Audio analyser: measures audio so the agent can reason about what it can't hear."""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from . import beats, compare, features
from .features import public

AUDIO_EXTENSIONS = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
_CACHE: OrderedDict = OrderedDict()
_CACHE_SIZE = 256


def analyze(path: str, start: float | None = None, end: float | None = None,
            tempo_bpm: float | None = None, quick: bool = False) -> dict:
    """features.analyze with an in-memory cache keyed on the file's mtime and size."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no audio file at {path}")
    stat = os.stat(path)
    key = (os.path.abspath(path), stat.st_mtime_ns, stat.st_size, start, end, tempo_bpm, quick)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    result = features.analyze(path, start, end, tempo_bpm, quick)
    _CACHE[key] = result
    if len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return result


def find_audio_files(folder: str, limit: int = 200) -> list[str]:
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"no folder at {folder}")
    found = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in AUDIO_EXTENSIONS and p.is_file():
            found.append(str(p))
            if len(found) >= limit:
                break
    return found


__all__ = ["analyze", "beats", "compare", "features", "find_audio_files", "public", "AUDIO_EXTENSIONS"]
