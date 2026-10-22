"""LivePilot MCP server: exposes Ableton Live control as MCP tools.

Conventions shared by every tool:
- Indices are 0-based (track 0 is the leftmost track, slot 0 the top clip slot).
- Times and lengths are in beats (quarter notes); one 4/4 bar = 4 beats.
- kind selects which track list an index refers to: "track", "return", or "master".
- MIDI pitch uses Ableton's convention: C3 = 60. Drum Rack pads start at C1 = 36.

Working style:
- When the user names a key, set it on the Live set with set_song_key before writing music.
  The generators default to the set's key.
- When the user names an instrument or plugin (e.g. Serum 2), load exactly that with
  load_device_by_name. Never substitute a default Live device without saying so.
- Clips in the Arrangement (vocals, recorded audio, placed sections) are edited with the same
  clip and note tools by passing arrangement_clip_index instead of slot_index. Find indices
  with get_arrangement_clips.
- Generators give a starting point. Shape the result with add_notes and edit_notes (vary bars,
  add ghost notes, adjust velocities) instead of leaving raw patterns.
- Plugin parameters are only reachable if Live exposes them (get_device_parameters). Big synths
  (Serum) and mastering or mixing suites (Ozone, Neutron, FabFilter) often expose only a few
  until the user adds them with Configure in the device title bar. Say so rather than guessing.
- Signal flow: a track's devices run first, then its volume fader and pan, then sends (post-fader
  by default). On the master track, the master fader comes after the master devices, so turning
  it down doesn't give a limiter or Ozone more headroom. Use a Utility first in the chain
  (position=0) for gain before the master chain.
- Live's script API can't export, render, bounce or freeze audio. The analyser reads source
  files before any effects, so to check processed audio ask the user to export (or bounce) and
  pass the file to analyze_audio_file.
- You can't hear audio. Before judging or changing a mix, measure it with the analyser tools
  (analyze_clip, analyze_audio_file, compare_to_reference, analyze_frequency_space,
  score_samples) and base decisions on those numbers.
- Imported stems or recordings (AI-generated songs, live takes) often drift in tempo, so no
  song tempo or source_bpm lines them up. Use warp_to_grid (report first, then apply) to warp
  them bar by bar onto the grid.
- Automation (filter sweeps, builds, fades, send throws) is written with record_automation,
  which records it in real time. Put every parameter that moves over the same span in one
  call: one pass records them all.
- Mixing and mastering order: gain_stage for a starting balance (report first, apply once the
  user agrees), then mix moves, then build_master_chain, then the user exports the whole song
  and match_loudness sets the final level. Repeat export + match_loudness until on target.
"""
import math
import os
import re
import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import music
from .connection import LiveConnection, LiveError

TrackKind = Literal["track", "return", "master"]

mcp = MCPServer("livepilot", instructions=__doc__)
live = LiveConnection()


def _send(command: str, **params: Any) -> Any:
    return live.send(command, **{k: v for k, v in params.items() if v is not None})


# ---------------------------------------------------------------- session

@mcp.tool()
def get_session_info(include_clips: bool = True) -> dict:
    """Overview of the Live set: tempo, transport, every track (with devices and clips), returns,
    master, scenes and locators. Call this first to orient yourself."""
    return _send("get_session_info", include_clips=include_clips)


@mcp.tool()
def set_tempo(bpm: float) -> dict:
    """Set the song tempo in BPM (20-999)."""
    return _send("set_tempo", bpm=bpm)


@mcp.tool()
def set_time_signature(numerator: int, denominator: int) -> dict:
    """Set the song time signature, e.g. 4/4, 3/4, 7/8."""
    return _send("set_time_signature", numerator=numerator, denominator=denominator)


@mcp.tool()
def set_song_key(root: str, scale: str = "minor", scale_mode: bool = True) -> dict:
    """Set the Live set's key (Live 12+), e.g. root 'F', scale 'minor'. Call this whenever the user
    names a key. scale_mode highlights the scale in clips and keeps Live's MIDI tools in key.
    scale: major, minor, dorian, phrygian, lydian, mixolydian, harmonic_minor, pentatonic_major,
    pentatonic_minor, blues, or any scale name exactly as Live shows it."""
    live_scale = music.SCALE_TO_LIVE.get(scale.lower().replace(" ", "_"), scale)
    root_note = music.note_number(root, 0) % 12
    return _send("set_song_key", root_note=root_note, scale_name=live_scale, scale_mode=scale_mode)


@mcp.tool()
def transport(action: Literal["play", "continue", "stop", "stop_all_clips"]) -> dict:
    """Start playback from the start marker, continue from the current position, stop,
    or stop all playing session clips."""
    command = {"play": "start_playback", "continue": "continue_playback",
               "stop": "stop_playback", "stop_all_clips": "stop_all_clips"}[action]
    return _send(command)


@mcp.tool()
def set_song_time(beats: float) -> dict:
    """Move the arrangement playhead to a position in beats."""
    return _send("set_song_time", beats=beats)


@mcp.tool()
def set_metronome(enabled: bool) -> dict:
    """Turn the metronome on or off."""
    return _send("set_metronome", enabled=enabled)


@mcp.tool()
def set_arrangement_loop(start: float | None = None, length: float | None = None,
                         enabled: bool | None = None) -> dict:
    """Set the arrangement loop brace (start and length in beats) and/or toggle looping."""
    return _send("set_loop", start=start, length=length, enabled=enabled)


@mcp.tool()
def set_recording(arrangement_record: bool | None = None, session_record: bool | None = None) -> dict:
    """Toggle arrangement record mode and/or session record."""
    result = {}
    if arrangement_record is not None:
        result.update(_send("set_record_mode", enabled=arrangement_record))
    if session_record is not None:
        result.update(_send("set_session_record", enabled=session_record))
    return result


@mcp.tool()
def capture_midi() -> dict:
    """Capture MIDI: turn what was recently played on armed tracks into a clip."""
    return _send("capture_midi")


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Undo the last change(s) in Live."""
    for _ in range(steps):
        _send("undo")
    return {"undone": steps}


@mcp.tool()
def redo(steps: int = 1) -> dict:
    """Redo previously undone change(s)."""
    for _ in range(steps):
        _send("redo")
    return {"redone": steps}


@mcp.tool()
def show_view(view: Literal["Session", "Arranger", "Detail/Clip", "Detail/DeviceChain", "Browser"]) -> dict:
    """Bring a view of Live's window to the front."""
    return _send("show_view", view=view)


# ----------------------------------------------------------------- tracks

@mcp.tool()
def get_track_info(track_index: int, kind: TrackKind = "track") -> dict:
    """Full detail for one track: mixer, sends, devices, clip slots, arrangement clips, routing."""
    return _send("get_track_info", track_index=track_index, kind=kind)


@mcp.tool()
def create_track(type: Literal["midi", "audio", "return"], name: str | None = None, index: int = -1) -> dict:
    """Create a MIDI, audio or return track. index -1 appends at the end."""
    if type == "return":
        return _send("create_return_track", name=name)
    return _send(f"create_{type}_track", index=index, name=name)


@mcp.tool()
def delete_track(track_index: int, kind: Literal["track", "return"] = "track") -> dict:
    """Delete a track or return track."""
    return _send("delete_track", track_index=track_index, kind=kind)


@mcp.tool()
def duplicate_track(track_index: int) -> dict:
    """Duplicate a track (with its clips and devices); the copy lands right after it."""
    return _send("duplicate_track", track_index=track_index)


@mcp.tool()
def set_track_properties(track_index: int, kind: TrackKind = "track", name: str | None = None,
                         volume_db: float | None = None, pan: float | None = None,
                         mute: bool | None = None, solo: bool | None = None, arm: bool | None = None,
                         color_index: int | None = None) -> dict:
    """Change one or more track settings. volume_db is in dB (0 = unity, max +6).
    pan runs -1 (left) to 1 (right). color_index is Live's palette index 0-69."""
    result: dict = {}
    common = {"track_index": track_index, "kind": kind}
    if name is not None:
        result.update(_send("set_track_name", name=name, **common))
    if volume_db is not None:
        result.update(_send("set_track_volume", db=volume_db, **common))
    if pan is not None:
        result.update(_send("set_track_pan", pan=pan, **common))
    if mute is not None:
        result.update(_send("set_track_mute", mute=mute, **common))
    if solo is not None:
        result.update(_send("set_track_solo", solo=solo, **common))
    if arm is not None:
        result.update(_send("set_track_arm", track_index=track_index, arm=arm))
    if color_index is not None:
        result.update(_send("set_track_color", color_index=color_index, **common))
    return result


@mcp.tool()
def set_track_send(track_index: int, send_index: int, db: float, kind: TrackKind = "track") -> dict:
    """Set a track's send level to a return track (send 0 = return A) in dB. Use -70 or lower for off."""
    return _send("set_track_send", track_index=track_index, send_index=send_index, db=db, kind=kind)


@mcp.tool()
def select_track(track_index: int, kind: TrackKind = "track") -> dict:
    """Select a track in Live (browser loads go to the selected track)."""
    return _send("select_track", track_index=track_index, kind=kind)


@mcp.tool()
def get_track_routing(track_index: int) -> dict:
    """Show a track's current input/output routing and all available options."""
    return _send("get_track_routing", track_index=track_index)


@mcp.tool()
def set_track_routing(track_index: int, input: str | None = None, input_channel: str | None = None,
                      output: str | None = None,
                      monitoring: Literal["in", "auto", "off"] | None = None) -> dict:
    """Set routing by display name as Live shows it (e.g. input 'Ext. In' or another track's name,
    output 'Master' or a group/track name). Partial names match. Call get_track_routing for options."""
    result: dict = {}
    if input is not None:
        result.update(_send("set_track_input_routing", track_index=track_index, name=input,
                            channel=input_channel))
    if output is not None:
        result.update(_send("set_track_output_routing", track_index=track_index, name=output))
    if monitoring is not None:
        result.update(_send("set_track_monitoring", track_index=track_index,
                            state={"in": 0, "auto": 1, "off": 2}[monitoring]))
    return result


# ------------------------------------------------------------------ clips

def _ref(track_index: int, slot_index: int | None, arrangement_clip_index: int | None) -> dict:
    return {"track_index": track_index, "slot_index": slot_index,
            "arrangement_clip_index": arrangement_clip_index}


@mcp.tool()
def get_clip_info(track_index: int, slot_index: int | None = None,
                  arrangement_clip_index: int | None = None) -> dict:
    """Clip details: length, loop and markers (plus start/end time for arrangement clips).
    For audio clips this includes the file path, gain, pitch, warp mode and warp markers.
    Pass slot_index for a Session clip or arrangement_clip_index for an Arrangement clip."""
    return _send("get_clip_info", **_ref(track_index, slot_index, arrangement_clip_index))


@mcp.tool()
def get_arrangement_clips(track_index: int | None = None) -> dict:
    """List every clip in the Arrangement (or on one track) as {"clips": [...]}, ordered by track
    then start time. Each entry has track_index, track name, arrangement_clip_index (counted per
    track), start/end in beats, type, mute state and, for audio, file path. Use this before
    editing arrangement clips such as vocals."""
    return {"clips": _send("get_arrangement_clips", track_index=track_index)}


@mcp.tool()
def create_clip(track_index: int, slot_index: int, length: float = 4.0, name: str | None = None) -> dict:
    """Create an empty MIDI clip in a session slot. length is in beats (16 = 4 bars in 4/4)."""
    return _send("create_clip", track_index=track_index, slot_index=slot_index, length=length, name=name)


@mcp.tool()
def create_audio_clip(track_index: int, slot_index: int, file_path: str) -> dict:
    """Create an audio clip from a file on disk in a session slot of an audio track."""
    return _send("create_audio_clip", track_index=track_index, slot_index=slot_index, file_path=file_path)


@mcp.tool()
def delete_clip(track_index: int, slot_index: int) -> dict:
    """Delete the clip in a session slot."""
    return _send("delete_clip", track_index=track_index, slot_index=slot_index)


@mcp.tool()
def duplicate_clip(track_index: int, slot_index: int, target_track_index: int | None = None,
                   target_slot_index: int | None = None) -> dict:
    """Copy a clip. Without a target, it duplicates into the next free slot below (like Ctrl+D).
    target_track_index defaults to the same track."""
    if target_slot_index is None:
        return _send("duplicate_clip_slot", track_index=track_index, slot_index=slot_index)
    if target_track_index is None:
        target_track_index = track_index
    return _send("copy_clip", track_index=track_index, slot_index=slot_index,
                 target_track_index=target_track_index, target_slot_index=target_slot_index)


@mcp.tool()
def set_clip_properties(track_index: int, slot_index: int | None = None,
                        arrangement_clip_index: int | None = None, name: str | None = None,
                        color_index: int | None = None, muted: bool | None = None,
                        looping: bool | None = None, loop_start: float | None = None,
                        loop_end: float | None = None, start_marker: float | None = None,
                        end_marker: float | None = None) -> dict:
    """Rename, recolor or mute a clip and set its loop region and markers (in beats).
    Pass slot_index for a Session clip or arrangement_clip_index for an Arrangement clip."""
    ref = _ref(track_index, slot_index, arrangement_clip_index)
    result: dict = {}
    if name is not None:
        result.update(_send("set_clip_name", name=name, **ref))
    if color_index is not None:
        result.update(_send("set_clip_color", color_index=color_index, **ref))
    if muted is not None:
        result.update(_send("set_clip_muted", muted=muted, **ref))
    if any(v is not None for v in (looping, loop_start, loop_end, start_marker, end_marker)):
        result.update(_send("set_clip_loop", looping=looping, loop_start=loop_start, loop_end=loop_end,
                            start_marker=start_marker, end_marker=end_marker, **ref))
    return result


@mcp.tool()
def set_audio_clip_properties(track_index: int, slot_index: int | None = None,
                              arrangement_clip_index: int | None = None, gain: float | None = None,
                              transpose_semitones: int | None = None, detune_cents: float | None = None,
                              warping: bool | None = None,
                              warp_mode: Literal["beats", "tones", "texture", "repitch", "complex",
                                                 "complex_pro"] | None = None,
                              source_bpm: float | None = None) -> dict:
    """Adjust an audio clip (e.g. a vocal): gain (0..1 slider position, ~0.4 = 0 dB), pitch,
    warping and warp mode (complex_pro suits vocals). Pass slot_index for a Session clip or
    arrangement_clip_index for an Arrangement clip. source_bpm is the tempo the file was
    recorded at (e.g. an acapella's stated BPM): it warps the whole file at that tempo so it
    follows the song tempo. Use it instead of trusting Live's auto-warp on vocals."""
    return _send("set_audio_clip_properties", gain=gain, pitch_coarse=transpose_semitones,
                 pitch_fine=detune_cents, warping=warping,
                 warp_mode=WARP_MODES[warp_mode] if warp_mode else None,
                 source_bpm=source_bpm, **_ref(track_index, slot_index, arrangement_clip_index))


WARP_MODES = {"beats": 0, "tones": 1, "texture": 2, "repitch": 3, "complex": 4, "complex_pro": 6}
RHYTHM_TRACK_WORDS = ("drum", "perc", "kick", "snare", "hat", "beat")


@mcp.tool()
def warp_to_grid(track_indices: list[int] | None = None, rhythm_track_indices: list[int] | None = None,
                 beats_per_marker: int = 4, apply: bool = False) -> dict:
    """Line up audio whose tempo drifts (AI-generated songs such as Suno stems, live takes) with
    Live's grid: track every beat, find the downbeat, and warp each Arrangement clip with a
    marker every beats_per_marker beats (default 4: every bar), so the music's bars land on the
    grid's bars at the song tempo. For one song's stems, which start together; the clips should
    start on a bar line. Beats are tracked on rhythm_track_indices (default: tracks named like
    drums or percussion, else all) and the downbeat is found from all of them.
    apply=False reports the tempo curve and downbeat; apply=True writes the markers (drum and
    percussion tracks get Beats mode, the rest Complex Pro). Undo restores the clips."""
    import soundfile as sf

    clips = [c for c in _send("get_audio_clip_files", include_muted=True)
             if "arrangement_clip_index" in c and (track_indices is None or c["track_index"] in track_indices)]
    if not clips:
        return {"error": "no audio clips in the Arrangement on those tracks"}
    lengths = {c["file_path"]: sf.info(c["file_path"]).duration for c in clips}
    if max(lengths.values()) - min(lengths.values()) > 0.05:
        return {"error": "the files differ in length, so they aren't stems of one song that start together",
                "files": {p: round(d, 2) for p, d in lengths.items()}}

    def is_rhythm(c):
        if rhythm_track_indices is not None:
            return c["track_index"] in rhythm_track_indices
        return any(w in c["track"].lower() for w in RHYTHM_TRACK_WORDS)

    paths = sorted(lengths)
    rhythm = sorted({c["file_path"] for c in clips if is_rhythm(c)})
    grid = _analysis().beats.beat_grid(paths, rhythm or None, _song_tempo())
    beats, first = grid["beats"], grid["first_downbeat"]
    picks = list(range(first, len(beats), max(1, int(beats_per_marker))))
    if picks[-1] != len(beats) - 1:
        picks.append(len(beats) - 1)
    markers = [{"beat_time": float(i - first), "sample_time": beats[i]} for i in picks]
    tail = grid["duration_sec"] - beats[-1]
    if tail > 0.01:  # the audio after the last beat, at the last beat's tempo
        markers.append({"beat_time": len(beats) - 1 - first + tail / (beats[-1] - beats[-2]),
                        "sample_time": grid["duration_sec"]})
    result: dict = {
        "applied": apply, "song_tempo": _song_tempo(), "tempo_bpm": grid["tempo_bpm"],
        "steady": grid["steady"], "onset_jitter_ms": grid["onset_jitter_ms"],
        "beats_tracked_on": [c["track"] for c in clips if c["file_path"] in rhythm] or "all tracks",
        "bar_1_at_sec": round(beats[first], 3), "downbeat_confidence": grid["downbeat_confidence"],
        "markers_per_clip": len(markers),
        "bars": grid["bars"][::8],
    }
    if beats[first] > 0.05:
        result["note"] = (f"The first {beats[first]:.2f} s of each file comes before bar 1, so it sits "
                          "before the clip start; drag the clip start left in Live if it holds a pickup.")
    if grid["downbeat_confidence"] < 0.1:
        result["downbeat_warning"] = ("The downbeat is uncertain: check that bar 1 starts on the first beat "
                                      "of a bar, and move the clips by a beat if not.")
    if apply:
        done = []
        for c in clips:
            mode = WARP_MODES["beats"] if is_rhythm(c) else WARP_MODES["complex_pro"]
            r = _send("set_warp_markers", track_index=c["track_index"],
                      arrangement_clip_index=c["arrangement_clip_index"], markers=markers, warp_mode=mode)
            done.append({"track": c["track"], "clip": c["name"], "markers": r["markers"],
                         "matches": r["matches"],
                         "warp_mode": next(k for k, v in WARP_MODES.items() if v == r["warp_mode"])})
        result["clips"] = done
    return result


@mcp.tool()
def launch(target: Literal["clip", "scene", "stop_clip", "stop_track"], track_index: int | None = None,
           slot_index: int | None = None, scene_index: int | None = None) -> dict:
    """Launch a clip or scene, or stop a clip slot or a whole track's clips."""
    if target == "clip":
        return _send("fire_clip", track_index=track_index, slot_index=slot_index)
    if target == "stop_clip":
        return _send("stop_clip", track_index=track_index, slot_index=slot_index)
    if target == "stop_track":
        return _send("stop_track_clips", track_index=track_index)
    return _send("fire_scene", scene_index=scene_index)


@mcp.tool()
def duplicate_clip_loop(track_index: int, slot_index: int | None = None,
                        arrangement_clip_index: int | None = None) -> dict:
    """Double a MIDI clip's loop, copying its notes into the new half."""
    return _send("duplicate_clip_loop", **_ref(track_index, slot_index, arrangement_clip_index))


# ------------------------------------------------------------------ notes
# Every note tool takes slot_index (Session clip) or arrangement_clip_index (Arrangement clip).
# Note times are relative to the clip's start, not the song.

@mcp.tool()
def get_notes(track_index: int, slot_index: int | None = None,
              arrangement_clip_index: int | None = None) -> dict:
    """Read all MIDI notes in a clip: pitch, start, duration, velocity, mute, probability.
    Pass slot_index for a Session clip or arrangement_clip_index for an Arrangement clip."""
    return _send("get_notes", **_ref(track_index, slot_index, arrangement_clip_index))


@mcp.tool()
def add_notes(track_index: int, notes: list[dict], slot_index: int | None = None,
              arrangement_clip_index: int | None = None, replace: bool = False) -> dict:
    """Write MIDI notes into a clip. Each note: {"pitch": 60, "start": 0.0, "duration": 0.5,
    "velocity": 100, "mute": false, "probability": 1.0}. Times are in beats from the clip start.
    replace=True clears the clip first. Pass slot_index for a Session clip or
    arrangement_clip_index for an Arrangement clip."""
    return _send("add_notes", notes=notes, replace=replace,
                 **_ref(track_index, slot_index, arrangement_clip_index))


@mcp.tool()
def remove_notes(track_index: int, slot_index: int | None = None, arrangement_clip_index: int | None = None,
                 from_pitch: int = 0, pitch_span: int = 128, from_time: float = 0.0,
                 time_span: float | None = None) -> dict:
    """Delete notes in a pitch/time range (defaults to every note in the clip)."""
    return _send("remove_notes", from_pitch=from_pitch, pitch_span=pitch_span, from_time=from_time,
                 time_span=time_span, **_ref(track_index, slot_index, arrangement_clip_index))


@mcp.tool()
def edit_notes(track_index: int, operation: Literal["quantize", "humanize", "transpose", "velocity"],
               slot_index: int | None = None, arrangement_clip_index: int | None = None,
               pitch: int | None = None, grid: float = 0.25, amount: float = 1.0,
               quantize_ends: bool = False, timing: float = 0.01, velocity_range: float = 8,
               semitones: int = 0, velocity_factor: float = 1.0, velocity_offset: float = 0.0,
               seed: int | None = None) -> dict:
    """Process existing notes; pitch limits the edit to one note number (e.g. only hats).
    - quantize: snap to grid (beats: 0.25 = 1/16, 0.5 = 1/8, 1/3 = 1/8 triplet) by amount 0..1.
    - humanize: random timing shift up to `timing` beats, velocity by up to `velocity_range`.
    - transpose: shift by `semitones`.
    - velocity: new = old * velocity_factor + velocity_offset.
    Pass slot_index for a Session clip or arrangement_clip_index for an Arrangement clip."""
    base = {"pitch": pitch, **_ref(track_index, slot_index, arrangement_clip_index)}
    if operation == "quantize":
        return _send("quantize_notes", grid=grid, amount=amount, quantize_ends=quantize_ends, **base)
    if operation == "humanize":
        return _send("humanize_notes", timing=timing, velocity=velocity_range, seed=seed, **base)
    if operation == "transpose":
        return _send("transpose_notes", semitones=semitones, **base)
    return _send("scale_velocity", factor=velocity_factor, offset=velocity_offset, **base)


# -------------------------------------------------------------- generators

def _write_generated(track_index: int, slot_index: int, notes: list[dict], length: float, name: str) -> dict:
    info = _send("get_track_info", track_index=track_index)
    slot = info["clip_slots"][slot_index]
    if not slot["has_clip"]:
        _send("create_clip", track_index=track_index, slot_index=slot_index, length=length, name=name)
    elif slot["clip"]["length"] < length:
        _send("set_clip_loop", track_index=track_index, slot_index=slot_index, loop_end=length,
              end_marker=length)
    result = _send("add_notes", track_index=track_index, slot_index=slot_index, notes=notes, replace=True)
    result["notes"] = len(notes)
    return result


def _resolve_key(root: str | None, scale: str | None) -> tuple[str, str]:
    """Fill in a missing root/scale from the Live set's key (Live 12), else A minor."""
    if root and scale:
        return root, scale
    key = _send("get_session_info", include_clips=False).get("key") or {}
    song_scale = music.LIVE_SCALES.get(key.get("scale", ""), "minor")
    return root or key.get("root", "A"), scale or song_scale


@mcp.tool()
def generate_drum_pattern(track_index: int, slot_index: int,
                          style: Literal[tuple(sorted(music.DRUM_STYLES))] = "house",  # type: ignore[valid-type]
                          bars: int = 2, swing: float = 0.0, fill: bool = False, variation: float = 0.3,
                          seed: int | None = None) -> dict:
    """Write a drum pattern into a clip (creating it if needed) on a track with a Drum Rack.
    Uses standard pads: kick 36, rim 37, snare 38, clap 39, closed hat 42, open hat 46, shaker 70.
    swing 0..1 (tech house sits around 0.2-0.4). variation 0..1 changes hats and percussion
    between bars. fill adds a roll at the end of the last bar. Replaces existing notes."""
    notes = music.drum_pattern(style, bars, swing, fill, variation, seed)
    return _write_generated(track_index, slot_index, notes, bars * 4.0, f"{style} drums")


@mcp.tool()
def generate_bassline(track_index: int, slot_index: int, root: str | None = None, scale: str | None = None,
                      bars: int = 2, octave: int = 1,
                      style: Literal["offbeat", "rolling", "root_eighths", "walking", "tech_house",
                                     "syncopated"] = "offbeat",
                      progression: list[int] | None = None, seed: int | None = None) -> dict:
    """Write a bassline into a clip. root/scale default to the Live set's key. progression lists
    1-based scale degrees, one per bar, e.g. [1, 6, 4, 5]. octave 1 means C1 = 36.
    Replaces existing notes."""
    root, scale = _resolve_key(root, scale)
    notes = music.bassline(root, scale, bars, octave, style, progression, seed)
    return _write_generated(track_index, slot_index, notes, bars * 4.0, f"{root} {scale} bass")


@mcp.tool()
def generate_chords(track_index: int, slot_index: int, root: str | None = None, scale: str | None = None,
                    degrees: list[int] | None = None, beats_per_chord: float = 4.0, octave: int = 3,
                    sevenths: bool = False, velocity: int = 90,
                    rhythm: Literal["sustained", "stabs", "offbeat_stabs", "pulse"] = "sustained") -> dict:
    """Write a diatonic chord progression into a clip. root/scale default to the Live set's key.
    degrees are 1-based scale degrees, e.g. [1, 6, 3, 7]. rhythm: sustained pads, offbeat stabs,
    syncopated offbeat_stabs (tech house), or 8th-note pulse. Replaces existing notes."""
    root, scale = _resolve_key(root, scale)
    degrees = degrees or [1, 6, 3, 7]
    notes = music.chord_progression(root, scale, degrees, beats_per_chord, octave, sevenths, velocity, rhythm)
    return _write_generated(track_index, slot_index, notes, len(degrees) * beats_per_chord,
                            f"{root} {scale} chords")


@mcp.tool()
def scale_notes(root: str, scale: str = "minor", octave: int = 3, octaves: int = 1) -> dict:
    """Return MIDI note numbers for a scale (C3 = 60). Scales: major, minor, harmonic_minor,
    dorian, phrygian, lydian, mixolydian, pentatonic_major, pentatonic_minor, blues."""
    return {"pitches": music.scale_pitches(root, scale, octave, octaves)}


# ----------------------------------------------------------------- scenes

@mcp.tool()
def manage_scene(action: Literal["create", "delete", "duplicate", "rename", "fire"],
                 scene_index: int = -1, name: str | None = None) -> dict:
    """Create (scene_index -1 appends), delete, duplicate, rename or fire a scene."""
    if action == "create":
        return _send("create_scene", index=scene_index, name=name)
    if action == "rename":
        return _send("set_scene_name", scene_index=scene_index, name=name)
    return _send(f"{action}_scene", scene_index=scene_index)


# ---------------------------------------------------------------- devices

@mcp.tool()
def get_devices(track_index: int, kind: TrackKind = "track") -> dict:
    """List the devices on a track, including rack chains and loaded Drum Rack pads."""
    return {"devices": _send("get_devices", track_index=track_index, kind=kind)}


@mcp.tool()
def get_device_parameters(track_index: int, device_index: int, kind: TrackKind = "track") -> dict:
    """List a device's parameters with their index, current value, range and displayed value."""
    return _send("get_device_parameters", track_index=track_index, device_index=device_index, kind=kind)


@mcp.tool()
def set_device_parameter(track_index: int, device_index: int, parameter: int | str,
                         value: float | None = None, display_value: str | None = None,
                         kind: TrackKind = "track") -> dict:
    """Set a device parameter by index or name. Prefer display_value, what the control should
    show: a number with its unit for continuous controls ('350 Hz', '2.5 kHz', '-18 dB',
    '150 ms', '1.5 s', '30 %', '25L', 'C'), or a menu entry for switches and menus ('On').
    The result's display shows what Live set. value is the raw value (min/max from
    get_device_parameters), and is often not linear in Hz, dB or ms."""
    return _send("set_device_parameter", track_index=track_index, device_index=device_index,
                 parameter=parameter, value=value, display_value=display_value, kind=kind)


@mcp.tool()
def move_device(track_index: int, device_index: int, position: int, kind: TrackKind = "track",
                target_track_index: int | None = None, target_kind: TrackKind | None = None) -> dict:
    """Move a device to another position in its chain (0 = first), e.g. an EQ before a
    compressor, or to another track's chain with target_track_index/target_kind."""
    return _send("move_device", track_index=track_index, device_index=device_index, position=position,
                 kind=kind, target_track_index=target_track_index, target_kind=target_kind)


@mcp.tool()
def set_device_on(track_index: int, device_index: int, on: bool, kind: TrackKind = "track") -> dict:
    """Turn a device on or off (bypass)."""
    return _send("set_device_active", track_index=track_index, device_index=device_index, active=on, kind=kind)


@mcp.tool()
def delete_device(track_index: int, device_index: int, kind: TrackKind = "track") -> dict:
    """Remove a device from a track."""
    return _send("delete_device", track_index=track_index, device_index=device_index, kind=kind)


@mcp.tool()
def set_sidechain(track_index: int, device_index: int, source: str, enabled: bool = True) -> dict:
    """Point a Compressor/Gate's sidechain at another track (by its name as shown in Live),
    e.g. side-chain the bass compressor to the kick."""
    return _send("set_compressor_sidechain", track_index=track_index, device_index=device_index,
                 source=source, enabled=enabled)


# ---------------------------------------------------------------- browser

@mcp.tool()
def search_browser(query: str, category: Literal["all", "instruments", "audio_effects", "midi_effects",
                                                 "drums", "sounds", "samples", "clips", "plugins",
                                                 "max_for_live", "user_library", "packs"] = "all",
                   max_results: int = 25) -> dict:
    """Search Live's browser for loadable items by name (all words must match). Narrowing the
    category is much faster. Returns uris for load_browser_item."""
    return _send("search_browser", query=query, category=category, max_results=max_results)


@mcp.tool()
def browse(category: Literal["instruments", "audio_effects", "midi_effects", "drums", "sounds", "samples",
                             "clips", "plugins", "max_for_live", "user_library", "packs",
                             "current_project"],
           path: str = "") -> dict:
    """List one folder of Live's browser. path is '/'-separated folder names, e.g. 'Drum Rack'
    under drums, or '' for the top level."""
    return {"items": _send("browse", category=category, path=path)}


@mcp.tool()
def load_browser_item(uri: str, track_index: int | None = None, kind: TrackKind = "track",
                      position: int | None = None) -> dict:
    """Load a browser item (instrument, effect, preset, drum kit, sample) onto a track.
    Get the uri from search_browser or browse. Effects go at the end of the chain unless you
    pass position (0 = first device), e.g. to put a Utility before a limiter."""
    return _send("load_browser_item", uri=uri, track_index=track_index, kind=kind, position=position)


_LOAD_ORDER = {
    "instrument": ["plugins", "instruments", "sounds", "drums", "user_library"],
    "audio_effect": ["audio_effects", "plugins", "user_library"],
    "midi_effect": ["midi_effects", "plugins", "user_library"],
    "any": ["all"],
}


@mcp.tool()
def load_device_by_name(name: str, track_index: int,
                        type: Literal["instrument", "audio_effect", "midi_effect", "any"] = "any",
                        kind: TrackKind = "track", position: int | None = None) -> dict:
    """Find a device, plugin or preset by name and load the best match onto a track. Works for
    Live devices ('Operator', 'EQ Eight', 'Compressor'), presets and kits ('808 Core Kit'), and
    third-party plugins ('Serum 2', 'Diva', 'Pro-Q 3'). When the user names a plugin, always load
    it with this tool rather than substituting a Live instrument. If nothing matches, report that
    instead of loading something else. Effects go at the end of the chain unless you pass
    position (0 = first device)."""
    return _load_by_name(name, track_index, type, kind, position)


def _load_by_name(name: str, track_index: int, type: str = "any", kind: str = "track",
                  position: int | None = None) -> dict:
    found: list[dict] = []
    for category in _LOAD_ORDER[type]:
        found = _send("search_browser", query=name, category=category, max_results=10)["results"]
        if found and found[0]["rank"] == 0:
            break
    if not found:
        return {"error": f"nothing named {name!r} found in Live's browser. If it's a plugin, check it "
                         "is installed and enabled under Settings > Plug-Ins and rescan."}
    pick = found[0]
    result = _send("load_browser_item", uri=pick["uri"], track_index=track_index, kind=kind,
                   position=position)
    result["matched"] = pick["name"]
    if pick["rank"] != 0:
        result["note"] = f"no exact match for {name!r}; loaded closest: {pick['name']}"
    return result


@mcp.tool()
def plugin_presets(track_index: int, device_index: int, select_index: int | None = None,
                   select_name: str | None = None, kind: TrackKind = "track") -> dict:
    """List or switch the presets (programs) a third-party plugin reports to Live. Many modern
    plugins, Serum 2 included, manage presets in their own browser and report none here. In that
    case, suggest the user saves favourite sounds as Live presets (the save icon in the device
    title bar), which makes them loadable by name with load_device_by_name."""
    if select_index is None and select_name is None:
        return _send("get_plugin_presets", track_index=track_index, device_index=device_index, kind=kind)
    return _send("set_plugin_preset", track_index=track_index, device_index=device_index,
                 index=select_index, name=select_name, kind=kind)


# ------------------------------------------------------------ arrangement

@mcp.tool()
def manage_locator(action: Literal["list", "add", "rename", "delete", "jump"], time: float | None = None,
                   name: str | None = None, index: int | None = None) -> dict:
    """Arrangement locators, indexed in time order: list them, add one at `time` (beats) with an
    optional name (e.g. 'Intro', 'Drop'; adding where one exists renames it), rename or delete one
    by index, or jump the playhead to one. Adding and deleting need playback stopped, because
    Live places locators at the playhead; the playhead is put back afterwards."""
    if action == "list":
        return {"locators": _send("get_locators")}
    if action == "add":
        return _send("add_locator", time=time, name=name)
    if action == "rename":
        return _send("rename_locator", index=index, name=name)
    if action == "delete":
        return _send("delete_locator", index=index)
    return _send("jump_to_locator", index=index)


@mcp.tool()
def place_clip_in_arrangement(track_index: int, time: float, slot_index: int | None = None,
                              arrangement_clip_index: int | None = None, repeats: int = 1) -> dict:
    """Copy a clip into the arrangement at `time` (beats), back to back `repeats` times. The
    source is a Session clip (slot_index) or an Arrangement clip on the same track
    (arrangement_clip_index), e.g. to repeat a section or extend a bounced track. Use this to
    build sections: e.g. bars 1-16 intro = time 0 to 64."""
    ref = _ref(track_index, slot_index, arrangement_clip_index)
    info = _send("get_clip_info", **ref)
    if arrangement_clip_index is not None:
        length = info["end_time"] - info["start_time"]
    else:
        length = info["loop_end"] - info["loop_start"] if info["looping"] else info["length"]
    for i in range(repeats):
        _send("duplicate_clip_to_arrangement", time=time + i * length, **ref)
        if arrangement_clip_index is not None and time + i * length < info["start_time"]:
            ref["arrangement_clip_index"] += 1  # the copy landed before the source
    return {"placed": repeats, "start": time, "end": time + repeats * length}


@mcp.tool()
def create_arrangement_clip(track_index: int, start: float, length: float) -> dict:
    """Create an empty MIDI clip directly in the arrangement (Live 12.1+)."""
    return _send("create_arrangement_midi_clip", track_index=track_index, start=start, length=length)


@mcp.tool()
def delete_arrangement_clip(track_index: int, arrangement_clip_index: int) -> dict:
    """Delete an arrangement clip by its arrangement_clip_index (see get_arrangement_clips)."""
    return _send("delete_arrangement_clip", track_index=track_index,
                 arrangement_clip_index=arrangement_clip_index)


@mcp.tool()
def record_automation(lanes: list[dict], pre_roll_beats: float | None = None) -> dict:
    """Write Arrangement automation by recording it: Live plays the span with automation
    recording on while each parameter follows its points, moving in straight lines between
    them. Live's API can't draw breakpoints directly, so this takes as long as the music does
    (16 bars at 124 BPM is about 31 s) and its timing is about 0.1 s, so it suits sweeps,
    builds, fades and rides, not fast gating.

    lanes: one entry per parameter, all recorded in one pass:
      {"track_index": 2, "device_index": 1, "parameter": "Frequency",
       "points": [{"beat": 64, "display": "200 Hz"}, {"beat": 128, "display": "8 kHz"}]}
    Omit device_index for the mixer: parameter "volume", "pan" or "send A". kind is "track"
    (default), "return" or "master". Each point has beat and either display (with units) or a
    raw value. Before a lane's first point the parameter is untouched.

    Playback must be stopped, with no Session clips playing. Armed tracks are disarmed for the
    pass and re-armed after. The playhead starts one bar before the first point (pre_roll_beats
    changes that) and returns to where it was. Undo removes the recording."""
    started = _send("record_automation", lanes=lanes, pre_roll_beats=pre_roll_beats)
    deadline = time.monotonic() + started["seconds"] * 2 + 30
    status = started
    while time.monotonic() < deadline:
        time.sleep(0.5)
        status = _send("job_status", job=started["job"])
        if status["state"] in ("done", "error", "cancelled"):
            break
    else:
        status = _send("cancel_job", job=started["job"])
        status["error"] = "recording didn't finish in time and was cancelled"
    if status["state"] == "done":
        status["next"] = ("Check the lanes in Live (show automation with A in the Arrangement). The "
                          "recorded line has small steps at the 0.1 s update rate.")
    return status


@mcp.tool()
def back_to_arrangement() -> dict:
    """Press 'Back to Arrangement' so the arrangement plays instead of session clips."""
    return _send("back_to_arrangement")


# --------------------------------------------------------------- analyser
# Imported lazily so the rest of the server still works if numpy/scipy are missing.

def _analysis():
    from . import analysis
    return analysis


def _song_tempo() -> float | None:
    try:
        return _send("get_session_info", include_clips=False)["tempo"]
    except Exception:
        return None


def _song_key() -> tuple[int, str] | None:
    try:
        key = _send("get_session_info", include_clips=False).get("key") or {}
    except Exception:
        return None
    mode = {"Major": "major", "Minor": "minor"}.get(key.get("scale", ""))
    return (key["root_note"], mode) if mode and key.get("root_note") is not None else None


def _beats_to_seconds(beat: float, markers: list[dict]) -> float:
    """Map a clip beat position to a file position via warp markers, extrapolating past the ends."""
    pts = sorted((m["beat_time"], m["sample_time"]) for m in markers)
    if beat <= pts[0][0]:
        seg = pts[:2]
    elif beat >= pts[-1][0]:
        seg = pts[-2:]
    else:
        seg = next((a, b) for a, b in zip(pts, pts[1:]) if a[0] <= beat <= b[0])
    (b0, s0), (b1, s1) = seg
    return s0 + (beat - b0) * (s1 - s0) / (b1 - b0) if b1 != b0 else s0


@mcp.tool()
def analyze_audio_file(path: str, start_sec: float | None = None, end_sec: float | None = None,
                       tempo_bpm: float | None = None) -> dict:
    """Measure an audio file (wav, aif, flac, mp3, ogg) so you can reason about how it sounds:
    loudness (integrated LUFS, true peak, loudness range, crest), frequency balance by band and
    octave, brightness, stereo width and low-end mono, tempo and rhythmic density, key, and
    energy over time, plus plain-language observations. Optionally only a region (seconds).
    tempo_bpm (e.g. the song tempo) is used for onsets-per-beat. Use this for exported mixdowns,
    stems, reference tracks and samples. MIDI/synth tracks have no file: bounce them first."""
    a = _analysis()
    return a.public(a.analyze(path, start_sec, end_sec, tempo_bpm))


@mcp.tool()
def analyze_clip(track_index: int, slot_index: int | None = None,
                 arrangement_clip_index: int | None = None) -> dict:
    """Analyse an audio clip in the set (Session slot or Arrangement clip) from its source file,
    limited to the audio the clip actually plays when it's warped: for an Arrangement clip, from
    its start marker for its length in the arrangement; for a looped clip, its loop region.
    Unwarped clips are analysed whole. Uses the song tempo for rhythm figures. Clip gain, effects
    and faders aren't applied: this measures the source audio."""
    info = _send("get_clip_info", **_ref(track_index, slot_index, arrangement_clip_index))
    if info.get("is_midi"):
        return {"error": "MIDI clip: there is no audio to analyse. Bounce or freeze and flatten the track, "
                         "or export it, then use analyze_audio_file."}
    start, end, region = _clip_region(info)
    a = _analysis()
    result = a.public(a.analyze(info["file_path"], start, end, _song_tempo()))
    result["clip"] = {"name": info["name"], "region": region}
    return result


def _clip_region(info: dict) -> tuple[float | None, float | None, dict]:
    """The part of the clip's file that it plays, in file seconds (None = file start/end)."""
    markers = info.get("warp_markers") or []
    if not (info.get("warping") and len(markers) >= 2):
        return None, None, {"description": "whole file (clip is unwarped, so its markers can't be "
                                           "mapped to the file)"}
    b0, b1, description = _played_beats(info)
    start = max(0.0, _beats_to_seconds(b0, markers))
    end = _beats_to_seconds(b1, markers)
    return start, end, {"description": description, "clip_beats": [round(b0, 3), round(b1, 3)],
                        "file_seconds": [round(start, 3), round(end, 3)]}


def _played_beats(info: dict) -> tuple[float, float, str]:
    """The span of clip content (in clip beats) that the clip plays.

    Trimming an Arrangement clip changes its length in the arrangement but not its end marker, so
    for Arrangement clips the played span is start marker + arrangement length, not the end marker.
    An unlooped clip plays from loop_start to loop_end (Live's API gives the clip's start and end
    there); Live ignores a start marker past loop_start."""
    looping = info.get("looping")
    loop = (info["loop_start"], info["loop_end"])
    if info.get("start_time") is not None:  # Arrangement clip
        length = info["end_time"] - info["start_time"]
        if looping:
            if length >= loop[1] - loop[0]:
                return loop[0], loop[1], "loop region (repeats in the arrangement)"
            begin = info["start_marker"]
            return begin, min(loop[1], begin + length), "part of the loop played in the arrangement"
        return loop[0], min(loop[1], loop[0] + length), "region played in the arrangement"
    if looping:
        return loop[0], loop[1], "loop region"
    return loop[0], loop[1], "clip start to end"


@mcp.tool()
def compare_to_reference(path: str, reference_path: str, start_sec: float | None = None,
                         end_sec: float | None = None, reference_start_sec: float | None = None,
                         reference_end_sec: float | None = None) -> dict:
    """Compare a mix (or stem) with a reference track: tonal balance per octave after loudness
    matching, loudness, dynamics, stereo width, low-end mono and key, with concrete suggestions.
    Compare like with like, e.g. a drop against the reference's drop, using the region arguments."""
    a = _analysis()
    mix = a.analyze(path, start_sec, end_sec)
    ref = a.analyze(reference_path, reference_start_sec, reference_end_sec)
    return a.public(a.compare.compare_to_reference(mix, ref))


def _context_files(context_paths: list[str] | None) -> list[tuple[str, str]]:
    if context_paths:
        return [(p, os.path.basename(p)) for p in context_paths]
    seen, out = set(), []
    for c in _send("get_audio_clip_files"):
        if c["file_path"] and c["file_path"] not in seen:
            seen.add(c["file_path"])
            out.append((c["file_path"], f"{c['track']}: {c['name']}"))
    return out


def _merge_ranges(ranges: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _unmeasured_tracks() -> list[dict]:
    """MIDI tracks that actually make sound but have no audio file to measure: unmuted, with
    active (not deactivated) Arrangement clips or a playing Session clip. Each entry gives the
    beat ranges that are missing from the map."""
    tracks = _send("get_session_info", include_clips=True)["tracks"]
    ranges: dict[int, list[list[float]]] = {}
    for c in _send("get_arrangement_clips"):
        if c["type"] == "midi" and not c["muted"]:
            ranges.setdefault(c["track_index"], []).append([c["start"], c["end"]])
    out = []
    for i, t in enumerate(tracks):
        if t["type"] != "midi" or t["mute"]:
            continue
        entry: dict = {"track": t["name"]}
        if i in ranges:
            entry["beats"] = _merge_ranges(ranges[i])
        if any(c.get("playing") for c in t.get("clips", [])):
            entry["session_clip_playing"] = True
        if len(entry) > 1:
            out.append(entry)
    return out


def _space(context_paths: list[str] | None) -> tuple[dict, list[str]]:
    a = _analysis()
    analysed, skipped = [], []
    for path, label in _context_files(context_paths):
        try:
            r = dict(a.analyze(path, quick=True))
        except Exception as e:
            skipped.append(f"{label}: {e}")
            continue
        r["label"] = label
        analysed.append(r)
    space = a.compare.frequency_space(analysed)
    if context_paths is None:
        missing = _unmeasured_tracks()
        if missing:
            space["not_measured"] = missing
            names = ", ".join(m["track"] for m in missing)
            space["warning"] = (f"{len(missing)} MIDI/instrument track(s) with active clips aren't in this map "
                                f"({names}; see not_measured for the beat ranges). "
                                "Bands they fill (e.g. a synth bass) may show as open when they aren't. "
                                "For an accurate map, export or bounce those tracks and pass the files "
                                "as context_paths together with the other stems.")
    return space, skipped


@mcp.tool()
def analyze_frequency_space(context_paths: list[str] | None = None) -> dict:
    """Show which frequency bands the set's audio already fills, which are open, and which
    parts crowd each band. By default uses every unmuted audio clip in the set; or pass files
    (e.g. exported stems). Each file counts equally, so this shows occupancy, not final levels.
    MIDI/synth tracks aren't included unless you pass their bounced audio."""
    space, skipped = _space(context_paths)
    result = _analysis().public(space)
    if skipped:
        result["skipped"] = skipped
    return result


@mcp.tool()
def score_samples(candidate_paths: list[str] | None = None, folder: str | None = None,
                  context_paths: list[str] | None = None, max_files: int = 100, top: int = 15) -> dict:
    """Rank samples by how well they'd fit the current set: high scores put their energy in bands
    the set leaves open (not clashing with existing parts) and are in a compatible key (Camelot
    wheel against the song key). Pass candidate files and/or a folder to scan (recursive).
    Context defaults to the set's unmuted audio clips; pass context_paths to use stems instead."""
    a = _analysis()
    candidates = list(candidate_paths or [])
    if folder:
        candidates += a.find_audio_files(folder, limit=max_files)
    candidates = candidates[:max_files]
    if not candidates:
        return {"error": "pass candidate_paths or a folder containing audio files"}
    space, skipped = _space(context_paths)
    song_key = _song_key()
    ranked = []
    for path in candidates:
        try:
            # The first 30 s are plenty to characterise a sample or loop.
            r = a.analyze(path, end=30.0 if a.features.sf.info(path).duration > 30 else None, quick=True)
        except Exception as e:
            skipped.append(f"{os.path.basename(path)}: {e}")
            continue
        if r.get("silent"):
            continue
        ranked.append(a.compare.score_sample(r, space, song_key))
    ranked.sort(key=lambda s: -(s["score"] or 0))
    result = {
        "song_key": a.compare.key_name(*song_key) if song_key else None,
        "context_files": space.get("files", 0),
        "open_bands": space.get("open_bands", []),
        "analysed": len(ranked),
        **({"warning": space["warning"]} if "warning" in space else {}),
        "ranked": ranked[:top],
    }
    if space.get("files", 0) == 0:
        result["note"] = ("No audio context found (MIDI-only set?), so scores reflect key fit only. "
                          "Pass bounced stems as context_paths for frequency-fit scoring.")
    if skipped:
        result["skipped"] = skipped
    return result


# ---------------------------------------------------------- mix and master

# Integrated loudness (LUFS) and true-peak ceiling (dBTP) for common destinations.
LOUDNESS_TARGETS = {
    "streaming": (-14.0, -1.0),  # Spotify, YouTube and Tidal normalise to about -14 LUFS
    "apple": (-16.0, -1.0),      # Apple Music normalises to -16
    "club": (-8.0, -0.5),        # loud dance masters for DJ sets; streaming will turn them down
    "broadcast": (-23.0, -1.0),  # EBU R128
}
LoudnessTarget = Literal["streaming", "apple", "club", "broadcast"]


def _db(text: str | None) -> float | None:
    """'-3.5 dB' -> -3.5; '-inf dB' -> -inf; None if the text isn't a dB value."""
    m = re.search(r"(-?inf|[-+]?\d*\.?\d+)\s*dB", text or "", re.IGNORECASE)
    return float(m.group(1)) if m else None


def _try_set(device_index: int, parameter: str, display: str, kind: str = "master",
             track_index: int = 0) -> tuple[bool, str]:
    try:
        r = _send("set_device_parameter", track_index=track_index, device_index=device_index,
                  parameter=parameter, display_value=display, kind=kind)
        return True, f"{r['name']} = {r['display']}"
    except LiveError as e:
        return False, f"{parameter} -> {display}: {e}"


def _devices(kind: str = "master", track_index: int = 0) -> list[dict]:
    return _send("get_devices", track_index=track_index, kind=kind)


def _limiter_param(device_index: int, *names: str) -> dict | None:
    """The master Limiter's first parameter named one of `names` (any case), or None."""
    params = _send("get_device_parameters", track_index=0, device_index=device_index, kind="master")
    by_name = {p["name"].lower(): p for p in params["parameters"]}
    return next((by_name[n] for n in names if n in by_name), None)


@mcp.tool()
def gain_stage(target_lufs: float = -18.0, track_offsets: dict[str, float] | None = None,
               apply: bool = False) -> dict:
    """Set a starting balance: measure each audio track's clips (the parts they play, with clip
    gain) and work out the fader setting that brings each track to target_lufs, so every part
    starts at the same loudness with headroom for the master. track_offsets adjusts single
    tracks by name, in dB (e.g. {"Kick": 2, "Hats": -6}). apply=False only reports; apply=True
    sets the faders. Measures source audio before effects: tracks with devices are flagged."""
    a = _analysis()
    by_track: dict[int, list[dict]] = {}
    for c in _send("get_audio_clip_files"):
        by_track.setdefault(c["track_index"], []).append(c)
    offsets = {k.lower(): v for k, v in (track_offsets or {}).items()}
    tempo = _song_tempo()
    tracks, skipped = [], []
    for ti, clips in sorted(by_track.items()):
        # The Arrangement is what a mixdown plays; Session clips count only if there's nothing there.
        clips = [c for c in clips if "arrangement_clip_index" in c] or clips
        power = seconds = 0.0
        for c in clips:
            info = _send("get_clip_info", **_ref(ti, c.get("slot_index"), c.get("arrangement_clip_index")))
            start, end, _ = _clip_region(info)
            try:
                r = a.analyze(info["file_path"], start, end, tempo, quick=True)
            except Exception as e:
                skipped.append(f"{c['track']}: {c['name']}: {e}")
                continue
            lufs = r.get("loudness", {}).get("integrated_lufs")
            gain = _db(info.get("gain")) or 0.0
            if lufs is None or gain == float("-inf"):
                continue
            power += r["duration_sec"] * 10 ** ((lufs + gain) / 10)
            seconds += r["duration_sec"]
        if not seconds:
            continue
        loud = 10 * math.log10(power / seconds)
        track = _send("get_track_info", track_index=ti)
        target = target_lufs + offsets.get(track["name"].lower(), 0.0)
        fader = max(-70.0, min(6.0, target - loud))
        entry: dict = {"track_index": ti, "track": track["name"], "source_lufs": round(loud, 1),
                       "target_lufs": round(target, 1), "fader_now": track["volume"],
                       "fader_db": round(fader, 1)}
        if target - loud > 6.0:
            entry["note"] = "needs more than the fader's +6 dB: raise the clip gain or add a Utility"
        active = [d["name"] for d in track["devices"] if d["is_active"]]
        if active:
            entry["devices"] = active
        if apply:
            entry["fader_set"] = _send("set_track_volume", track_index=ti, db=round(fader, 1))["volume"]
        tracks.append(entry)
    result: dict = {"target_lufs": target_lufs, "applied": apply, "tracks": tracks}
    if any("devices" in t for t in tracks):
        result["warning"] = ("Tracks listing devices were measured before their effects; EQ, compression "
                             "or saturation change their level. Check those by ear or with an export.")
    missing = _unmeasured_tracks()
    if missing:
        result["not_measured"] = [m["track"] for m in missing]
        result["note"] = ("MIDI/instrument tracks have no audio file to measure; set their faders by "
                          "ear against the staged tracks, or bounce them and run this again.")
    if skipped:
        result["skipped"] = skipped
    return result


@mcp.tool()
def build_master_chain(target: LoudnessTarget = "streaming", glue: bool = True,
                       force: bool = False) -> dict:
    """Put a basic master chain on the master track with Live's devices: Glue Compressor (2:1,
    10 ms attack, auto release) for gentle glue, then Limiter with its ceiling at the target's
    true-peak limit. Reuses a Limiter that's already there. Refuses if the master already has a
    third-party plugin (e.g. Ozone), unless force. Then export the mix and run match_loudness
    to set the limiter's gain."""
    lufs, ceiling = LOUDNESS_TARGETS[target]
    devices = _devices()
    plugins = [d["name"] for d in devices if "Plugin" in d["class_name"]]
    if plugins and not force:
        return {"error": f"the master already has {', '.join(plugins)}. Adding Live's limiter as well "
                         "would limit twice. Use match_loudness, which works with the existing chain, "
                         "or pass force=True to build the Live chain anyway."}
    done, failed, added = [], [], []
    limiter = next((d["index"] for d in devices if d["class_name"] == "Limiter"), None)
    if glue and not any(d["class_name"] == "GlueCompressor" for d in devices):
        _load_by_name("Glue Compressor", 0, "audio_effect", "master", position=limiter)
        added.append("Glue Compressor")
    devices = _devices()
    if limiter is None:
        _load_by_name("Limiter", 0, "audio_effect", "master")
        added.append("Limiter")
        devices = _devices()
    for d in devices:
        if d["class_name"] == "GlueCompressor" and "Glue Compressor" in added:
            for name, value in (("Ratio", "2"), ("Attack", "10"), ("Release", "A")):
                ok, msg = _try_set(d["index"], name, value)
                (done if ok else failed).append(f"Glue Compressor: {msg}")
        if d["class_name"] == "Limiter":
            ok, msg = _try_set(d["index"], "Ceiling", f"{ceiling} dB")
            (done if ok else failed).append(f"Limiter: {msg}")
            # Standard mode limits sample peaks, so true peaks can pass the ceiling (Live 12+ only).
            if _limiter_param(d["index"], "mode"):
                ok, msg = _try_set(d["index"], "Mode", "True Peak")
                (done if ok else failed).append(f"Limiter: {msg}")
    result: dict = {"target": target, "target_lufs": lufs, "true_peak_ceiling": ceiling,
                    "added": added, "set": done, "chain": [d["name"] for d in _devices()],
                    "next": ("Glue's threshold depends on the mix level and Live doesn't report gain "
                             "reduction to scripts: ask the user to pull Threshold down until the meter "
                             "shows 1-2 dB of reduction. Then export the mix and call match_loudness.")}
    if failed:
        result["failed"] = failed
    return result


@mcp.tool()
def match_loudness(mix_path: str, target: LoudnessTarget | None = "streaming",
                   target_lufs: float | None = None, true_peak_ceiling: float | None = None) -> dict:
    """Bring the master to a loudness target from an exported mixdown: measures mix_path (export
    the whole song from the master), then raises or lowers the gain into the master's Limiter by
    the difference and sets its ceiling. With no Live Limiter on the master (e.g. Ozone), it
    uses a Utility at the start of the master chain instead, adding one if needed. Limiting
    harder doesn't raise loudness one-for-one, so export again and repeat until it's within
    0.5 LU. target picks a preset; target_lufs/true_peak_ceiling override it."""
    lufs, ceiling = LOUDNESS_TARGETS[target] if target else (-14.0, -1.0)
    lufs = lufs if target_lufs is None else target_lufs
    ceiling = ceiling if true_peak_ceiling is None else true_peak_ceiling
    loud = _analysis().analyze(mix_path)["loudness"]
    measured, peak = loud.get("integrated_lufs"), loud.get("true_peak_dbtp")
    if measured is None:
        return {"error": f"{mix_path} is silent or too short to measure"}
    delta = round(lufs - measured, 1)
    result: dict = {"measured_lufs": measured, "measured_true_peak": peak, "target_lufs": lufs,
                    "true_peak_ceiling": ceiling, "change_db": delta}
    if abs(delta) <= 0.5 and (peak is None or peak <= ceiling + 0.1):
        result["status"] = "on target; nothing changed"
        return result
    devices = _devices()
    limiter = next((d for d in devices if d["class_name"] == "Limiter"), None)
    if limiter:
        gain = _limiter_param(limiter["index"], "input gain", "gain")  # Live 12 renamed Gain
        if gain is None:
            return {**result, "error": "the Limiter has no Gain parameter exposed"}
        now = gain.get("display_number", _db(gain["display"]) or 0.0)  # display rounds above 10 dB
        ok, msg = _try_set(limiter["index"], gain["name"], f"{now + delta:.2f} dB")
        result["limiter_gain"] = msg
        ok2, msg2 = _try_set(limiter["index"], "Ceiling", f"{ceiling} dB")
        result["limiter_ceiling"] = msg2
        if not (ok and ok2):
            result["error"] = "couldn't set the Limiter; see limiter_gain/limiter_ceiling"
    else:
        utility = devices[0] if devices and devices[0]["class_name"] == "StereoGain" else None
        if utility is None:
            _load_by_name("Utility", 0, "audio_effect", "master", position=0)
            utility = _devices()[0]
            result["added"] = "Utility at the start of the master chain"
        params = _send("get_device_parameters", track_index=0, device_index=utility["index"], kind="master")
        gain = next((p for p in params["parameters"] if p["name"].lower() == "gain"), None)
        now = (gain.get("display_number", _db(gain["display"])) if gain else 0.0) or 0.0
        ok, msg = _try_set(utility["index"], "Gain", f"{now + delta:.2f} dB")
        result["utility_gain"] = msg
        plugins = [d["name"] for d in devices if "Plugin" in d["class_name"]]
        result["note"] = ("Gain goes in before the rest of the master chain"
                          + (f"; set the ceiling in {', '.join(plugins)} to {ceiling} dBTP yourself, "
                             "since it doesn't expose it" if plugins else
                             "; there's no limiter on the master, so peaks aren't controlled. Consider "
                             "build_master_chain."))
    if delta > 6:
        result["warning"] = (f"{delta} dB is a big push into the limiter: it will squash transients and "
                             "won't gain the full amount. Consider raising the mix level first.")
    result["next"] = "Export the mix again and call match_loudness with the new file to check."
    return result


@mcp.tool()
def raw_command(command: str, params: dict | None = None) -> dict:
    """Escape hatch: send any Remote Script command directly. Use 'list_commands' to see all.
    The bridge's reply is returned under "result"."""
    return {"result": live.send(command, **(params or {}))}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
