"""Analyser tests on synthesised audio with known answers."""
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from livepilot import analysis  # noqa: E402
from livepilot.analysis import compare, features  # noqa: E402

SR = 48000


def write(tmp_path, name, data, sr=SR):
    path = tmp_path / name
    sf.write(path, np.asarray(data, dtype=np.float32), sr, subtype="FLOAT")
    return str(path)


def sine(freq, seconds, amp=0.5, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return amp * np.sin(2 * np.pi * freq * t)


def stereo(mono):
    return np.column_stack([mono, mono])


def click_track(bpm, seconds, sr=SR):
    x = np.zeros(int(seconds * sr))
    period = 60.0 / bpm
    burst = np.random.default_rng(0).standard_normal(int(0.01 * sr)) * np.hanning(int(0.01 * sr))
    t = 0.0
    while t * sr + len(burst) < len(x):
        i = int(t * sr)
        x[i:i + len(burst)] += 0.8 * burst
        t += period
    return x


def tone(midi, seconds, amp=0.2):
    f = 440 * 2 ** ((midi - 69) / 12)
    return sum(amp / h * sine(f * h, seconds, 1.0) for h in (1, 2, 3))


# ------------------------------------------------------------------ loudness

def test_ebu_reference_sine_reads_minus_23_lufs(tmp_path):
    # EBU Tech 3341 case 1: stereo 997 Hz sine at -23 dBFS measures -23.0 LUFS.
    amp = 10 ** (-23 / 20)
    path = write(tmp_path, "ref.wav", stereo(sine(997, 20, amp)))
    loud = analysis.analyze(path)["loudness"]
    assert loud["integrated_lufs"] == pytest.approx(-23.0, abs=0.1)
    assert loud["true_peak_dbtp"] == pytest.approx(-23.0, abs=0.2)


def test_loudness_matches_pyloudnorm_on_music_like_signal(tmp_path):
    pyln = pytest.importorskip("pyloudnorm")
    rng = np.random.default_rng(1)
    x = 0.3 * click_track(124, 12) + tone(41, 12) + 0.02 * rng.standard_normal(12 * SR)
    x[SR * 4:SR * 6] *= 0.1  # a quiet passage so gating matters
    data = stereo(x) * [1.0, 0.8]
    path = write(tmp_path, "music.wav", data)
    ours = analysis.analyze(path)["loudness"]["integrated_lufs"]
    theirs = pyln.Meter(SR).integrated_loudness(data)
    assert ours == pytest.approx(theirs, abs=0.1)


def test_silence_is_flagged(tmp_path):
    path = write(tmp_path, "silence.wav", np.zeros(SR))
    assert analysis.analyze(path)["silent"] is True


# ------------------------------------------------------------------ spectrum

def test_bands_and_brightness(tmp_path):
    low = analysis.analyze(write(tmp_path, "sub.wav", sine(45, 4)))
    assert max(low["spectrum"]["bands"], key=lambda b: b["share_pct"])["band"] == "sub"
    assert low["spectrum"]["brightness"] == "dark"

    noise = np.random.default_rng(2).standard_normal(4 * SR) * 0.1
    from scipy import signal
    hats = signal.sosfilt(signal.butter(4, 7000, "highpass", fs=SR, output="sos"), noise)
    high = analysis.analyze(write(tmp_path, "hats.wav", hats))
    assert max(high["spectrum"]["bands"], key=lambda b: b["share_pct"])["band"] == "air"
    assert high["spectrum"]["brightness"] == "bright"


def test_stereo_and_low_end_mono(tmp_path):
    bass = sine(60, 4, 0.4)
    wide_bass = np.column_stack([bass, -bass * 0.2 + sine(63, 4, 0.3)])
    r = analysis.analyze(write(tmp_path, "wide.wav", wide_bass))
    assert r["stereo"]["low_end_correlation"] < 0.7
    assert any("mono" in o for o in r["observations"])

    mono = analysis.analyze(write(tmp_path, "mono.wav", stereo(bass)))
    assert mono["stereo"]["correlation"] == pytest.approx(1.0)
    assert mono["stereo"]["width"] == "mono or near mono"


# ------------------------------------------------------------ rhythm and key

@pytest.mark.parametrize("bpm", [124.0, 128.0, 174.0])
def test_tempo_detection(tmp_path, bpm):
    r = analysis.analyze(write(tmp_path, f"click{bpm}.wav", click_track(bpm, 16)))
    assert r["rhythm"]["tempo_bpm_detected"] == pytest.approx(bpm, abs=1.0)


def test_onsets_per_beat_uses_given_tempo(tmp_path):
    # 16th notes at 125 BPM = 4 onsets per beat.
    r = analysis.analyze(write(tmp_path, "sixteenths.wav", click_track(500, 16)), tempo_bpm=125)
    assert r["rhythm"]["onsets_per_beat"] == pytest.approx(4.0, abs=0.3)
    assert r["rhythm"]["density"] == "busy"


def test_key_f_minor(tmp_path):
    # i - iv - v - i in F minor, with bass roots.
    chords = [(53, 56, 60, 41), (58, 61, 65, 46), (60, 63, 67, 48), (53, 56, 60, 41)]
    x = np.concatenate([sum(tone(n, 2.0) for n in chord) for chord in chords])
    k = analysis.analyze(write(tmp_path, "fminor.wav", x))["key"]
    assert k["key"] == "F minor"


def test_drums_have_no_clear_key(tmp_path):
    k = analysis.analyze(write(tmp_path, "drums.wav", click_track(125, 8)))["key"]
    assert k["key"] is None


# --------------------------------------------------------------- comparison

def test_camelot_and_compatibility():
    assert compare.camelot(5, "minor") == (4, "A")   # F minor = 4A
    assert compare.camelot(8, "major") == (4, "B")   # Ab major = 4B (relative)
    assert compare.camelot(0, "major") == (8, "B")
    assert compare.key_compatibility((5, "minor"), (5, "minor"))[0] == 1.0
    assert compare.key_compatibility((5, "minor"), (8, "major"))[1] == "relative major/minor"
    assert compare.key_compatibility((5, "minor"), (0, "minor"))[0] == 0.8   # C minor = 5A
    assert compare.key_compatibility((5, "minor"), (11, "minor"))[0] == 0.0  # B minor = 10A


def test_reference_comparison_finds_the_boosted_band(tmp_path):
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(10 * SR) * 0.05
    from scipy import signal
    lowmid = signal.sosfilt(signal.butter(2, [250, 500], "bandpass", fs=SR, output="sos"), noise) * 4
    mix = write(tmp_path, "mix.wav", stereo(noise + lowmid))
    ref = write(tmp_path, "ref.wav", stereo(noise))
    out = analysis.public(compare.compare_to_reference(analysis.analyze(mix), analysis.analyze(ref)))
    worst = max(out["tonal_balance_diff_db"].items(), key=lambda kv: kv[1])
    assert worst[0] in ("250", "500") and worst[1] > 2
    assert any("cut" in s for s in out["suggestions"])


def test_sample_fit_prefers_open_bands_and_matching_key(tmp_path):
    bass = analysis.analyze(write(tmp_path, "bass.wav", tone(41, 4)), quick=True)
    kick = analysis.analyze(write(tmp_path, "kick.wav", sine(50, 4)), quick=True)
    space = compare.frequency_space([dict(bass, label="bass"), dict(kick, label="kick")])
    assert "air" in space["open_bands"]

    from scipy import signal
    noise = np.random.default_rng(4).standard_normal(4 * SR) * 0.1
    hats = analysis.analyze(write(tmp_path, "hats.wav",
                                  signal.sosfilt(signal.butter(4, 7000, "highpass", fs=SR, output="sos"), noise)),
                            quick=True)
    sub = analysis.analyze(write(tmp_path, "sub2.wav", sine(55, 4)), quick=True)
    f_minor = (5, "minor")
    hat_score = compare.score_sample(hats, space, f_minor)
    sub_score = compare.score_sample(sub, space, f_minor)
    assert hat_score["score"] > sub_score["score"]
    assert hat_score["clash"] < sub_score["clash"]

    lead_in_key = analysis.analyze(write(tmp_path, "lead_f.wav", sum(tone(n, 4) for n in (77, 80, 84))), quick=True)
    lead_off_key = analysis.analyze(write(tmp_path, "lead_b.wav", sum(tone(n, 4) for n in (83, 86, 90))), quick=True)
    assert (compare.score_sample(lead_in_key, space, f_minor)["score"]
            > compare.score_sample(lead_off_key, space, f_minor)["score"])


def test_cache_returns_same_result_and_handles_regions(tmp_path):
    path = write(tmp_path, "region.wav", np.concatenate([sine(100, 2), sine(5000, 2)]))
    first = analysis.analyze(path, 0.0, 2.0)
    assert analysis.analyze(path, 0.0, 2.0) is first
    second_half = analysis.analyze(path, 2.0, 4.0)
    assert second_half["spectrum"]["centroid_hz"] > first["spectrum"]["centroid_hz"]
    assert second_half["duration_sec"] == pytest.approx(2.0)


def test_long_file_memory_path(tmp_path):
    # Longer than one 30 s STFT chunk and one 2^20-sample filter chunk.
    path = write(tmp_path, "long.wav", stereo(click_track(125, 70) + tone(41, 70)))
    r = analysis.analyze(path)
    assert r["rhythm"]["tempo_bpm_detected"] == pytest.approx(125, abs=1)
    assert r["loudness"]["integrated_lufs"] is not None


def test_true_peak_catches_intersample_peaks(tmp_path):
    # A sine at fs/4 sampled 45 degrees off its peaks: samples reach only 0.707 of the real peak.
    n = np.arange(SR * 4)
    x = 0.9 * np.sin(2 * np.pi * n / 4 + np.pi / 4)
    loud = analysis.analyze(write(tmp_path, "isp.wav", stereo(x)))["loudness"]
    assert loud["sample_peak_dbfs"] == pytest.approx(20 * np.log10(0.9 * np.sqrt(0.5)), abs=0.1)
    assert loud["true_peak_dbtp"] == pytest.approx(20 * np.log10(0.9), abs=0.3)


def test_clip_region_from_warp_markers():
    from livepilot.server import _beats_to_seconds
    # 120 BPM file: 1 beat = 0.5 s, with the audio starting 0.25 s into the file.
    markers = [{"beat_time": 0.0, "sample_time": 0.25}, {"beat_time": 8.0, "sample_time": 4.25}]
    assert _beats_to_seconds(4.0, markers) == pytest.approx(2.25)
    assert _beats_to_seconds(16.0, markers) == pytest.approx(8.25)   # extrapolated past the last marker
    assert _beats_to_seconds(-1.0, markers) == pytest.approx(-0.25)  # before the first
    tempo_change = markers + [{"beat_time": 16.0, "sample_time": 6.25}]  # second half twice as fast
    assert _beats_to_seconds(12.0, tempo_change) == pytest.approx(5.25)
