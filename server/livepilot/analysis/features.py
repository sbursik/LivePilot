"""Audio feature extraction: loudness, spectrum, stereo image, rhythm and key.

The agent can't hear, so these numbers stand in for listening. Pure numpy/scipy so it runs the
same on Windows and macOS. Long files are processed in chunks to keep memory flat.
"""
from __future__ import annotations

import numpy as np
import soundfile as sf
from scipy import signal

# Named bands used for mix balance and sample fit (Hz).
BANDS = (
    ("sub", 20, 60),
    ("bass", 60, 250),
    ("low_mid", 250, 500),
    ("mid", 500, 2000),
    ("high_mid", 2000, 4000),
    ("presence", 4000, 6000),
    ("air", 6000, 20000),
)
# Octave bands used for tonal-balance comparison against a reference.
OCTAVE_CENTERS = (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
# Krumhansl-Kessler key profiles.
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

SILENCE_DBFS = -80.0
ANALYSIS_SR = 22050  # rhythm and key work on a downsampled mono signal
CHUNK_SECONDS = 30.0


def _power_db(p: float) -> float:
    return float(10 * np.log10(max(p, 1e-20)))


def _amp_db(a: float) -> float:
    return float(20 * np.log10(max(a, 1e-10)))


def _r(x: float | None, digits: int = 1):
    return None if x is None or not np.isfinite(x) else round(float(x), digits)


# ------------------------------------------------------------------- loading

def load(path: str, start: float | None = None, end: float | None = None) -> tuple[np.ndarray, int]:
    """Read (frames, channels) float32, keeping at most two channels. start/end in seconds."""
    info = sf.info(path)
    sr = info.samplerate
    first = max(0, int(round((start or 0.0) * sr)))
    last = info.frames if end is None else min(info.frames, int(round(end * sr)))
    if last <= first:
        raise ValueError(f"empty region {start}-{end}s in {path}")
    data, sr = sf.read(path, start=first, stop=last, dtype="float32", always_2d=True)
    return data[:, :2], sr


# ------------------------------------------------------------------ loudness
# ITU-R BS.1770-4 / EBU R128. K-weighting uses Brecht De Man's analog-prototype parameters,
# which reproduce the standard's 48 kHz coefficients exactly and work at any sample rate.

def _k_weighting(sr: int):
    def shelf():
        g, q, fc = 3.99984385397, 0.7071752369554193, 1681.9744509555319
        k = np.tan(np.pi * fc / sr)
        vh = 10 ** (g / 20)
        vb = vh ** 0.499666774155
        a0 = 1 + k / q + k * k
        b = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]
        a = [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
        return np.array(b), np.array(a)

    def highpass():
        q, fc = 0.5003270373253953, 38.13547087613982
        k = np.tan(np.pi * fc / sr)
        a0 = 1 + k / q + k * k
        return np.array([1.0, -2.0, 1.0]), np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])

    return shelf(), highpass()


def _block_power(weighted_sq: np.ndarray, sr: int, block_s: float, step_s: float) -> np.ndarray:
    """Mean square per block of a per-sample channel-summed power signal."""
    size, step = int(block_s * sr), max(1, int(step_s * sr))
    if len(weighted_sq) < size:
        return np.array([])
    csum = np.concatenate([[0.0], np.cumsum(weighted_sq, dtype=np.float64)])
    starts = np.arange(0, len(weighted_sq) - size + 1, step)
    return (csum[starts + size] - csum[starts]) / size


def _lufs(p: np.ndarray) -> np.ndarray:
    return -0.691 + 10 * np.log10(np.maximum(p, 1e-20))


def _gated_mean(p: np.ndarray, relative_gate: float) -> tuple[float | None, np.ndarray]:
    p = p[_lufs(p) > -70]
    if p.size == 0:
        return None, p
    threshold = _lufs(np.array([p.mean()]))[0] + relative_gate
    kept = p[_lufs(p) > threshold]
    if kept.size == 0:
        return None, kept
    return float(_lufs(np.array([kept.mean()]))[0]), kept


def _true_peak(x: np.ndarray) -> float:
    """4x oversampled peak, in chunks so long files don't need 4x the memory."""
    peak, chunk, pad = 0.0, 1 << 18, 256
    for i in range(0, len(x), chunk):
        lo = max(0, i - pad)
        seg = x[lo: i + chunk + pad].astype(np.float64)
        up = signal.resample_poly(seg, 4, 1, axis=0)
        # The padding absorbs filter ringing from the cut edges; only measure this chunk's samples.
        own = up[(i - lo) * 4: (i - lo + min(chunk, len(x) - i)) * 4]
        peak = max(peak, float(np.max(np.abs(own))))
    return peak


def _k_weighted_power(x: np.ndarray, sr: int) -> np.ndarray:
    """Per-sample K-weighted power summed over channels (L/R weights are 1), filtered in
    chunks with carried filter state."""
    (b1, a1), (b2, a2) = _k_weighting(sr)
    ch = x.shape[1]
    z1 = np.zeros((2, ch))
    z2 = np.zeros((2, ch))
    out = np.empty(len(x))
    chunk = 1 << 20
    for i in range(0, len(x), chunk):
        seg = x[i:i + chunk].astype(np.float64)
        seg, z1 = signal.lfilter(b1, a1, seg, axis=0, zi=z1)
        seg, z2 = signal.lfilter(b2, a2, seg, axis=0, zi=z2)
        out[i:i + chunk] = (seg ** 2).sum(axis=1)
    return out


def loudness(x: np.ndarray, sr: int, true_peak: bool = True) -> dict:
    sq = _k_weighted_power(x, sr)
    momentary = _block_power(sq, sr, 0.4, 0.1)
    integrated, _ = _gated_mean(momentary, -10.0) if momentary.size else (None, None)
    short = _block_power(sq, sr, 3.0, 0.1)
    lra = None
    if short.size:
        _, kept = _gated_mean(short, -20.0)
        if kept.size >= 2:
            levels = _lufs(kept)
            lra = float(np.percentile(levels, 95) - np.percentile(levels, 10))
    sample_peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    result = {
        "integrated_lufs": _r(integrated),
        "short_term_max_lufs": _r(float(_lufs(short).max())) if short.size else None,
        "momentary_max_lufs": _r(float(_lufs(momentary).max())) if momentary.size else None,
        "loudness_range_lu": _r(lra),
        "sample_peak_dbfs": _r(_amp_db(sample_peak)),
        "rms_dbfs": _r(_amp_db(rms)),
        "crest_db": _r(_amp_db(sample_peak) - _amp_db(rms)),
    }
    if true_peak:
        result["true_peak_dbtp"] = _r(_amp_db(_true_peak(x)))
    return result


# ------------------------------------------------------------------ spectrum

def _psd(mono: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    nper = min(8192, len(mono))
    return signal.welch(mono, sr, nperseg=nper, scaling="density")


def _band_powers(freqs: np.ndarray, psd: np.ndarray, edges) -> np.ndarray:
    df = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
    return np.array([psd[(freqs >= lo) & (freqs < hi)].sum() * df for lo, hi in edges])


def band_shares(freqs: np.ndarray, psd: np.ndarray) -> np.ndarray:
    """Fraction of 20 Hz-20 kHz power in each named band (sums to ~1)."""
    p = _band_powers(freqs, psd, [(lo, hi) for _, lo, hi in BANDS])
    return p / max(p.sum(), 1e-20)


def octave_profile(freqs: np.ndarray, psd: np.ndarray) -> np.ndarray:
    """dB of each octave band relative to total power: the file's tonal balance."""
    edges = [(c / np.sqrt(2), c * np.sqrt(2)) for c in OCTAVE_CENTERS]
    p = _band_powers(freqs, psd, edges)
    return 10 * np.log10(np.maximum(p, 1e-20) / max(p.sum(), 1e-20))


def spectrum(mono: np.ndarray, sr: int) -> dict:
    freqs, psd = _psd(mono, sr)
    audible = (freqs >= 20) & (freqs <= 20000)
    f, p = freqs[audible], psd[audible]
    total = max(p.sum(), 1e-20)
    centroid = float((f * p).sum() / total)
    rolloff = float(f[np.searchsorted(np.cumsum(p), 0.85 * total)]) if len(f) else 0.0
    flat = p[f <= 16000]
    flatness = float(np.exp(np.mean(np.log(flat + 1e-20))) / (np.mean(flat) + 1e-20)) if len(flat) else 0.0
    shares = band_shares(freqs, psd)
    if centroid < 800:
        brightness = "dark"
    elif centroid < 2500:
        brightness = "balanced"
    else:
        brightness = "bright"
    return {
        "bands": [{"band": name, "range_hz": [lo, hi], "share_pct": _r(100 * s), "db": _r(_power_db(s))}
                  for (name, lo, hi), s in zip(BANDS, shares)],
        "octave_profile_db": {str(c): _r(v) for c, v in zip(OCTAVE_CENTERS, octave_profile(freqs, psd))},
        "centroid_hz": _r(centroid, 0),
        "rolloff_85_hz": _r(rolloff, 0),
        "flatness": _r(flatness, 3),
        "brightness": brightness,
        "_shares": shares,
        "_octaves": octave_profile(freqs, psd),
    }


# -------------------------------------------------------------------- stereo

def stereo(x: np.ndarray, sr: int) -> dict:
    if x.shape[1] < 2:
        return {"channels": 1}
    left, right = x[:, 0].astype(np.float64), x[:, 1].astype(np.float64)

    def corr(a, b):
        den = np.sqrt((a * a).sum() * (b * b).sum())
        return float((a * b).sum() / den) if den > 0 else 1.0

    mid, side = (left + right) / 2, (left - right) / 2
    side_to_mid = _power_db((side ** 2).sum() / max((mid ** 2).sum(), 1e-20))
    sos = signal.butter(4, 150, "lowpass", fs=sr, output="sos")
    low_corr = corr(signal.sosfilt(sos, left), signal.sosfilt(sos, right))
    c = corr(left, right)
    if c > 0.97:
        width = "mono or near mono"
    elif c > 0.6:
        width = "moderate"
    elif c > 0.2:
        width = "wide"
    else:
        width = "very wide (check mono compatibility)"
    return {"channels": 2, "correlation": _r(c, 2), "side_to_mid_db": _r(max(side_to_mid, -60.0)),
            "low_end_correlation": _r(low_corr, 2), "width": width}


# ------------------------------------------------------------ rhythm and key

def _to_analysis_rate(mono: np.ndarray, sr: int) -> np.ndarray:
    if sr == ANALYSIS_SR:
        return mono.astype(np.float32)
    g = np.gcd(sr, ANALYSIS_SR)
    return signal.resample_poly(mono, ANALYSIS_SR // g, sr // g).astype(np.float32)


def _stft_frames(y: np.ndarray, n_fft: int, hop: int):
    """Yield |STFT| (bins, frames) in chunks; frames are contiguous across chunks."""
    window = signal.get_window("hann", n_fft).astype(np.float32)
    frames_per_chunk = max(1, int(CHUNK_SECONDS * ANALYSIS_SR / hop))
    n_frames = 1 + (len(y) - n_fft) // hop if len(y) >= n_fft else 0
    for f0 in range(0, n_frames, frames_per_chunk):
        idx = np.arange(f0, min(n_frames, f0 + frames_per_chunk))
        segs = np.lib.stride_tricks.sliding_window_view(y, n_fft)[idx * hop]
        yield np.abs(np.fft.rfft(segs * window, axis=1)).T


def onset_envelope(y: np.ndarray, hop: int = 256) -> tuple[np.ndarray, float]:
    flux, prev = [], None
    for mag in _stft_frames(y, 1024, hop):
        logmag = np.log1p(100 * mag)
        if prev is not None:
            logmag_full = np.concatenate([prev, logmag], axis=1)
        else:
            logmag_full = logmag
        flux.append(np.maximum(0, np.diff(logmag_full, axis=1)).sum(axis=0))
        prev = logmag[:, -1:]
    env = np.concatenate(flux) if flux else np.zeros(0)
    return env, ANALYSIS_SR / hop


# Tempo search settings, tuned on 170 of Live's Core Library loops (true BPM in their filenames).
# On those, 70% of reported tempos are exact without a hint and 88% with the song tempo as the
# hint (up from 49%); most misses are double/half tempo or pads and FX with no steady beat.
TEMPO_CENTER_BPM = 110.0     # centre of the preference used to settle double/half ambiguity
TEMPO_WIDTH_OCTAVES = 0.75
MIN_TEMPO_CONFIDENCE = 0.2   # below this there's no steady beat (e.g. vocals); don't report a tempo
LOOP_BARS = (1, 2, 4, 8, 16, 32)
LOOP_BAR_TOLERANCE = 0.015


def _autocorrelation(e: np.ndarray) -> np.ndarray | None:
    e = e - e.mean()
    n = 1 << int(np.ceil(np.log2(2 * len(e))))
    spec = np.fft.rfft(e, n)
    ac = np.fft.irfft(spec * np.conj(spec), n)[: len(e)]
    return ac / ac[0] if ac[0] > 0 else None


def _tempo_prior(bpm):
    return np.exp(-0.5 * (np.log2(np.asarray(bpm) / TEMPO_CENTER_BPM) / TEMPO_WIDTH_OCTAVES) ** 2)


def _tempo(env: np.ndarray, frame_rate: float, duration: float,
           hint: float | None = None) -> tuple[float | None, float, str]:
    """Returns (bpm, confidence, method). confidence is the onset envelope's autocorrelation at
    the beat period, or at half or double it if stronger: how strongly the audio repeats at this
    pulse (0-1). Double and half tempo are the same pulse, so they share one confidence."""
    if len(env) < frame_rate * 0.5:
        return None, 0.0, "too short"
    # Subtract the local mean so sustained energy doesn't mask the pulse.
    w = max(1, int(0.4 * frame_rate))
    e = np.maximum(0, env - np.convolve(env, np.ones(w) / w, mode="same"))
    ac = _autocorrelation(e)
    if ac is None:
        return None, 0.0, "no onsets"
    lags = np.arange(len(ac))

    def strength(bpm):
        lag = 60.0 * frame_rate / bpm
        return float(np.interp(lag, lags, ac)) if lag < len(ac) - 1 else 0.0

    def score(bpm):
        # The beat period plus half credit for two beats. Don't add four beats: a 3/4-beat
        # period lands on 3 beats at 4x and reads 100 BPM as 133.
        return strength(bpm) + 0.5 * strength(bpm / 2)

    grid = np.arange(60.0, 200.0, 0.25)
    best = float(grid[int(np.argmax(np.array([score(b) for b in grid]) * _tempo_prior(grid)))])
    fine = np.arange(best - 1.0, best + 1.0, 0.02)
    best = float(fine[int(np.argmax([score(b) for b in fine]))])
    method = "onset periodicity"

    # A short file is usually a loop cut to whole bars: of the related tempos, prefer one that
    # makes it a whole number of bars. This removes 4/3 and 3/2 errors.
    if duration < 40:
        fits = []
        for b in (best * r for r in (1, 2, 0.5, 4 / 3, 3 / 4, 3 / 2, 2 / 3)):
            bars = duration * b / 240.0
            n = round(bars)
            if 60 <= b <= 200 and n in LOOP_BARS and abs(bars - n) / n < LOOP_BAR_TOLERANCE:
                exact = 240.0 * n / duration
                fits.append((score(exact) * float(_tempo_prior(exact)), exact, n))
        if fits:
            _, best, bars = max(fits)
            method = f"loop length ({bars} bar{'s' if bars > 1 else ''} of 4/4)"
    if hint:
        for r in (2, 0.5):
            if abs(best * r - hint) < abs(best - hint) and abs(best * r - hint) / hint < 0.04:
                best *= r
                method += ", octave from song tempo"
    return best, max(0.0, strength(best), strength(best / 2), strength(best * 2)), method


def rhythm(y: np.ndarray, tempo_bpm: float | None = None) -> dict:
    env, frame_rate = onset_envelope(y)
    duration = len(y) / ANALYSIS_SR
    if len(env) == 0 or env.max() <= 0:
        return {"onsets_per_second": 0.0, "density": "silent or sustained"}
    norm = env / env.max()
    peaks, _ = signal.find_peaks(norm, height=max(0.1, norm.mean() + 0.5 * norm.std()),
                                 distance=max(1, int(0.05 * frame_rate)))
    detected, confidence, method = _tempo(env, frame_rate, duration, tempo_bpm)
    per_second = len(peaks) / duration
    result = {"tempo_confidence": _r(confidence, 2), "onsets_per_second": _r(per_second, 2)}
    if detected is not None and confidence >= MIN_TEMPO_CONFIDENCE:
        result["tempo_bpm_detected"] = _r(detected)
        result["tempo_method"] = method
    else:
        result["tempo_bpm_detected"] = None
        result["tempo_note"] = "no steady beat detected (sustained, rubato or arrhythmic material)"
    beat_bpm = tempo_bpm or result["tempo_bpm_detected"]
    if beat_bpm:
        per_beat = per_second * 60.0 / beat_bpm
        result["onsets_per_beat"] = _r(per_beat, 2)
        result["density"] = "sparse" if per_beat < 1 else "moderate" if per_beat < 2.5 else "busy"
    return result


def chroma(y: np.ndarray) -> np.ndarray:
    n_fft, hop = 8192, 4096
    freqs = np.fft.rfftfreq(n_fft, 1 / ANALYSIS_SR)
    use = (freqs >= 55) & (freqs <= 4200)
    pitch = 69 + 12 * np.log2(freqs[use] / 440.0)
    pc = np.round(pitch).astype(int) % 12
    # Bins far from a semitone centre say little about pitch class.
    weight = np.clip(1 - 2 * np.abs(pitch - np.round(pitch)), 0, 1)
    total = np.zeros(12)
    for mag in _stft_frames(y, n_fft, hop):
        energy = mag[use].sum(axis=1) * weight
        total += np.bincount(pc, weights=energy, minlength=12)
    return total


def key(y: np.ndarray) -> dict:
    c = chroma(y)
    if c.sum() <= 0:
        return {"key": None, "note": "no tonal content"}
    candidates = []
    for root in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            r = float(np.corrcoef(c, np.roll(profile, root))[0, 1])
            candidates.append((r, root, mode))
    candidates.sort(reverse=True)
    best_r, root, mode = candidates[0]
    result = {
        "key": f"{NOTE_NAMES[root]} {mode}" if best_r >= 0.5 else None,
        "root_note": root if best_r >= 0.5 else None,
        "mode": mode if best_r >= 0.5 else None,
        "confidence": _r(best_r, 2),
        "alternatives": [{"key": f"{NOTE_NAMES[k]} {m}", "correlation": _r(r, 2)} for r, k, m in candidates[1:3]],
        "chroma": {NOTE_NAMES[i]: _r(v / c.max(), 2) for i, v in enumerate(c)},
    }
    if best_r < 0.5:
        result["note"] = "no clear key (percussive, noisy or atonal material)"
    elif best_r - candidates[1][0] < 0.05:
        second = candidates[1]
        result["ambiguous_with"] = f"{NOTE_NAMES[second[1]]} {second[2]}"
        result["note"] = (f"{result['key']} and {result['ambiguous_with']} fit almost equally well; "
                          "the material may not contain the notes that tell them apart.")
    return result


# -------------------------------------------------------------------- output

def energy_over_time(mono: np.ndarray, sr: int, points: int = 32) -> list[dict]:
    duration = len(mono) / sr
    n = int(max(1, min(points, round(duration))))
    edges = np.linspace(0, len(mono), n + 1).astype(int)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = mono[a:b].astype(np.float64)
        out.append({"time_sec": _r(a / sr, 2), "rms_dbfs": _r(max(_amp_db(np.sqrt(np.mean(seg ** 2))), -90.0))})
    return out


BASS_HEAVY_PCT = 85  # share of energy below 250 Hz; see observations()


def observations(result: dict) -> list[str]:
    """Plain-language flags a producer would notice. Heuristics, not rules."""
    notes = []
    loud, spec, st = result.get("loudness", {}), result.get("spectrum", {}), result.get("stereo", {})
    tp = loud.get("true_peak_dbtp")
    if tp is not None and tp > -1.0:
        notes.append(f"True peak is {tp} dBTP: above -1 dBTP risks clipping after MP3/AAC encoding.")
    crest = loud.get("crest_db")
    if crest is not None and crest < 6:
        notes.append(f"Crest factor {crest} dB: heavily compressed or limited, little transient punch left.")
    shares = {b["band"]: b["share_pct"] or 0 for b in spec.get("bands", [])}
    if shares:
        low = shares["sub"] + shares["bass"]
        # Energy share always leans low (a kick or bass note carries far more energy than a hi-hat
        # at the same loudness). Finished house mixes measured 70-75% below 250 Hz, so 65% misfired.
        if low > BASS_HEAVY_PCT:
            notes.append(f"Low end (below 250 Hz) carries {low:.0f}% of the energy, more than most "
                         "full mixes (normal for a kick or bass stem). Confirm with compare_to_reference "
                         "against a track in the same genre before cutting bass.")
        if shares["low_mid"] > 20:
            notes.append(f"Low mids (250-500 Hz) at {shares['low_mid']:.0f}%: muddy or boxy for a full mix "
                         "(normal for a vocal or single instrument).")
        if shares["air"] < 0.5 and low < 90:
            notes.append("Very little energy above 6 kHz: a full mix would sound dull next to commercial "
                         "tracks.")
    if st.get("channels") == 2:
        if (st.get("low_end_correlation") or 1) < 0.7:
            notes.append("Low end below 150 Hz isn't mono: bass may thin out on mono club systems.")
        if (st.get("correlation") or 1) < 0.2:
            notes.append("Stereo correlation is low: check how it sounds summed to mono.")
    k = result.get("key", {})
    if k.get("key"):
        notes.append(k.get("note") or f"Key estimate: {k['key']} (confidence {k['confidence']}).")
    return notes


def analyze(path: str, start: float | None = None, end: float | None = None,
            tempo_bpm: float | None = None, quick: bool = False) -> dict:
    """Full analysis of a file or region. quick skips loudness range/true peak and stereo,
    for scanning many samples."""
    x, sr = load(path, start, end)
    mono = x.mean(axis=1)
    result: dict = {"file": path, "duration_sec": _r(len(x) / sr, 2), "sample_rate": sr,
                    "channels": int(x.shape[1])}
    if start or end:
        result["region_sec"] = [_r(start or 0.0, 3), _r(end if end is not None else len(x) / sr, 3)]
    peak_db = _amp_db(float(np.max(np.abs(x)))) if len(x) else -200.0
    if peak_db < SILENCE_DBFS:
        result["silent"] = True
        return result
    result["loudness"] = loudness(x, sr, true_peak=not quick)
    result["spectrum"] = spectrum(mono, sr)
    if not quick:
        result["stereo"] = stereo(x, sr)
    y = _to_analysis_rate(mono, sr)
    result["rhythm"] = rhythm(y, tempo_bpm)
    result["key"] = key(y)
    if not quick:
        result["energy_over_time"] = energy_over_time(mono, sr)
    result["observations"] = observations(result)
    return result


def public(result: dict) -> dict:
    """Strip internal arrays (keys starting with '_') before returning to a client."""
    if isinstance(result, dict):
        return {k: public(v) for k, v in result.items() if not k.startswith("_")}
    if isinstance(result, list):
        return [public(v) for v in result]
    return result
