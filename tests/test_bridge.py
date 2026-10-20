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
