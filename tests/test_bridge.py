"""End-to-end test: MCP-side LiveConnection -> socket -> Remote Script -> fake Live song."""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "remote_script"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(Path(__file__).parent))

import fake_live  # noqa: E402

fake_live.install()

from LivePilot import control_surface  # noqa: E402
from livepilot import music  # noqa: E402
from livepilot.connection import LiveConnection, LiveError  # noqa: E402

TEST_PORT = 19877


@pytest.fixture()
def live():
    control_surface.PORT = TEST_PORT
    surface = control_surface.LivePilot(None)
    stop = threading.Event()

    def main_thread():
        while not stop.is_set():
            surface.update_display()
            time.sleep(0.005)

    pump = threading.Thread(target=main_thread, daemon=True)
    pump.start()
    conn = LiveConnection(port=TEST_PORT, timeout=5)
    yield conn, surface.song()
    conn.close()
    stop.set()
    surface.disconnect()
    time.sleep(1.1)  # let the accept loop notice the closed socket


def test_session_info_and_tempo(live):
    conn, song = live
    assert conn.send("set_tempo", bpm=128)["tempo"] == 128
    info = conn.send("get_session_info")
    assert info["tempo"] == 128
    assert info["tracks"][0]["name"] == "1-MIDI"


def test_unknown_command_and_bad_index(live):
    conn, _ = live
    with pytest.raises(LiveError, match="unknown command"):
        conn.send("nope")
    with pytest.raises(LiveError, match="out of range"):
        conn.send("get_track_info", track_index=5)


def test_clip_notes_roundtrip(live):
    conn, song = live
    conn.send("create_midi_track", name="Drums")
    conn.send("create_clip", track_index=1, slot_index=0, length=4)
    notes = music.drum_pattern("house", bars=1, seed=1)
    assert conn.send("add_notes", track_index=1, slot_index=0, notes=notes)["added"] == len(notes)
    got = conn.send("get_notes", track_index=1, slot_index=0)["notes"]
    assert len(got) == len(notes)
    assert sum(1 for n in got if n["pitch"] == 36) == 4

    conn.send("transpose_notes", track_index=1, slot_index=0, semitones=12, pitch=36)
    got = conn.send("get_notes", track_index=1, slot_index=0)["notes"]
    assert sum(1 for n in got if n["pitch"] == 48) == 4


def test_quantize(live):
    conn, _ = live
    conn.send("create_clip", track_index=0, slot_index=0, length=4)
    conn.send("add_notes", track_index=0, slot_index=0,
              notes=[{"pitch": 60, "start": 0.27, "duration": 0.2}, {"pitch": 62, "start": 1.1, "duration": 0.5}])
    conn.send("quantize_notes", track_index=0, slot_index=0, grid=0.25, amount=1.0)
    starts = [n["start"] for n in conn.send("get_notes", track_index=0, slot_index=0)["notes"]]
    assert starts == [0.25, 1.0]


def test_volume_db(live):
    conn, song = live
    result = conn.send("set_track_volume", track_index=0, db=-6.0)
    assert result["volume"] == "-6.0 dB"


def test_music_helpers():
    assert music.note_number("C", 3) == 60
    assert music.note_number("A", 1) == 45
    chords = music.chord_progression("C", "major", [1, 4], sevenths=False)
    assert [n["pitch"] for n in chords[:3]] == [60, 64, 67]
    assert [n["pitch"] for n in chords[3:]] == [65, 69, 72]
    bass = music.bassline("A", "minor", bars=2, style="root_eighths")
    assert len(bass) == 16 and bass[0]["pitch"] == 45


def test_song_key(live):
    conn, song = live
    key = conn.send("set_song_key", root_note=5, scale_name="Minor", scale_mode=True)
    assert key["root"] == "F" and key["scale"] == "Minor" and song.scale_mode is True
    assert conn.send("get_session_info")["key"]["root"] == "F"


def test_search_finds_plugin_despite_huge_sample_library(live):
    conn, _ = live
    found = conn.send("search_browser", query="serum 2")["results"]
    assert found[0]["name"] == "Serum 2" and found[0]["rank"] == 0 and found[0]["category"] == "plugins"
    # A preset that merely contains the word ranks below the exact plugin match.
    names = [r["name"] for r in conn.send("search_browser", query="serum")["results"]]
    assert names[0] == "Serum 2" and "Serum Bass Lead.adv" in names
    assert conn.send("search_browser", query="808 core kit")["results"][0]["rank"] == 0


def test_tech_house_patterns():
    drums = music.drum_pattern("tech_house", bars=4, variation=0.5, seed=3)
    kicks = sorted(n["start"] for n in drums if n["pitch"] == music.DRUMS["kick"])
    assert kicks == [float(b) for b in range(16)]  # kick is never varied away
    bar1 = {(n["pitch"], n["start"]) for n in drums if n["start"] < 4}
    bar2 = {(n["pitch"], n["start"] - 4) for n in drums if 4 <= n["start"] < 8}
    assert bar1 != bar2  # variation changed the answering bar

    bass = music.bassline("F", "minor", bars=2, style="tech_house", seed=1)
    assert all(n["start"] % 1 != 0 for n in bass)  # stays off the kick
    assert {n["pitch"] % 12 for n in bass} <= {5, 0}  # F root and its fifth C

    stabs = music.chord_progression("F", "minor", [1], beats_per_chord=4, rhythm="offbeat_stabs")
    assert len(stabs) == 5 * 3 and stabs[0]["pitch"] == 65


def test_arrangement_clips(live):
    conn, song = live
    track = song.tracks[0]
    # Live doesn't promise any order; indices must follow start time.
    late, early = fake_live.FakeClip(8, start_time=32.0), fake_live.FakeClip(4, start_time=0.0)
    late.name, early.name = "Drop", "Intro"
    track.arrangement_clips = [late, early]

    listing = conn.send("get_arrangement_clips")
    assert [(c["track_index"], c["arrangement_clip_index"], c["name"]) for c in listing] == [
        (0, 0, "Intro"), (0, 1, "Drop")]
    assert conn.send("get_track_info", track_index=0)["arrangement_clips"][1]["name"] == "Drop"

    info = conn.send("get_clip_info", track_index=0, arrangement_clip_index=1)
    assert info["start_time"] == 32.0 and info["end_time"] == 40.0

    conn.send("add_notes", track_index=0, arrangement_clip_index=1,
              notes=[{"pitch": 60, "start": 0.3, "duration": 0.5}])
    conn.send("quantize_notes", track_index=0, arrangement_clip_index=1, grid=0.25)
    assert conn.send("get_notes", track_index=0, arrangement_clip_index=1)["notes"][0]["start"] == 0.25
    assert early.notes == []  # the other clip was untouched

    conn.send("set_clip_muted", track_index=0, arrangement_clip_index=0, muted=True)
    assert early.muted is True
    assert conn.send("delete_arrangement_clip", track_index=0, arrangement_clip_index=0)["deleted"] == "Intro"
    assert track.arrangement_clips == [late]

    with pytest.raises(LiveError, match="out of range"):
        conn.send("get_clip_info", track_index=0, arrangement_clip_index=5)
    with pytest.raises(LiveError, match="pass slot_index"):
        conn.send("get_notes", track_index=0)


def test_arrangement_listing_skips_group_tracks(live):
    conn, song = live
    song.tracks.insert(0, fake_live.FakeGroupTrack("Drums Group"))
    clip = fake_live.FakeClip(4, start_time=0.0)
    clip.name = "Vox"
    song.tracks[1].arrangement_clips = [clip]

    listing = conn.send("get_arrangement_clips")
    assert [(c["track_index"], c["track"], c["name"]) for c in listing] == [(1, "1-MIDI", "Vox")]
    assert conn.send("get_arrangement_clips", track_index=0) == []
    assert conn.send("get_track_info", track_index=0)["arrangement_clips"] == []
    with pytest.raises(LiveError, match="out of range"):
        conn.send("get_clip_info", track_index=0, arrangement_clip_index=0)


@pytest.mark.parametrize("tool,args", [
    ("get_arrangement_clips", {}), ("get_devices", {"track_index": 0}),
    ("browse", {"category": "drums"}), ("manage_locator", {"action": "list"}),
    ("raw_command", {"command": "get_locators"}),
])
def test_list_results_arrive_as_one_block(monkeypatch, tool, args):
    # MCP turns a returned list into one content block per item, so tools must wrap lists.
    import asyncio
    from livepilot import server

    rows = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    monkeypatch.setattr(server, "_send", lambda cmd, **p: rows)
    monkeypatch.setattr(server.live, "send", lambda cmd, **p: rows)
    result = asyncio.run(server.mcp.call_tool(tool, args))
    assert len(result.content) == 1


def test_analyser_tools_end_to_end(live, monkeypatch, tmp_path):
    """MCP tool -> socket -> fake Live for clip lookups -> analyser on real files."""
    import asyncio
    import json

    import numpy as np
    import soundfile as sf
    from livepilot import server

    conn, song = live
    monkeypatch.setattr(server, "live", conn)
    sr = 44100
    t = np.arange(4 * sr) / sr
    vocal_path, hats_path = str(tmp_path / "vocal.wav"), str(tmp_path / "hats.wav")
    sf.write(vocal_path, 0.3 * np.sin(2 * np.pi * 330 * t), sr)
    sf.write(hats_path, 0.05 * np.random.default_rng(0).standard_normal(len(t)) * (np.sin(2 * np.pi * 8 * t) > 0.9), sr)

    class AudioClip(fake_live.FakeClip):
        is_midi_clip, is_audio_clip = False, True
        gain_display_string, pitch_coarse, pitch_fine, warping, warp_mode, warp_markers = "0.0 dB", 0, 0.0, False, 0, []

    audio = fake_live.FakeTrack("Vox")
    audio.has_midi_input, audio.has_audio_input = False, True
    audio.clip_slots[0].clip = AudioClip(8)
    audio.clip_slots[0].clip.name, audio.clip_slots[0].clip.file_path = "Lead", vocal_path
    song.tracks.append(audio)  # track 1; track 0 is the fake MIDI track

    def call(tool, args):
        result = asyncio.run(server.mcp.call_tool(tool, args))
        assert len(result.content) == 1
        return json.loads(result.content[0].text)

    clip = call("analyze_clip", {"track_index": 1, "slot_index": 0})
    assert clip["clip"]["region"]["description"].startswith("whole file") and clip["spectrum"]["bands"]

    # The MIDI track makes sound only where it has active Arrangement clips.
    midi = song.tracks[0]
    assert "not_measured" not in call("analyze_frequency_space", {})
    midi.arrangement_clips = [fake_live.FakeClip(8, 0.0), fake_live.FakeClip(8, 8.0), fake_live.FakeClip(16, 32.0)]
    midi.arrangement_clips[2].muted = True
    space = call("analyze_frequency_space", {})
    assert space["files"] == 1 and space["not_measured"] == [{"track": "1-MIDI", "beats": [[0.0, 16.0]]}]

    ranked = call("score_samples", {"candidate_paths": [hats_path, vocal_path]})
    assert ranked["context_files"] == 1 and "warning" in ranked
    assert ranked["ranked"][0]["file"] == hats_path  # fills space the vocal leaves open

    song.tracks[0].clip_slots[0].create_clip(4)
    assert "MIDI clip" in call("analyze_clip", {"track_index": 0, "slot_index": 0})["error"]


def test_locators_land_where_asked_not_at_the_old_playhead(live):
    conn, song = live
    song._song_time = 8.0  # playhead somewhere else, as in a real session

    added = conn.send("add_locator", time=32.0, name="Drop")
    assert added["name"] == "Drop" and added["time"] == 32.0
    conn.send("add_locator", time=0.0, name="Intro")
    conn.send("add_locator", time=64.0, name="Outro")
    listing = conn.send("get_locators")
    assert [(c["time"], c["name"]) for c in listing] == [(0.0, "Intro"), (32.0, "Drop"), (64.0, "Outro")]
    assert conn.send("get_session_info")["song_time"] == 8.0  # playhead restored

    # Live keeps cues in its own order; every listing must use the same time-ordered indices.
    song.cue_points.reverse()
    assert conn.send("get_session_info")["locators"] == conn.send("get_locators")
    assert conn.send("get_session_info")["locators"][0]["name"] == "Intro"

    # Adding where one exists renames it instead of toggling it away.
    assert conn.send("add_locator", time=32.0, name="Drop 1")["existing"] is True

    deleted = conn.send("delete_locator", index=1)
    assert deleted["deleted"] == "Drop 1"
    assert [c["name"] for c in deleted["locators"]] == ["Intro", "Outro"]

    conn.send("rename_locator", index=1, name="End")
    assert [c["name"] for c in conn.send("get_locators")] == ["Intro", "End"]

    song.is_playing = True
    with pytest.raises(LiveError, match="stop playback first"):
        conn.send("add_locator", time=16.0)
    assert len(song.cue_points) == 2


def test_other_clients_still_complete_during_a_multi_tick_command(live):
    """A command from a second client queues behind add_locator and still completes (no deadlock)."""
    conn, song = live
    other = LiveConnection(port=TEST_PORT, timeout=5)
    results = {}

    def add():
        results["add"] = conn.send("add_locator", time=48.0, name="Break")

    worker = threading.Thread(target=add)
    worker.start()
    tempo = other.send("set_tempo", bpm=130)
    worker.join()
    other.close()
    assert results["add"]["time"] == 48.0 and tempo["tempo"] == 130


def test_continuous_parameters_by_display_value(live):
    conn, song = live
    eq = fake_live.FakeDevice("EQ Eight", [fake_live.FakeParam("1 Frequency A", 0.5, kind="freq"),
                                           fake_live.FakeParam("Release", 0.5, kind="time"),
                                           fake_live.FakeParam("1 Resonance A", 0.5, kind="plain")])
    song.tracks[0].devices.append(eq)

    def show(param, text):
        return conn.send("set_device_parameter", track_index=0, device_index=0, parameter=param,
                         display_value=text)["display"]

    assert show("Frequency", "350 Hz") == "350 Hz"
    assert show("Frequency", "2.5 kHz") == "2.50 kHz"
    assert show("Frequency", "1200 Hz") == "1.20 kHz"  # crosses the Hz/kHz display switch
    assert show("Release", "150 ms") == "150.0 ms"
    assert show("Release", "1.5 s") == "1.50 s"
    assert show("Resonance", "0.71") == "0.71"  # no unit
    with pytest.raises(LiveError, match="not '-18 dB'"):
        show("Frequency", "-18 dB")
    with pytest.raises(LiveError, match="can't read"):
        show("Frequency", "bright")


def test_load_at_position_and_move_device(live, monkeypatch):
    import Live
    conn, song = live
    track = song.tracks[0]
    track.devices[:] = [fake_live.FakeDevice("Ozone 11")]
    browser = Live.Application.get_application().browser
    monkeypatch.setattr(browser, "load_item",
                        lambda item: song.view.selected_track.devices.append(fake_live.FakeDevice(item.name)))
    conn.send("search_browser", query="EQ Eight", category="audio_effects")
    loaded = conn.send("load_browser_item", uri="query:EQ Eight", track_index=0, position=0)
    assert loaded["devices"] == ["EQ Eight", "Ozone 11"]
    moved = conn.send("move_device", track_index=0, device_index=0, position=1)
    assert moved["devices"] == ["Ozone 11", "EQ Eight"]
    track.devices.append(fake_live.FakeDevice("Utility"))
    moved = conn.send("move_device", track_index=0, device_index=0, position=1)
    assert moved["devices"] == ["EQ Eight", "Ozone 11", "Utility"]
    moved = conn.send("move_device", track_index=0, device_index=2, position=0)
    assert moved["devices"] == ["Utility", "EQ Eight", "Ozone 11"]


def test_source_bpm_warps_whole_file_at_that_tempo(live):
    conn, song = live

    class AudioClip(fake_live.FakeClip):
        is_midi_clip, is_audio_clip = False, True
        gain_display_string, pitch_coarse, pitch_fine, warp_mode = "0.0 dB", 0, 0.0, 0
        sample_rate, sample_length = 48000, 48000 * 217.5  # a 217.5 s acapella

        def __init__(self):
            super().__init__(478.5)
            self.looping, self.warping = False, False
            # What Live gives an unwarped 217.5 s file in a 132 BPM set once warping is on.
            self.real_markers = [fake_live.types.SimpleNamespace(beat_time=0.0, sample_time=0.0),
                                 fake_live.types.SimpleNamespace(beat_time=478.5, sample_time=217.5)]

        @property
        def warp_markers(self):
            # Like Live, list an unremovable marker 1/32 beat after the last real one.
            last = max(self.real_markers, key=lambda m: m.beat_time)
            return self.real_markers + [fake_live.types.SimpleNamespace(
                beat_time=last.beat_time + 0.03125, sample_time=last.sample_time + 0.03125 * 60 / 132)]

        def add_warp_marker(self, m):
            self.real_markers.append(m)

        def remove_warp_marker(self, beat_time):
            if not any(m.beat_time == beat_time for m in self.real_markers):
                raise RuntimeError("no warp marker at %s" % beat_time)
            self.real_markers = [m for m in self.real_markers if m.beat_time != beat_time]

    clip = AudioClip()
    song.tracks[0].clip_slots[0].clip = clip
    result = conn.send("set_audio_clip_properties", track_index=0, slot_index=0, source_bpm=128)
    assert result["warping"] is True
    assert [(m.beat_time, m.sample_time) for m in clip.real_markers] == [(0.0, 0.0), (464.0, 217.5)]
    assert clip.end_marker == clip.loop_end == 464.0


def test_copy_clip_to_a_slot_on_the_same_track(live, monkeypatch):
    import asyncio
    from livepilot import server

    conn, song = live
    monkeypatch.setattr(server, "live", conn)
    track = song.tracks[0]
    track.clip_slots[0].create_clip(8)
    asyncio.run(server.mcp.call_tool("duplicate_clip", {"track_index": 0, "slot_index": 0, "target_slot_index": 3}))
    assert track.clip_slots[3].has_clip and not track.clip_slots[1].has_clip


def test_markers_on_an_unlooped_clip_set_its_region(live):
    conn, song = live
    song.tracks[0].clip_slots[0].create_clip(464)
    clip = song.tracks[0].clip_slots[0].clip
    clip.looping = False
    result = conn.send("set_clip_loop", track_index=0, slot_index=0, start_marker=32, end_marker=192)
    assert (result["loop_start"], result["loop_end"]) == (32.0, 192.0)
    assert (result["start_marker"], result["end_marker"]) == (32.0, 192.0)


def test_menu_entries_match_by_number_or_prefix(live):
    conn, song = live
    song.tracks[0].devices.append(fake_live.live_device("Glue Compressor"))

    def show(param, text):
        return conn.send("set_device_parameter", track_index=0, device_index=0, parameter=param,
                         display_value=text)["display"]

    assert show("Ratio", "2:1") == "2"
    assert show("Attack", "10 ms") == "10"
    assert show("Attack", "0.30") == "0.3"
    assert show("Release", "Auto") == "A"
    with pytest.raises(LiveError, match="not in"):
        show("Ratio", "3")


@pytest.fixture()
def mcp_call(live, monkeypatch):
    import asyncio
    import json

    import Live
    from livepilot import server

    conn, song = live
    monkeypatch.setattr(server, "live", conn)
    browser = Live.Application.get_application().browser
    monkeypatch.setattr(browser, "load_item",
                        lambda item: song.view.selected_track.devices.append(fake_live.live_device(item.name)))

    def call(tool, args):
        result = asyncio.run(server.mcp.call_tool(tool, args))
        assert len(result.content) == 1
        return json.loads(result.content[0].text)

    return call, song


def _tone(path, amplitude, seconds=6, sr=44100):
    import numpy as np
    import soundfile as sf
    t = np.arange(seconds * sr) / sr
    sf.write(path, amplitude * np.sin(2 * np.pi * 997 * t), sr)
    return path


def test_master_chain_then_match_loudness(mcp_call, tmp_path):
    call, song = mcp_call
    chain = call("build_master_chain", {"target": "streaming"})
    assert chain["chain"] == ["Glue Compressor", "Limiter"] and "failed" not in chain
    glue, limiter = song.master_track.devices
    assert [p.str_for_value(p.value) for p in glue.parameters[1:4]] == ["2", "10", "A"]
    assert limiter.parameters[2].str_for_value(limiter.parameters[2].value) == "-1.0 dB"
    assert call("build_master_chain", {})["added"] == []  # nothing added twice

    # A 997 Hz sine at -20 dBFS peak reads -23 LUFS (EBU test signal), 9 LU under streaming.
    mix = _tone(str(tmp_path / "mix.wav"), 10 ** (-20 / 20))
    r = call("match_loudness", {"mix_path": mix})
    assert abs(r["measured_lufs"] + 23.0) < 0.2 and abs(r["change_db"] - 9.0) < 0.2
    assert abs(limiter.parameters[1].value - 9.0) < 0.2 and "warning" in r

    on_target = _tone(str(tmp_path / "loud.wav"), 10 ** (-11 / 20))
    assert call("match_loudness", {"mix_path": on_target})["status"].startswith("on target")


def test_match_loudness_uses_a_utility_before_a_plugin(mcp_call, tmp_path):
    call, song = mcp_call
    song.master_track.devices[:] = [fake_live.FakeDevice("Ozone 11", class_name="PluginDevice")]
    assert "error" in call("build_master_chain", {})
    r = call("match_loudness", {"mix_path": _tone(str(tmp_path / "mix.wav"), 10 ** (-17 / 20))})
    assert [d.name for d in song.master_track.devices] == ["Utility", "Ozone 11"]
    assert song.master_track.devices[0].parameters[1].value == pytest.approx(6.0, abs=0.2)  # -20 LUFS -> -14
    assert "Ozone 11" in r["note"]


def test_gain_stage_sets_faders_to_the_target(mcp_call, tmp_path):
    call, song = mcp_call

    class AudioClip(fake_live.FakeClip):
        is_midi_clip, is_audio_clip = False, True
        gain_display_string, pitch_coarse, pitch_fine, warping, warp_mode, warp_markers = "-3.0 dB", 0, 0.0, False, 0, []

    vox = fake_live.FakeTrack("Vox")
    vox.has_midi_input, vox.has_audio_input = False, True
    clip = AudioClip(16, 0.0)
    clip.name, clip.file_path = "Lead", _tone(str(tmp_path / "vox.wav"), 10 ** (-20 / 20))
    vox.arrangement_clips = [clip]
    song.tracks.append(vox)

    # Source -23 LUFS plus -3 dB clip gain = -26; with a +2 offset the target is -16: fader +10,
    # beyond the fader's +6.
    r = call("gain_stage", {"target_lufs": -18.0, "track_offsets": {"vox": 2.0}})
    t = r["tracks"][0]
    assert t["source_lufs"] == pytest.approx(-26.0, abs=0.2) and t["fader_db"] == 6.0 and "note" in t
    assert r["applied"] is False and song.tracks[1].mixer_device.volume.value == 0.85
    r = call("gain_stage", {"target_lufs": -30.0, "apply": True})
    assert r["tracks"][0]["fader_set"] == "-4.0 dB"
