"""Beat grid: where every beat and downbeat falls in a recording whose tempo may drift.

AI-generated songs (Suno, Udio) and live recordings rarely hold one tempo: a song can ramp from
130 to 133 BPM, so no song tempo lines it up with Live's grid. This finds each beat so the clip
can be warped with a marker on every bar.
"""
from __future__ import annotations

import numpy as np
from scipy import signal

from . import features
from .features import ANALYSIS_SR, _r

HOP = 256
FRAME_RATE = ANALYSIS_SR / HOP
TIGHTNESS = 100.0       # how strongly the beat tracker keeps to the tempo between beats
REFINE_SECONDS = 0.025  # a tracked beat moves to the strongest onset this close to it
SMOOTH_BEATS = 8        # beats on each side in the local fit that removes onset jitter
# The spectral-flux peak comes this long before the hit (48 ms on clicks and snares, 44 on a real
# drum stem): a frame differs most from the one before as the hit enters the end of its window.
ONSET_LEAD_SECONDS = 0.045
MIN_TEMPO_CONFIDENCE = 0.2


def _mix(paths: list[str]) -> np.ndarray:
    """Sum the files (stems of one song) to mono at the analysis rate."""
    mix = None
    for path in paths:
        x, sr = features.load(path)
        y = features._to_analysis_rate(x.mean(axis=1), sr)
        if mix is None:
            mix = y
        else:
            n = max(len(mix), len(y))
            mix = np.pad(mix, (0, n - len(mix))) + np.pad(y, (0, n - len(y)))
    return mix


def _normalised_onsets(y: np.ndarray) -> np.ndarray:
    env, _ = features.onset_envelope(y, HOP)
    w = int(0.75 * FRAME_RATE)
    env = np.maximum(0, env - np.convolve(env, np.ones(w) / w, mode="same"))
    return env / (env.std() + 1e-9)


def _track(env: np.ndarray, period: float) -> np.ndarray:
    """Dynamic-programming beat tracker (Ellis 2007): the beat sequence with the most onset
    strength, penalising gaps that stray from the period. Returns beat frames."""
    n = len(env)
    offsets = np.arange(int(period * 0.8), int(np.ceil(period * 1.25)) + 1)
    penalty = -TIGHTNESS * np.log(offsets / period) ** 2
    score, back = env.astype(float).copy(), -np.ones(n, dtype=int)
    for i in range(offsets[-1], n):
        candidates = score[i - offsets] + penalty
        j = int(np.argmax(candidates))
        score[i] += candidates[j]
        back[i] = i - offsets[j]
    i = n - offsets[-1] + int(np.argmax(score[-offsets[-1]:]))
    beats = [i]
    while back[i] >= 0:
        i = back[i]
        beats.append(i)
    return np.array(beats[::-1])


def _refine(env: np.ndarray, frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Move each beat to the nearby onset peak, between frames. Returns (seconds, strength)."""
    w = int(REFINE_SECONDS * FRAME_RATE)
    times, strength = [], []
    for f in frames:
        lo, hi = max(1, f - w), min(len(env) - 2, f + w)
        j = lo + int(np.argmax(env[lo:hi + 1]))
        a, b, c = env[j - 1], env[j], env[j + 1]
        curve = a - 2 * b + c
        offset = 0.5 * (a - c) / curve if curve < 0 else 0.0
        times.append((j + offset) / FRAME_RATE)
        strength.append(b)
    return np.array(times) + ONSET_LEAD_SECONDS, np.array(strength)


def _smooth(times: np.ndarray, strength: np.ndarray) -> np.ndarray:
    """Each beat from a weighted straight-line fit to its neighbours: follows tempo drift but
    not the few-ms jitter of single onsets, and bridges beats with no onset (breaks, silence)."""
    out = np.empty_like(times)
    weights = strength ** 2 + 1e-3
    for i in range(len(times)):
        lo, hi = max(0, i - SMOOTH_BEATS), min(len(times), i + SMOOTH_BEATS + 1)
        k = np.arange(lo, hi) - i
        p = np.polyfit(k, times[lo:hi], 1, w=np.sqrt(weights[lo:hi]))
        out[i] = p[1]
    return out


def _beat_chroma(y: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """Pitch-class energy between consecutive beats (beats, 12)."""
    n_fft = 4096
    freqs = np.fft.rfftfreq(n_fft, 1 / ANALYSIS_SR)
    use = (freqs >= 55) & (freqs <= 2000)
    pc = np.round(69 + 12 * np.log2(freqs[use] / 440.0)).astype(int) % 12
    window = signal.get_window("hann", n_fft)
    out = np.zeros((len(beats), 12))
    for i, t in enumerate(beats):
        start = int(t * ANALYSIS_SR)
        seg = y[start:start + n_fft]
        if len(seg) < n_fft:
            break
        mag = np.abs(np.fft.rfft(seg * window))[use]
        out[i] = np.bincount(pc, weights=mag, minlength=12)
    return out


def _downbeat(y: np.ndarray, beats: np.ndarray) -> tuple[int, float]:
    """Which of the first four beats starts a bar (4/4), from where chords change and where the
    low end (kick, bass notes) hits hardest. Returns (index, confidence 0-1)."""
    c = _beat_chroma(y, beats)
    c /= np.linalg.norm(c, axis=1, keepdims=True) + 1e-9
    change = np.r_[0.0, 1 - (c[1:] * c[:-1]).sum(axis=1)]
    sos = signal.butter(4, 150, "lowpass", fs=ANALYSIS_SR, output="sos")
    low = np.abs(signal.sosfilt(sos, y))
    hit = np.array([low[int(t * ANALYSIS_SR):int((t + 0.08) * ANALYSIS_SR)].mean() for t in beats])
    score = np.zeros(4)
    for feature in (change, hit):
        per_phase = np.array([feature[m::4].mean() for m in range(4)])
        score += per_phase / (per_phase.max() + 1e-12)
    order = np.argsort(score)[::-1]
    return int(order[0]), float((score[order[0]] - score[order[1]]) / 2)


def beat_grid(paths: list[str], rhythm_paths: list[str] | None = None,
              tempo_hint: float | None = None) -> dict:
    """Beats (seconds), the first downbeat, and the tempo curve of a song from its stems (or one
    mixdown). Stems must start together, as exported stems do. Beats are tracked on
    rhythm_paths (e.g. the drum stems; default all): vocal and pad onsets off the beat pull a
    tracker on the full mix astray. The downbeat comes from all of them."""
    y = _mix(paths)
    duration = len(y) / ANALYSIS_SR
    env = _normalised_onsets(_mix(rhythm_paths) if rhythm_paths else y)
    bpm, confidence, _ = features._tempo(env, FRAME_RATE, duration, tempo_hint)
    if bpm is None or confidence < MIN_TEMPO_CONFIDENCE:
        raise ValueError("no steady beat to follow in this audio")
    raw, strength = _refine(env, _track(env, 60.0 * FRAME_RATE / bpm))
    beats = _smooth(raw, strength)
    keep = np.r_[True, np.diff(beats) > 0]
    beats, raw, strength = beats[keep], raw[keep], strength[keep]
    first_downbeat, downbeat_confidence = _downbeat(y, beats)
    strong = strength > np.median(strength)
    jitter = np.sqrt(np.mean((raw[strong] - beats[strong]) ** 2)) if strong.any() else 0.0
    local = 60.0 / np.diff(beats)
    bars = [{"bar": i // 4 + 1, "time_sec": _r(beats[i], 3),
             "bpm": _r(240.0 / (beats[i + 4] - beats[i]), 2)}
            for i in range(first_downbeat, len(beats) - 4, 4)]
    return {
        "duration_sec": duration,
        "beats": beats.tolist(),
        "first_downbeat": first_downbeat,
        "downbeat_confidence": _r(downbeat_confidence, 2),
        "tempo_bpm": {"start": _r(np.median(local[:16]), 2), "end": _r(np.median(local[-16:]), 2),
                      "mean": _r(60.0 * (len(beats) - 1) / (beats[-1] - beats[0]), 2),
                      "min": _r(min(b["bpm"] for b in bars), 2) if bars else None,
                      "max": _r(max(b["bpm"] for b in bars), 2) if bars else None},
        "steady": bool(bars) and max(b["bpm"] for b in bars) - min(b["bpm"] for b in bars) < 0.2,
        "onset_jitter_ms": _r(jitter * 1000, 1),
        "bars": bars,
    }
