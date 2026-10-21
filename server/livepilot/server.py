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
- Plugin parameters are only reachable if Live exposes them (get_device_parameters). For big
  synths like Serum, Live shows parameters only after the user adds them with Configure.
- You can't hear audio. Before judging or changing a mix, measure it with the analyser tools
  (analyze_clip, analyze_audio_file, compare_to_reference, analyze_frequency_space,
  score_samples) and base decisions on those numbers.
"""
import os
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import music
from .connection import LiveConnection

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
    """Copy a clip. Without a target, it duplicates into the next free slot below (like Ctrl+D)."""
    if target_track_index is None or target_slot_index is None:
        return _send("duplicate_clip_slot", track_index=track_index, slot_index=slot_index)
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
                                                 "complex_pro"] | None = None) -> dict:
    """Adjust an audio clip (e.g. a vocal): gain (0..1 slider position, ~0.4 = 0 dB), pitch,
    warping and warp mode (complex_pro suits vocals). Pass slot_index for a Session clip or
    arrangement_clip_index for an Arrangement clip."""
    modes = {"beats": 0, "tones": 1, "texture": 2, "repitch": 3, "complex": 4, "complex_pro": 6}
    return _send("set_audio_clip_properties", gain=gain, pitch_coarse=transpose_semitones,
                 pitch_fine=detune_cents, warping=warping, warp_mode=modes[warp_mode] if warp_mode else None,
                 **_ref(track_index, slot_index, arrangement_clip_index))


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
    """Set a device parameter by index or name. value is the raw value (check min/max with
    get_device_parameters). For switch/menu parameters you can pass display_value (e.g. 'On')."""
    return _send("set_device_parameter", track_index=track_index, device_index=device_index,
                 parameter=parameter, value=value, display_value=display_value, kind=kind)


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
def load_browser_item(uri: str, track_index: int | None = None, kind: TrackKind = "track") -> dict:
    """Load a browser item (instrument, effect, preset, drum kit, sample) onto a track.
    Get the uri from search_browser or browse. Effects are added after the existing devices."""
    return _send("load_browser_item", uri=uri, track_index=track_index, kind=kind)


_LOAD_ORDER = {
    "instrument": ["plugins", "instruments", "sounds", "drums", "user_library"],
    "audio_effect": ["audio_effects", "plugins", "user_library"],
    "midi_effect": ["midi_effects", "plugins", "user_library"],
    "any": ["all"],
}


@mcp.tool()
def load_device_by_name(name: str, track_index: int,
                        type: Literal["instrument", "audio_effect", "midi_effect", "any"] = "any",
                        kind: TrackKind = "track") -> dict:
    """Find a device, plugin or preset by name and load the best match onto a track. Works for
    Live devices ('Operator', 'EQ Eight', 'Compressor'), presets and kits ('808 Core Kit'), and
    third-party plugins ('Serum 2', 'Diva', 'Pro-Q 3'). When the user names a plugin, always load
    it with this tool rather than substituting a Live instrument. If nothing matches, report that
    instead of loading something else."""
    found: list[dict] = []
    for category in _LOAD_ORDER[type]:
        found = _send("search_browser", query=name, category=category, max_results=10)["results"]
        if found and found[0]["rank"] == 0:
            break
    if not found:
        return {"error": f"nothing named {name!r} found in Live's browser. If it's a plugin, check it "
                         "is installed and enabled under Settings > Plug-Ins and rescan."}
    pick = found[0]
    result = _send("load_browser_item", uri=pick["uri"], track_index=track_index, kind=kind)
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
def place_clip_in_arrangement(track_index: int, slot_index: int, time: float, repeats: int = 1) -> dict:
    """Copy a session clip into the arrangement at `time` (beats), back to back `repeats` times.
    Use this to build sections: e.g. bars 1-16 intro = time 0 to 64."""
    info = _send("get_clip_info", track_index=track_index, slot_index=slot_index)
    length = info["loop_end"] - info["loop_start"] if info["looping"] else info["length"]
    for i in range(repeats):
        _send("duplicate_clip_to_arrangement", track_index=track_index, slot_index=slot_index,
              time=time + i * length)
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
    start = end = None
    region = {"description": "whole file (clip is unwarped, so its markers can't be mapped to the file)"}
    markers = info.get("warp_markers") or []
    if info.get("warping") and len(markers) >= 2:
        b0, b1, description = _played_beats(info)
        start = max(0.0, _beats_to_seconds(b0, markers))
        end = _beats_to_seconds(b1, markers)
        region = {"description": description, "clip_beats": [round(b0, 3), round(b1, 3)],
                  "file_seconds": [round(start, 3), round(end, 3)]}
    a = _analysis()
    result = a.public(a.analyze(info["file_path"], start, end, _song_tempo()))
    result["clip"] = {"name": info["name"], "region": region}
    return result


def _played_beats(info: dict) -> tuple[float, float, str]:
    """The span of clip content (in clip beats) that the clip plays.

    Trimming an Arrangement clip changes its length in the arrangement but not its end marker, so
    for Arrangement clips the played span is start marker + arrangement length, not the end marker."""
    looping = info.get("looping")
    loop = (info["loop_start"], info["loop_end"])
    if info.get("start_time") is not None:  # Arrangement clip
        length = info["end_time"] - info["start_time"]
        if looping:
            if length >= loop[1] - loop[0]:
                return loop[0], loop[1], "loop region (repeats in the arrangement)"
            begin = info["start_marker"]
            return begin, min(loop[1], begin + length), "part of the loop played in the arrangement"
        begin = info["start_marker"]
        return begin, min(info["end_marker"], begin + length), "region played in the arrangement"
    if looping:
        return loop[0], loop[1], "loop region"
    return info["start_marker"], info["end_marker"], "between start and end markers"


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


def _unmeasured_tracks() -> list[str]:
    """Unmuted MIDI tracks: they make sound in the set but have no audio file to measure."""
    tracks = _send("get_session_info", include_clips=False)["tracks"]
    return [t["name"] for t in tracks if t["type"] == "midi" and not t["mute"]]


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
            space["warning"] = (f"{len(missing)} MIDI/instrument track(s) aren't in this map ({', '.join(missing)}). "
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


@mcp.tool()
def raw_command(command: str, params: dict | None = None) -> dict:
    """Escape hatch: send any Remote Script command directly. Use 'list_commands' to see all.
    The bridge's reply is returned under "result"."""
    return {"result": live.send(command, **(params or {}))}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
