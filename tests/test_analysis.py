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

@pytest.mark.parametrize("bpm,expected", [(124.0, 124.0), (128.0, 128.0),
                                          # Without a song tempo, fast tempos read at half speed:
                                          # the same pulse, settled by the 110 BPM preference.
                                          (174.0, 87.0)])
def test_tempo_detection(tmp_path, bpm, expected):
    r = analysis.analyze(write(tmp_path, f"click{bpm}.wav", click_track(bpm, 16)))
    assert r["rhythm"]["tempo_bpm_detected"] == pytest.approx(expected, abs=1.0)


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


def test_loop_length_settles_three_four_ratio_errors(tmp_path):
    # A 4-bar loop at 100 BPM with 16th-note hats: the classic case that read as 133 BPM.
    bpm, bars = 100.0, 4
    x = click_track(bpm * 4, bars * 240 / bpm) * 0.3 + click_track(bpm, bars * 240 / bpm)
    r = analysis.analyze(write(tmp_path, "loop100.wav", x))["rhythm"]
    assert r["tempo_bpm_detected"] == pytest.approx(100.0, abs=0.5)
    assert r["tempo_method"].startswith("loop length")


def test_song_tempo_settles_double_or_half(tmp_path):
    x = click_track(87.0, 16)
    unhinted = analysis.analyze(write(tmp_path, "dnb.wav", x))["rhythm"]["tempo_bpm_detected"]
    hinted = analysis.analyze(write(tmp_path, "dnb2.wav", x), tempo_bpm=174.0)["rhythm"]
    assert unhinted == pytest.approx(87.0, abs=1.0)
    assert hinted["tempo_bpm_detected"] == pytest.approx(174.0, abs=1.0)
    assert "song tempo" in hinted["tempo_method"]


def test_no_tempo_reported_without_a_steady_beat(tmp_path):
    # Notes at random times, like a rubato vocal phrase.
    rng = np.random.default_rng(5)
    x = np.zeros(12 * SR)
    for start in np.sort(rng.uniform(0, 11.5, 14)):
        i = int(start * SR)
        x[i:i + SR // 3] += tone(60 + rng.integers(0, 12), 1 / 3) * np.hanning(SR // 3)
    r = analysis.analyze(write(tmp_path, "rubato.wav", x))["rhythm"]
    assert r["tempo_bpm_detected"] is None and "no steady beat" in r["tempo_note"]


def test_played_region_of_trimmed_arrangement_clip():
    from livepilot.server import _played_beats
    # The clip is trimmed to 192 beats in the arrangement but its end marker is still the file's end.
    info = {"start_time": 64.0, "end_time": 256.0, "start_marker": 8.0, "end_marker": 449.0,
            "looping": False, "loop_start": 8.0, "loop_end": 449.0}
    assert _played_beats(info)[:2] == (8.0, 200.0)
    looped = dict(info, looping=True, loop_start=0.0, loop_end=16.0)
    assert _played_beats(looped)[:2] == (0.0, 16.0)                    # loop repeats: whole loop
    short = dict(looped, start_time=0.0, end_time=4.0, start_marker=2.0)
    assert _played_beats(short)[:2] == (2.0, 6.0)                       # shorter than one loop
    # Unlooped, Live plays loop_start..loop_end (the clip's start and end) and ignores the markers.
    session = {"start_marker": 0.0, "end_marker": 32.0, "looping": False, "loop_start": 4.0, "loop_end": 20.0}
    assert _played_beats(session)[:2] == (4.0, 20.0)
    assert _played_beats(dict(session, looping=True, loop_start=0.0, loop_end=32.0))[:2] == (0.0, 32.0)
    trimmed = dict(info, start_marker=0.0)  # unlooped arrangement clip: also from loop_start
    assert _played_beats(trimmed)[:2] == (8.0, 200.0)


def drifting_stems(tmp_path, bpm_from=120.0, bpm_to=126.0, seconds=40.0, first_beat=0.3, sr=SR):
    """Drums and chords whose tempo ramps like a Suno song. The music starts on a pickup beat,
    so the first downbeat is the second beat. Returns (paths, beat times)."""
    beats, t = [], first_beat
    while t < seconds - 1:
        beats.append(t)
        t += 60.0 / (bpm_from + (bpm_to - bpm_from) * t / seconds)
    n = int(seconds * sr)
    drums, chords = np.zeros(n), np.zeros(n)
    rng = np.random.default_rng(1)
    kick = np.sin(2 * np.pi * 55 * np.arange(int(0.15 * sr)) / sr) * np.exp(-np.arange(int(0.15 * sr)) / (0.04 * sr))
    snare = rng.standard_normal(int(0.08 * sr)) * np.exp(-np.arange(int(0.08 * sr)) / (0.02 * sr))
    progression = [(57, 60, 64), (53, 57, 60), (55, 59, 62), (52, 55, 59)]  # Am F G Em
    for k, b in enumerate(beats):
        i = int(b * sr)
        hit = kick if (k - 1) % 2 == 0 else 0.4 * snare  # kick on 1 and 3, snare on 2 and 4
        drums[i:i + len(hit)] += 0.8 * hit[:n - i]
        if (k - 1) % 4 == 0 and k + 4 < len(beats):  # a new chord each bar, from the downbeat
            j = int(beats[k + 4] * sr)
            tt = np.arange(j - i) / sr
            chords[i:j] += sum(0.15 * np.sin(2 * np.pi * 440 * 2 ** ((p - 69) / 12) * tt)
                               for p in progression[(k // 4) % 4]) * np.exp(-tt / 1.5)
    return [write(tmp_path, "drums.wav", drums), write(tmp_path, "chords.wav", chords)], np.array(beats)


def test_beat_grid_follows_a_drifting_tempo_and_finds_the_downbeat(tmp_path):
    from livepilot.analysis import beats

    (drums, chords), truth = drifting_stems(tmp_path)
    g = beats.beat_grid([drums, chords], [drums], tempo_hint=123)
    found = np.array(g["beats"])
    near = np.array([found[np.argmin(np.abs(found - t))] for t in truth])
    assert np.max(np.abs(near - truth)) < 0.008
    assert found[g["first_downbeat"]] == pytest.approx(truth[1], abs=0.008)
    assert g["tempo_bpm"]["start"] == pytest.approx(120.5, abs=0.7)
    assert g["tempo_bpm"]["end"] == pytest.approx(125.5, abs=0.7)
    assert g["steady"] is False
