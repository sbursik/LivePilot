"""Command handlers. Every public `cmd_<name>` method is a wire command.

All handlers run on Live's main thread. Keep them free of blocking calls.
Indices are 0-based. Times are in beats (quarter notes).
"""
import random
import re

import Live

TRACK_KINDS = ("track", "return", "master")
_EXT_RE = re.compile(r"\.(adg|adv|amxd|alc|als|vst3|vst|component|dll|wav|aif|aiff|mp3|flac|ogg)$")
_DB_RE = re.compile(r"(-?inf|-?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)


class CommandError(Exception):
    pass


class Handlers(object):
    def __init__(self, surface):
        self._surface = surface
        self._browser_cache = {}

    @property
    def song(self):
        return self._surface.song()

    @property
    def browser(self):
        return Live.Application.get_application().browser

    def run(self, cmd, params):
        fn = getattr(self, "cmd_" + str(cmd), None)
        if fn is None:
            raise CommandError("unknown command: %s" % cmd)
        return fn(**params)

    def cmd_list_commands(self):
        return sorted(n[4:] for n in dir(self) if n.startswith("cmd_"))

    # ================================================================ helpers

    def _track(self, track_index=0, kind="track"):
        if kind == "master":
            return self.song.master_track
        tracks = self.song.return_tracks if kind == "return" else self.song.tracks
        if kind not in TRACK_KINDS:
            raise CommandError("kind must be one of %s" % (TRACK_KINDS,))
        if not 0 <= track_index < len(tracks):
            raise CommandError("%s index %d out of range (0-%d)" % (kind, track_index, len(tracks) - 1))
        return tracks[track_index]

    def _slot(self, track_index, slot_index):
        track = self._track(track_index)
        if not 0 <= slot_index < len(track.clip_slots):
            raise CommandError("slot index %d out of range (0-%d)" % (slot_index, len(track.clip_slots) - 1))
        return track.clip_slots[slot_index]

    def _clip(self, track_index, slot_index=None, arrangement_clip_index=None):
        """A Session clip (slot_index) or an Arrangement clip (arrangement_clip_index, the
        position in the track's arrangement clips ordered by start time)."""
        if arrangement_clip_index is not None:
            clips = self._arrangement_clips(self._track(track_index))
            if not 0 <= arrangement_clip_index < len(clips):
                raise CommandError("arrangement clip index %d out of range (track has %d)"
                                   % (arrangement_clip_index, len(clips)))
            return clips[arrangement_clip_index]
        if slot_index is None:
            raise CommandError("pass slot_index (Session) or arrangement_clip_index (Arrangement)")
        slot = self._slot(track_index, slot_index)
        if not slot.has_clip:
            raise CommandError("no clip in track %d slot %d" % (track_index, slot_index))
        return slot.clip

    @staticmethod
    def _arrangement_clips(track):
        return sorted(getattr(track, "arrangement_clips", []), key=lambda c: c.start_time)

    def _midi_clip(self, track_index, slot_index=None, arrangement_clip_index=None):
        clip = self._clip(track_index, slot_index, arrangement_clip_index)
        if not clip.is_midi_clip:
            raise CommandError("clip is not a MIDI clip")
        return clip

    def _scene(self, scene_index):
        scenes = self.song.scenes
        if not 0 <= scene_index < len(scenes):
            raise CommandError("scene index %d out of range (0-%d)" % (scene_index, len(scenes) - 1))
        return scenes[scene_index]

    def _device(self, track_index, device_index, kind="track"):
        devices = self._track(track_index, kind).devices
        if not 0 <= device_index < len(devices):
            raise CommandError("device index %d out of range (0-%d)" % (device_index, len(devices) - 1))
        return devices[device_index]

    @staticmethod
    def _track_kind_name(track):
        if getattr(track, "is_foldable", False):
            return "group"
        if track.has_midi_input:
            return "midi"
        if track.has_audio_input:
            return "audio"
        return "other"

    @staticmethod
    def _param_info(param, index=None):
        info = {
            "name": param.name,
            "value": param.value,
            "min": param.min,
            "max": param.max,
            "display": param.str_for_value(param.value),
            "is_quantized": param.is_quantized,
        }
        if index is not None:
            info["index"] = index
        if param.is_quantized:
            info["value_items"] = list(param.value_items)
        return info

    @staticmethod
    def _parse_db(text):
        m = _DB_RE.search(text)
        if not m:
            return None
        v = m.group(1).lower()
        if v.endswith("inf"):
            return float("-inf")
        return float(v)

    def _set_param_db(self, param, db):
        """Bisect the parameter's range until its display string reads `db`."""
        lo, hi = param.min, param.max
        if self._parse_db(param.str_for_value(hi)) is None:
            raise CommandError("parameter %s does not display dB" % param.name)
        for _ in range(40):
            mid = (lo + hi) / 2.0
            cur = self._parse_db(param.str_for_value(mid))
            if cur is None or cur < db:
                lo = mid
            else:
                hi = mid
        param.value = hi
        return param.str_for_value(param.value)

    @staticmethod
    def _set_param(param, value):
        value = max(param.min, min(param.max, float(value)))
        param.value = value
        return param.str_for_value(param.value)

    @staticmethod
    def _note_dict(n):
        return {
            "pitch": n.pitch,
            "start": round(n.start_time, 6),
            "duration": round(n.duration, 6),
            "velocity": round(n.velocity, 2),
            "mute": bool(n.mute),
            "probability": round(getattr(n, "probability", 1.0), 3),
        }

    @staticmethod
    def _all_notes(clip):
        span = max(clip.length, clip.loop_end, clip.end_marker) + 1.0
        return clip.get_notes_extended(0, 128, 0.0, span)

    # ========================================================== session/song

    def cmd_ping(self):
        return {"pong": True, "live_version": "%d.%d.%d" % (
            Live.Application.get_application().get_major_version(),
            Live.Application.get_application().get_minor_version(),
            Live.Application.get_application().get_bugfix_version())}

    def cmd_get_session_info(self, include_clips=True):
        song = self.song
        tracks = []
        for i, t in enumerate(song.tracks):
            info = {
                "index": i,
                "name": t.name,
                "type": self._track_kind_name(t),
                "mute": t.mute,
                "solo": t.solo,
                "volume": t.mixer_device.volume.str_for_value(t.mixer_device.volume.value),
                "pan": round(t.mixer_device.panning.value, 3),
                "devices": [d.name for d in t.devices],
            }
            if t.can_be_armed:
                info["arm"] = t.arm
            if include_clips:
                info["clips"] = [
                    {"slot": si, "name": s.clip.name, "length": s.clip.length,
                     "playing": s.clip.is_playing}
                    for si, s in enumerate(t.clip_slots) if s.has_clip
                ]
            tracks.append(info)
        return {
            "tempo": song.tempo,
            "time_signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
            "is_playing": song.is_playing,
            "song_time": song.current_song_time,
            "loop": {"enabled": song.loop, "start": song.loop_start, "length": song.loop_length},
            "metronome": song.metronome,
            "key": self._key_info(),
            "tracks": tracks,
            "return_tracks": [
                {"index": i, "name": t.name,
                 "volume": t.mixer_device.volume.str_for_value(t.mixer_device.volume.value),
                 "devices": [d.name for d in t.devices]}
                for i, t in enumerate(song.return_tracks)
            ],
            "master": {
                "volume": song.master_track.mixer_device.volume.str_for_value(
                    song.master_track.mixer_device.volume.value),
                "devices": [d.name for d in song.master_track.devices],
            },
            "scenes": [{"index": i, "name": s.name} for i, s in enumerate(song.scenes)],
            "locators": [{"index": i, "name": c.name, "time": c.time} for i, c in enumerate(song.cue_points)],
        }

    def cmd_set_tempo(self, bpm):
        self.song.tempo = max(20.0, min(999.0, float(bpm)))
        return {"tempo": self.song.tempo}

    _NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

    def _key_info(self):
        song = self.song
        if not hasattr(song, "root_note"):
            return None  # Live 11 has no song-level key
        return {"root": self._NOTE_NAMES[song.root_note], "root_note": song.root_note,
                "scale": song.scale_name, "scale_mode": song.scale_mode,
                "intervals": list(song.scale_intervals)}

    def cmd_set_song_key(self, root_note=None, scale_name=None, scale_mode=None):
        """root_note: 0-11 (C=0). scale_name: Live's name, e.g. 'Major', 'Minor', 'Dorian'.
        scale_mode: highlight the scale in clips and keep MIDI devices in key."""
        song = self.song
        if not hasattr(song, "root_note"):
            raise CommandError("song key needs Live 12 or later")
        if root_note is not None:
            song.root_note = int(root_note) % 12
        if scale_name is not None:
            previous = song.scale_name
            song.scale_name = scale_name
            if song.scale_name != scale_name:
                song.scale_name = previous
                raise CommandError("Live did not accept scale %r" % scale_name)
        if scale_mode is not None:
            song.scale_mode = bool(scale_mode)
        return self._key_info()

    def cmd_set_time_signature(self, numerator, denominator):
        self.song.signature_numerator = int(numerator)
        self.song.signature_denominator = int(denominator)
        return {"time_signature": "%d/%d" % (self.song.signature_numerator, self.song.signature_denominator)}

    def cmd_start_playback(self):
        self.song.start_playing()
        return {"is_playing": True}

    def cmd_continue_playback(self):
        self.song.continue_playing()
        return {"is_playing": True}

    def cmd_stop_playback(self):
        self.song.stop_playing()
        return {"is_playing": False}

    def cmd_stop_all_clips(self):
        self.song.stop_all_clips()
        return {"stopped": True}

    def cmd_set_song_time(self, beats):
        self.song.current_song_time = max(0.0, float(beats))
        return {"song_time": self.song.current_song_time}

    def cmd_set_metronome(self, enabled):
        self.song.metronome = bool(enabled)
        return {"metronome": self.song.metronome}

    def cmd_set_loop(self, start=None, length=None, enabled=None):
        if start is not None:
            self.song.loop_start = max(0.0, float(start))
        if length is not None:
            self.song.loop_length = max(0.25, float(length))
        if enabled is not None:
            self.song.loop = bool(enabled)
        return {"enabled": self.song.loop, "start": self.song.loop_start, "length": self.song.loop_length}

    def cmd_set_record_mode(self, enabled):
        self.song.record_mode = bool(enabled)
        return {"record_mode": self.song.record_mode}

    def cmd_set_session_record(self, enabled):
        self.song.session_record = bool(enabled)
        return {"session_record": self.song.session_record}

    def cmd_capture_midi(self):
        self.song.capture_midi()
        return {"captured": True}

    def cmd_undo(self):
        if not self.song.can_undo:
            raise CommandError("nothing to undo")
        self.song.undo()
        return {"undone": True}

    def cmd_redo(self):
        if not self.song.can_redo:
            raise CommandError("nothing to redo")
        self.song.redo()
        return {"redone": True}

    def cmd_show_view(self, view):
        """view: Session | Arranger | Detail/Clip | Detail/DeviceChain | Browser"""
        Live.Application.get_application().view.show_view(view)
        return {"shown": view}

    # ================================================================ tracks

    def cmd_get_track_info(self, track_index, kind="track"):
        t = self._track(track_index, kind)
        mixer = t.mixer_device
        info = {
            "name": t.name,
            "type": "master" if kind == "master" else self._track_kind_name(t),
            "volume": mixer.volume.str_for_value(mixer.volume.value),
            "volume_raw": mixer.volume.value,
            "pan": round(mixer.panning.value, 3),
            "sends": [{"index": i, "display": s.str_for_value(s.value), "value": s.value}
                      for i, s in enumerate(mixer.sends)],
            "devices": [{"index": i, "name": d.name, "class_name": d.class_name, "is_active": d.is_active}
                        for i, d in enumerate(t.devices)],
            "color_index": getattr(t, "color_index", None),
        }
        if kind != "master":
            info.update({"mute": t.mute, "solo": t.solo})
        if kind == "track":
            info["arm"] = t.arm if t.can_be_armed else None
            info["clip_slots"] = [
                {"slot": i, "has_clip": s.has_clip,
                 "clip": ({"name": s.clip.name, "length": s.clip.length, "is_playing": s.clip.is_playing,
                           "is_midi": s.clip.is_midi_clip} if s.has_clip else None)}
                for i, s in enumerate(t.clip_slots)
            ]
            info["arrangement_clips"] = [self._arrangement_clip_summary(i, c)
                                         for i, c in enumerate(self._arrangement_clips(t))]
            try:
                info["input_routing"] = t.input_routing_type.display_name
                info["output_routing"] = t.output_routing_type.display_name
            except AttributeError:
                pass
        return info

    def cmd_create_midi_track(self, index=-1, name=None):
        self.song.create_midi_track(int(index))
        new_index = len(self.song.tracks) - 1 if index == -1 else int(index)
        if name:
            self.song.tracks[new_index].name = name
        return {"track_index": new_index, "name": self.song.tracks[new_index].name}

    def cmd_create_audio_track(self, index=-1, name=None):
        self.song.create_audio_track(int(index))
        new_index = len(self.song.tracks) - 1 if index == -1 else int(index)
        if name:
            self.song.tracks[new_index].name = name
        return {"track_index": new_index, "name": self.song.tracks[new_index].name}

    def cmd_create_return_track(self, name=None):
        self.song.create_return_track()
        new_index = len(self.song.return_tracks) - 1
        if name:
            self.song.return_tracks[new_index].name = name
        return {"return_index": new_index, "name": self.song.return_tracks[new_index].name}

    def cmd_delete_track(self, track_index, kind="track"):
        name = self._track(track_index, kind).name
        if kind == "return":
            self.song.delete_return_track(track_index)
        elif kind == "track":
            self.song.delete_track(track_index)
        else:
            raise CommandError("cannot delete the master track")
        return {"deleted": name}

    def cmd_duplicate_track(self, track_index):
        self._track(track_index)
        self.song.duplicate_track(track_index)
        return {"new_track_index": track_index + 1}

    def cmd_set_track_name(self, track_index, name, kind="track"):
        self._track(track_index, kind).name = name
        return {"name": name}

    def cmd_set_track_color(self, track_index, color_index, kind="track"):
        self._track(track_index, kind).color_index = int(color_index)
        return {"color_index": int(color_index)}

    def cmd_set_track_volume(self, track_index, db=None, value=None, kind="track"):
        param = self._track(track_index, kind).mixer_device.volume
        if db is not None:
            return {"volume": self._set_param_db(param, float(db))}
        if value is not None:
            return {"volume": self._set_param(param, value)}
        raise CommandError("pass db or value")

    def cmd_set_track_pan(self, track_index, pan, kind="track"):
        """pan: -1.0 (hard left) .. 1.0 (hard right)"""
        param = self._track(track_index, kind).mixer_device.panning
        return {"pan": self._set_param(param, pan)}

    def cmd_set_track_send(self, track_index, send_index, db=None, value=None, kind="track"):
        sends = self._track(track_index, kind).mixer_device.sends
        if not 0 <= send_index < len(sends):
            raise CommandError("send index %d out of range (%d sends)" % (send_index, len(sends)))
        param = sends[send_index]
        if db is not None:
            return {"send": self._set_param_db(param, float(db))}
        if value is not None:
            return {"send": self._set_param(param, value)}
        raise CommandError("pass db or value")

    def cmd_set_track_mute(self, track_index, mute, kind="track"):
        self._track(track_index, kind).mute = bool(mute)
        return {"mute": bool(mute)}

    def cmd_set_track_solo(self, track_index, solo, kind="track"):
        self._track(track_index, kind).solo = bool(solo)
        return {"solo": bool(solo)}

    def cmd_set_track_arm(self, track_index, arm):
        t = self._track(track_index)
        if not t.can_be_armed:
            raise CommandError("track cannot be armed")
        t.arm = bool(arm)
        return {"arm": t.arm}

    def cmd_select_track(self, track_index, kind="track"):
        self.song.view.selected_track = self._track(track_index, kind)
        return {"selected": self.song.view.selected_track.name}

    def cmd_stop_track_clips(self, track_index):
        self._track(track_index).stop_all_clips()
        return {"stopped": True}

    # --------------------------------------------------------------- routing

    def cmd_get_track_routing(self, track_index):
        t = self._track(track_index)
        return {
            "input": t.input_routing_type.display_name,
            "input_options": [r.display_name for r in t.available_input_routing_types],
            "input_channel": t.input_routing_channel.display_name,
            "input_channel_options": [r.display_name for r in t.available_input_routing_channels],
            "output": t.output_routing_type.display_name,
            "output_options": [r.display_name for r in t.available_output_routing_types],
            "monitoring": t.current_monitoring_state if t.can_be_armed else None,
        }

    @staticmethod
    def _pick_routing(options, name):
        for r in options:
            if r.display_name == name:
                return r
        lowered = name.lower()
        for r in options:
            if lowered in r.display_name.lower():
                return r
        raise CommandError("no routing matching %r; options: %s" % (name, [r.display_name for r in options]))

    def cmd_set_track_input_routing(self, track_index, name, channel=None):
        t = self._track(track_index)
        t.input_routing_type = self._pick_routing(t.available_input_routing_types, name)
        if channel:
            t.input_routing_channel = self._pick_routing(t.available_input_routing_channels, channel)
        return {"input": t.input_routing_type.display_name, "channel": t.input_routing_channel.display_name}

    def cmd_set_track_output_routing(self, track_index, name):
        t = self._track(track_index)
        t.output_routing_type = self._pick_routing(t.available_output_routing_types, name)
        return {"output": t.output_routing_type.display_name}

    def cmd_set_track_monitoring(self, track_index, state):
        """state: 0 = In, 1 = Auto, 2 = Off"""
        t = self._track(track_index)
        t.current_monitoring_state = int(state)
        return {"monitoring": t.current_monitoring_state}

    # ================================================================= clips

    def cmd_get_clip_info(self, track_index, slot_index=None, arrangement_clip_index=None):
        c = self._clip(track_index, slot_index, arrangement_clip_index)
        info = {
            "name": c.name,
            "is_midi": c.is_midi_clip,
            "length": c.length,
            "looping": c.looping,
            "loop_start": c.loop_start,
            "loop_end": c.loop_end,
            "start_marker": c.start_marker,
            "end_marker": c.end_marker,
            "is_playing": c.is_playing,
            "color_index": c.color_index,
            "signature": "%d/%d" % (c.signature_numerator, c.signature_denominator),
        }
        if c.is_arrangement_clip:
            info.update({"start_time": c.start_time, "end_time": c.end_time, "muted": c.muted})
        if c.is_audio_clip:
            info.update({
                "file_path": c.file_path,
                "gain": c.gain_display_string,
                "pitch_coarse": c.pitch_coarse,
                "pitch_fine": c.pitch_fine,
                "warping": c.warping,
                "warp_mode": int(c.warp_mode),
                "warp_markers": [{"beat_time": m.beat_time, "sample_time": m.sample_time}
                                 for m in getattr(c, "warp_markers", [])],
            })
        else:
            info["note_count"] = len(self._all_notes(c))
        return info

    def cmd_create_clip(self, track_index, slot_index, length=4.0, name=None):
        slot = self._slot(track_index, slot_index)
        if slot.has_clip:
            raise CommandError("slot already has a clip; delete it first")
        slot.create_clip(float(length))
        if name:
            slot.clip.name = name
        return {"name": slot.clip.name, "length": slot.clip.length}

    def cmd_create_audio_clip(self, track_index, slot_index, file_path):
        slot = self._slot(track_index, slot_index)
        if not hasattr(slot, "create_audio_clip"):
            raise CommandError("this Live version cannot create audio clips from the API")
        slot.create_audio_clip(file_path)
        return {"name": slot.clip.name, "length": slot.clip.length}

    def cmd_delete_clip(self, track_index, slot_index):
        slot = self._slot(track_index, slot_index)
        slot.delete_clip()
        return {"deleted": True}

    def cmd_duplicate_clip_slot(self, track_index, slot_index):
        """Duplicates the clip into the next empty slot below (Live's Ctrl+D)."""
        new_index = self._track(track_index).duplicate_clip_slot(slot_index)
        return {"new_slot_index": new_index}

    def cmd_copy_clip(self, track_index, slot_index, target_track_index, target_slot_index):
        src = self._slot(track_index, slot_index)
        dst = self._slot(target_track_index, target_slot_index)
        src.duplicate_clip_to(dst)
        return {"copied": True}

    def cmd_set_clip_name(self, track_index, slot_index=None, *, arrangement_clip_index=None, name):
        self._clip(track_index, slot_index, arrangement_clip_index).name = name
        return {"name": name}

    def cmd_set_clip_muted(self, track_index, slot_index=None, *, arrangement_clip_index=None, muted=True):
        """Deactivate a clip (Live's clip activator, key 0), mostly for arrangement clips."""
        c = self._clip(track_index, slot_index, arrangement_clip_index)
        c.muted = bool(muted)
        return {"muted": c.muted}

    def cmd_set_clip_color(self, track_index, slot_index=None, *, arrangement_clip_index=None, color_index):
        self._clip(track_index, slot_index, arrangement_clip_index).color_index = int(color_index)
        return {"color_index": int(color_index)}

    def cmd_fire_clip(self, track_index, slot_index):
        self._slot(track_index, slot_index).fire()
        return {"fired": True}

    def cmd_stop_clip(self, track_index, slot_index):
        self._slot(track_index, slot_index).stop()
        return {"stopped": True}

    def cmd_set_clip_loop(self, track_index, slot_index=None, *, arrangement_clip_index=None, loop_start=None, loop_end=None, looping=None,
                          start_marker=None, end_marker=None):
        c = self._clip(track_index, slot_index, arrangement_clip_index)
        if looping is not None:
            c.looping = bool(looping)
        # Order matters: Live rejects loop_start >= loop_end.
        if loop_end is not None and loop_start is not None and float(loop_start) >= c.loop_end:
            c.loop_end = float(loop_end)
            c.loop_start = float(loop_start)
        else:
            if loop_start is not None:
                c.loop_start = float(loop_start)
            if loop_end is not None:
                c.loop_end = float(loop_end)
        if end_marker is not None:
            c.end_marker = float(end_marker)
        if start_marker is not None:
            c.start_marker = float(start_marker)
        return {"looping": c.looping, "loop_start": c.loop_start, "loop_end": c.loop_end,
                "start_marker": c.start_marker, "end_marker": c.end_marker}

    def cmd_duplicate_clip_loop(self, track_index, slot_index=None, arrangement_clip_index=None):
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        c.duplicate_loop()
        return {"length": c.length}

    def cmd_set_audio_clip_properties(self, track_index, slot_index=None, *, arrangement_clip_index=None, gain=None, pitch_coarse=None,
                                      pitch_fine=None, warping=None, warp_mode=None):
        """gain: 0..1 normalized (Live's clip gain slider). warp_mode: 0 Beats, 1 Tones, 2 Texture,
        3 Re-Pitch, 4 Complex, 6 Complex Pro."""
        c = self._clip(track_index, slot_index, arrangement_clip_index)
        if not c.is_audio_clip:
            raise CommandError("clip is not an audio clip")
        if gain is not None:
            c.gain = max(0.0, min(1.0, float(gain)))
        if pitch_coarse is not None:
            c.pitch_coarse = int(pitch_coarse)
        if pitch_fine is not None:
            c.pitch_fine = float(pitch_fine)
        if warping is not None:
            c.warping = bool(warping)
        if warp_mode is not None:
            c.warp_mode = int(warp_mode)
        return {"gain": c.gain_display_string, "pitch_coarse": c.pitch_coarse,
                "pitch_fine": c.pitch_fine, "warping": c.warping, "warp_mode": int(c.warp_mode)}

    # ----------------------------------------------------------------- notes

    def cmd_get_notes(self, track_index, slot_index=None, arrangement_clip_index=None):
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        notes = sorted(self._all_notes(c), key=lambda n: (n.start_time, n.pitch))
        return {"length": c.length, "notes": [self._note_dict(n) for n in notes]}

    def cmd_add_notes(self, track_index, slot_index=None, *, arrangement_clip_index=None, notes, replace=False):
        """notes: [{pitch, start, duration, velocity?, mute?, probability?}]"""
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        if replace:
            c.remove_notes_extended(0, 128, 0.0, c.length + 1e6)
        specs = []
        for n in notes:
            kwargs = {
                "pitch": int(max(0, min(127, n["pitch"]))),
                "start_time": float(n["start"]),
                "duration": max(1.0 / 128, float(n.get("duration", 0.25))),
                "velocity": float(max(1, min(127, n.get("velocity", 100)))),
                "mute": bool(n.get("mute", False)),
            }
            if "probability" in n:
                kwargs["probability"] = float(max(0.0, min(1.0, n["probability"])))
            specs.append(Live.Clip.MidiNoteSpecification(**kwargs))
        c.add_new_notes(tuple(specs))
        return {"added": len(specs)}

    def cmd_remove_notes(self, track_index, slot_index=None, *, arrangement_clip_index=None, from_pitch=0, pitch_span=128,
                         from_time=0.0, time_span=None):
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        if time_span is None:
            time_span = max(c.length, c.end_marker) + 1.0
        c.remove_notes_extended(int(from_pitch), int(pitch_span), float(from_time), float(time_span))
        return {"removed": True}

    def _modify_notes(self, clip, fn, pitch=None):
        notes = self._all_notes(clip)
        changed = 0
        for n in notes:
            if pitch is not None and n.pitch != pitch:
                continue
            fn(n)
            changed += 1
        clip.apply_note_modifications(notes)
        return changed

    def cmd_quantize_notes(self, track_index, slot_index=None, *, arrangement_clip_index=None, grid=0.25, amount=1.0, quantize_ends=False,
                           pitch=None):
        """grid in beats: 0.25 = 1/16, 0.5 = 1/8, 1/3 = 1/8 triplet. amount 0..1."""
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        grid = float(grid)
        amount = max(0.0, min(1.0, float(amount)))

        def q(n):
            target = round(n.start_time / grid) * grid
            end = n.start_time + n.duration
            n.start_time = n.start_time + (target - n.start_time) * amount
            if quantize_ends:
                end_target = max(grid, round(end / grid) * grid)
                end = end + (end_target - end) * amount
            n.duration = max(1.0 / 128, end - n.start_time)

        return {"quantized": self._modify_notes(c, q, pitch)}

    def cmd_humanize_notes(self, track_index, slot_index=None, *, arrangement_clip_index=None, timing=0.01, velocity=8, pitch=None, seed=None):
        """timing: max random shift in beats. velocity: max random velocity change."""
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        rng = random.Random(seed)
        timing = abs(float(timing))
        velocity = abs(float(velocity))

        def h(n):
            n.start_time = max(0.0, n.start_time + rng.uniform(-timing, timing))
            n.velocity = max(1.0, min(127.0, n.velocity + rng.uniform(-velocity, velocity)))

        return {"humanized": self._modify_notes(c, h, pitch)}

    def cmd_transpose_notes(self, track_index, slot_index=None, *, arrangement_clip_index=None, semitones, pitch=None):
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)
        semitones = int(semitones)

        def t(n):
            n.pitch = max(0, min(127, n.pitch + semitones))

        return {"transposed": self._modify_notes(c, t, pitch)}

    def cmd_scale_velocity(self, track_index, slot_index=None, *, arrangement_clip_index=None, factor=1.0, offset=0.0, pitch=None):
        c = self._midi_clip(track_index, slot_index, arrangement_clip_index)

        def v(n):
            n.velocity = max(1.0, min(127.0, n.velocity * float(factor) + float(offset)))

        return {"changed": self._modify_notes(c, v, pitch)}

    # ================================================================ scenes

    def cmd_create_scene(self, index=-1, name=None):
        self.song.create_scene(int(index))
        new_index = len(self.song.scenes) - 1 if index == -1 else int(index)
        if name:
            self.song.scenes[new_index].name = name
        return {"scene_index": new_index}

    def cmd_delete_scene(self, scene_index):
        self._scene(scene_index)
        self.song.delete_scene(scene_index)
        return {"deleted": True}

    def cmd_duplicate_scene(self, scene_index):
        self._scene(scene_index)
        self.song.duplicate_scene(scene_index)
        return {"new_scene_index": scene_index + 1}

    def cmd_set_scene_name(self, scene_index, name):
        self._scene(scene_index).name = name
        return {"name": name}

    def cmd_fire_scene(self, scene_index):
        self._scene(scene_index).fire()
        return {"fired": True}

    # =============================================================== devices

    def cmd_get_devices(self, track_index, kind="track"):
        out = []
        for i, d in enumerate(self._track(track_index, kind).devices):
            info = {"index": i, "name": d.name, "class_name": d.class_name, "type": int(d.type),
                    "is_active": d.is_active, "parameter_count": len(d.parameters)}
            if getattr(d, "can_have_chains", False):
                info["chains"] = [{"name": ch.name, "devices": [cd.name for cd in ch.devices]}
                                  for ch in d.chains]
            if getattr(d, "can_have_drum_pads", False):
                info["drum_pads"] = [{"note": p.note, "name": p.name}
                                     for p in d.drum_pads if len(p.chains) > 0]
            out.append(info)
        return out

    def cmd_get_device_parameters(self, track_index, device_index, kind="track"):
        d = self._device(track_index, device_index, kind)
        return {"device": d.name, "parameters": [self._param_info(p, i) for i, p in enumerate(d.parameters)]}

    def _find_param(self, device, parameter):
        params = device.parameters
        if isinstance(parameter, int):
            if not 0 <= parameter < len(params):
                raise CommandError("parameter index out of range")
            return params[parameter]
        lowered = str(parameter).lower()
        for p in params:
            if p.name.lower() == lowered:
                return p
        for p in params:
            if lowered in p.name.lower():
                return p
        raise CommandError("no parameter %r on %s" % (parameter, device.name))

    def cmd_set_device_parameter(self, track_index, device_index, parameter, value=None, display_value=None,
                                 kind="track"):
        """parameter: index or name. value: raw value. display_value: an entry of value_items
        for quantized parameters (e.g. 'On')."""
        d = self._device(track_index, device_index, kind)
        p = self._find_param(d, parameter)
        if display_value is not None:
            items = list(p.value_items)
            if display_value not in items:
                raise CommandError("%r not in %s" % (display_value, items))
            p.value = p.min + items.index(display_value)
        elif value is not None:
            self._set_param(p, value)
        else:
            raise CommandError("pass value or display_value")
        return self._param_info(p)

    def cmd_set_device_active(self, track_index, device_index, active, kind="track"):
        d = self._device(track_index, device_index, kind)
        # Parameter 0 of every Live device is "Device On".
        d.parameters[0].value = 1.0 if active else 0.0
        return {"is_active": bool(active)}

    def cmd_delete_device(self, track_index, device_index, kind="track"):
        t = self._track(track_index, kind)
        name = self._device(track_index, device_index, kind).name
        t.delete_device(device_index)
        return {"deleted": name}

    def _plugin(self, track_index, device_index, kind):
        d = self._device(track_index, device_index, kind)
        if not hasattr(d, "presets"):
            raise CommandError("%s is not a plugin device" % d.name)
        return d

    def cmd_get_plugin_presets(self, track_index, device_index, kind="track"):
        """Programs the plugin reports to Live. Many modern plugins (Serum included) report few or
        none, because they manage presets in their own browser."""
        d = self._plugin(track_index, device_index, kind)
        return {"device": d.name, "selected_index": d.selected_preset_index,
                "presets": list(d.presets), "exposed_parameters": len(d.parameters) - 1}

    def cmd_set_plugin_preset(self, track_index, device_index, index=None, name=None, kind="track"):
        d = self._plugin(track_index, device_index, kind)
        presets = list(d.presets)
        if name is not None:
            matches = [i for i, p in enumerate(presets) if name.lower() in p.lower()]
            if not matches:
                raise CommandError("no plugin preset matching %r" % name)
            index = matches[0]
        if index is None or not 0 <= index < len(presets):
            raise CommandError("preset index out of range (%d presets)" % len(presets))
        d.selected_preset_index = int(index)
        return {"selected": presets[index], "index": index}

    def cmd_set_compressor_sidechain(self, track_index, device_index, source, enabled=True, kind="track"):
        """Route a Compressor/Gate sidechain input to `source` (a track name as shown in Live)."""
        d = self._device(track_index, device_index, kind)
        if not hasattr(d, "available_input_routing_types"):
            raise CommandError("%s has no sidechain routing exposed to the API" % d.name)
        d.input_routing_type = self._pick_routing(d.available_input_routing_types, source)
        for p in d.parameters:
            if p.name in ("S/C On", "Sidechain On", "S/C"):
                p.value = p.max if enabled else p.min
                break
        return {"sidechain_source": d.input_routing_type.display_name}

    # =============================================================== browser

    _BROWSER_ROOTS = ("instruments", "audio_effects", "midi_effects", "drums", "sounds", "samples",
                      "clips", "plugins", "max_for_live", "user_library", "packs", "current_project")

    def _root(self, category):
        if category not in self._BROWSER_ROOTS:
            raise CommandError("category must be one of %s" % (self._BROWSER_ROOTS,))
        root = getattr(self.browser, category, None)
        if root is None:
            raise CommandError("browser category %s not available" % category)
        return root

    def _remember(self, item):
        self._browser_cache[item.uri] = item
        return {"name": item.name, "uri": item.uri, "is_folder": item.is_folder,
                "is_loadable": item.is_loadable, "is_device": item.is_device}

    def cmd_browse(self, category, path=""):
        """List children of a browser folder. path: '/'-separated folder names below the category."""
        node = self._root(category)
        for part in [p for p in path.split("/") if p]:
            match = [c for c in node.children if c.name.lower() == part.lower()]
            if not match:
                raise CommandError("no folder %r under %s" % (part, node.name))
            node = match[0]
        return [self._remember(c) for c in node.children]

    # Searched by category="all", most useful first. Samples and clips are huge and only
    # searched when asked for, so they can't use up the budget before plugins are reached.
    _SEARCH_ORDER = ("plugins", "instruments", "audio_effects", "midi_effects", "drums", "sounds",
                     "max_for_live", "user_library", "packs")

    @staticmethod
    def _match_rank(name, query):
        """0 = exact, 1 = prefix, 2 = all words present, None = no match. Ignores case,
        spaces and file extensions, so 'serum 2' matches 'Serum2.vst3'."""
        base = _EXT_RE.sub("", name.lower())
        squashed, q = base.replace(" ", ""), query.lower().replace(" ", "")
        if squashed == q:
            return 0
        if squashed.startswith(q):
            return 1
        if all(t in base for t in query.lower().split()) or q in squashed:
            return 2
        return None

    def cmd_search_browser(self, query, category="all", max_results=25, max_nodes_per_category=15000):
        roots = self._SEARCH_ORDER if category == "all" else (category,)
        results = []
        truncated = []
        for r in roots:
            try:
                stack = [self._root(r)]
            except CommandError:
                continue
            visited = 0
            while stack and visited < max_nodes_per_category:
                node = stack.pop()
                visited += 1
                if node.is_loadable:
                    rank = self._match_rank(node.name, query)
                    if rank is not None:
                        info = self._remember(node)
                        info["category"] = r
                        info["rank"] = rank
                        results.append(info)
                children = node.children
                if children:
                    stack.extend(reversed(list(children)))
            if stack:
                truncated.append(r)
        # Exact matches first, then devices (instruments/plugins) before presets and samples.
        results.sort(key=lambda i: (i["rank"], not i["is_device"]))
        return {"results": results[:max_results], "total_matches": len(results),
                "truncated_categories": truncated}

    def cmd_load_browser_item(self, uri, track_index=None, kind="track"):
        """Load an item (device, preset, sample) onto a track, like double-clicking it in the browser.
        uri must come from search_browser or browse."""
        item = self._browser_cache.get(uri)
        if item is None:
            raise CommandError("unknown uri; call search_browser or browse first")
        if track_index is not None:
            self.song.view.selected_track = self._track(track_index, kind)
        self.browser.load_item(item)
        t = self.song.view.selected_track
        return {"loaded": item.name, "track": t.name, "devices": [d.name for d in t.devices]}

    # =========================================================== arrangement

    def cmd_get_locators(self):
        return [{"index": i, "name": c.name, "time": c.time} for i, c in enumerate(self.song.cue_points)]

    def cmd_add_locator(self, time, name=None):
        time = float(time)
        for c in self.song.cue_points:
            if abs(c.time - time) < 1e-6:
                if name:
                    c.name = name
                return {"name": c.name, "time": c.time, "existing": True}
        self.song.current_song_time = time
        self.song.set_or_delete_cue()
        for c in self.song.cue_points:
            if abs(c.time - time) < 1e-3:
                if name:
                    c.name = name
                return {"name": c.name, "time": c.time}
        return {"time": time, "note": "locator requested; Live may place it on the next update"}

    def cmd_delete_locator(self, index):
        cues = list(self.song.cue_points)
        if not 0 <= index < len(cues):
            raise CommandError("locator index out of range")
        self.song.current_song_time = cues[index].time
        self.song.set_or_delete_cue()
        return {"deleted": True}

    def cmd_jump_to_locator(self, index):
        cues = list(self.song.cue_points)
        if not 0 <= index < len(cues):
            raise CommandError("locator index out of range")
        cues[index].jump()
        return {"song_time": cues[index].time}

    def cmd_duplicate_clip_to_arrangement(self, track_index, slot_index, time):
        t = self._track(track_index)
        clip = self._clip(track_index, slot_index)
        if not hasattr(t, "duplicate_clip_to_arrangement"):
            raise CommandError("this Live version cannot place clips in the arrangement from the API")
        t.duplicate_clip_to_arrangement(clip, float(time))
        return {"placed": clip.name, "time": float(time), "length": clip.length}

    def cmd_create_arrangement_midi_clip(self, track_index, start, length):
        t = self._track(track_index)
        if not hasattr(t, "create_midi_clip"):
            raise CommandError("this Live version cannot create arrangement clips from the API")
        t.create_midi_clip(float(start), float(length))
        return {"created": True, "start": float(start), "length": float(length)}

    def cmd_delete_arrangement_clip(self, track_index, arrangement_clip_index):
        clip = self._clip(track_index, arrangement_clip_index=arrangement_clip_index)
        name = clip.name
        self._track(track_index).delete_clip(clip)
        return {"deleted": name}

    @staticmethod
    def _arrangement_clip_summary(index, c):
        info = {"arrangement_clip_index": index, "name": c.name, "start": c.start_time,
                "end": c.end_time, "type": "midi" if c.is_midi_clip else "audio", "muted": c.muted}
        if c.is_audio_clip:
            info["file_path"] = c.file_path
        return info

    def cmd_get_arrangement_clips(self, track_index=None):
        """Every clip in the Arrangement, per track (or one track), ordered by start time."""
        indices = [track_index] if track_index is not None else range(len(self.song.tracks))
        out = []
        for i in indices:
            t = self._track(i)
            clips = self._arrangement_clips(t)
            if clips or track_index is not None:
                out.append({"track_index": i, "track": t.name,
                            "clips": [self._arrangement_clip_summary(n, c) for n, c in enumerate(clips)]})
        return out

    def cmd_back_to_arrangement(self):
        self.song.back_to_arranger = False
        return {"back_to_arranger": True}
