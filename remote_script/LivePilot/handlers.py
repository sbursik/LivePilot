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
_DISPLAY_RE = re.compile(r"^\s*(-?inf|[-+]?\d*\.?\d+)\s*([a-z%]*)", re.IGNORECASE)
# Display units normalised to one base unit each, so "950 Hz" and "1.20 kHz" compare.
_UNITS = {"hz": ("hz", 1.0), "khz": ("hz", 1000.0), "ms": ("ms", 1.0), "s": ("ms", 1000.0),
          "db": ("db", 1.0), "%": ("%", 1.0), "l": ("pan", -1.0), "r": ("pan", 1.0), "": ("", 1.0)}


def _ptr(obj):
    """Stable identity for a Live object: the Python wrappers differ between reads."""
    return getattr(obj, "_live_ptr", id(obj))


class CommandError(Exception):
    pass


class Handlers(object):
    def __init__(self, surface):
        self._surface = surface
        self._browser_cache = {}
        self._jobs = {}  # id -> {"gen": generator, "status": dict}; see tick_jobs
        self._job_ids = 0

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

    def tick_jobs(self):
        """Advance background jobs one step. Called every update tick, before queued commands.
        Unlike multi-tick commands, a job doesn't hold up other commands, and its client polls
        for the result instead of waiting on one long reply."""
        for job in list(self._jobs.values()):
            status = job["status"]
            if status["state"] in ("done", "error", "cancelled"):
                continue
            try:
                next(job["gen"])
            except StopIteration:
                status["state"] = "done"
            except Exception as e:
                job["gen"].close()
                status.update(state="error", error=str(e))
                self._surface.log_message("LivePilot: job %s failed: %s" % (status["job"], e))

    def _start_job(self, gen, status):
        self._job_ids += 1
        status.update(job=self._job_ids, state="starting")
        self._jobs = {k: v for k, v in self._jobs.items()
                      if v["status"]["state"] not in ("done", "error", "cancelled")}
        self._jobs[self._job_ids] = {"gen": gen, "status": status}
        return status

    def cmd_job_status(self, job):
        if job not in self._jobs:
            raise CommandError("no job %r" % job)
        return self._jobs[job]["status"]

    def cmd_cancel_job(self, job):
        if job not in self._jobs:
            raise CommandError("no job %r" % job)
        entry = self._jobs[job]
        entry["gen"].close()  # runs the job's cleanup
        entry["status"]["state"] = "cancelled"
        return entry["status"]

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
        # Live raises RuntimeError (not AttributeError) for group, return and main tracks.
        if getattr(track, "is_foldable", False):
            return []
        try:
            clips = track.arrangement_clips
        except RuntimeError:
            return []
        return sorted(clips, key=lambda c: c.start_time)

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
        else:
            linear = Handlers._linear(param)
            if linear is not None:  # exact, where the display may round (Limiter: "14 dB")
                lo, hi, _ = linear
                info["display_number"] = round(
                    lo + (param.value - param.min) / (param.max - param.min) * (hi - lo), 3)
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

    @staticmethod
    def _parse_display(text):
        """'1.20 kHz' -> (1200.0, 'hz'), '-18.0 dB' -> (-18.0, 'db'), '25L' -> (-25.0, 'pan'),
        'C' -> (0.0, 'pan'). None if the text has no leading number or an unknown unit."""
        t = text.strip().lower()
        if t == "c":
            return 0.0, "pan"
        m = _DISPLAY_RE.match(t)
        if not m or m.group(2) not in _UNITS:
            return None
        unit, scale = _UNITS[m.group(2)]
        return float(m.group(1)) * scale, unit

    def _set_param_display(self, param, text):
        """Set a continuous parameter to the value whose display reads `text` ('350 Hz', '-18 dB',
        '150 ms', '25L')."""
        param.value = self._value_for_display(param, text)
        return param.str_for_value(param.value)

    def _value_for_display(self, param, text):
        """The raw value whose display reads `text`, found by bisecting the parameter's range.
        Assumes the display is monotonic, as it is for Live's frequency, gain, time and ratio
        controls. Stepped controls pick the matching entry instead."""
        steps = self._steps(param)
        if steps is not None:
            return param.min + self._match_item(steps, text)
        target, linear = self._parse_display(text), self._linear(param)
        if target is not None and linear is not None and target[1] in ("", linear[2]):
            lo, hi, _ = linear
            if target[0] in (float("inf"), float("-inf")):
                return param.max if (target[0] > 0) == (hi > lo) else param.min
            fraction = min(1.0, max(0.0, (target[0] - lo) / (hi - lo)))
            return param.min + fraction * (param.max - param.min)
        if target is None:
            raise CommandError("can't read %r as a value with a unit (e.g. '350 Hz', '-18 dB', '150 ms')"
                               % text)
        lo_d, hi_d = (self._parse_display(param.str_for_value(v)) for v in (param.min, param.max))
        if lo_d is None or hi_d is None:
            raise CommandError("%s displays %r..%r, which can't be set by display value; pass value"
                               % (param.name, param.str_for_value(param.min), param.str_for_value(param.max)))
        if target[1] and target[1] != lo_d[1]:
            raise CommandError("%s is shown in %r, not %r" % (param.name, param.str_for_value(param.max), text))
        goal, rising = target[0], hi_d[0] >= lo_d[0]
        if goal in (float("inf"), float("-inf")):
            return param.max if (goal > 0) == rising else param.min
        lo, hi = param.min, param.max
        for _ in range(40):
            mid = (lo + hi) / 2.0
            cur = self._parse_display(param.str_for_value(mid))
            below = cur is None or cur[0] < goal
            if below == rising:
                lo = mid
            else:
                hi = mid
        near = [(abs(d[0] - goal), v) for v in (lo, hi)
                for d in [self._parse_display(param.str_for_value(v))] if d is not None]
        return min(near)[1] if near else hi

    @staticmethod
    def _steps(param):
        """Entries of a stepped control that Live reports as continuous with no value_items
        (Glue Compressor's Ratio, Attack and Release take 0, 1, 2...): an integer range where a
        half step shows a neighbour's display. None for continuous controls."""
        lo, hi = param.min, param.max
        if param.is_quantized or lo != int(lo) or hi != int(hi) or not 0 < hi - lo <= 64:
            return None
        if param.str_for_value(lo + 0.5) not in (param.str_for_value(lo), param.str_for_value(lo + 1)):
            return None
        return [param.str_for_value(lo + i) for i in range(int(hi - lo) + 1)]

    @staticmethod
    def _linear(param):
        """(display at min, display at max, unit) when the display is a straight line in its
        unit across the range, as for the Limiter's Input Gain (-24..24 dB, shown without
        decimals above 10 dB); values can then be worked out exactly instead of read off a
        rounded display. None otherwise (frequency, the volume fader, stepped controls)."""
        span = param.max - param.min
        if param.is_quantized or span <= 0:
            return None
        ends = [Handlers._parse_display(param.str_for_value(v)) for v in (param.min, param.max)]
        if None in ends or ends[0][1] != ends[1][1] or ends[0][1] in ("", "pan"):
            return None
        (lo, unit), (hi, _) = ends
        if lo == hi or float("inf") in (abs(lo), abs(hi)):
            return None
        for f in (0.1, 0.25, 0.5, 0.75, 0.9):
            shown = Handlers._parse_display(param.str_for_value(param.min + f * span))
            # Allow for rounding to whole units; a curve misses by far more.
            if shown is None or shown[1] != unit or abs(shown[0] - (lo + f * (hi - lo))) > 0.01 * abs(hi - lo) + 0.06:
                return None
        return lo, hi, unit

    def _set_param_db(self, param, db):
        if self._parse_db(param.str_for_value(param.max)) is None:
            raise CommandError("parameter %s does not display dB" % param.name)
        return self._set_param_display(param, "%s dB" % db)

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
            "locators": self._locator_list(),  # same time order and indices as manage_locator
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
        beats = max(0.0, float(beats))
        if self.song.is_playing:
            self.song.current_song_time = beats
        else:  # wait until Live applies it, so the reply reports the new position
            yield from self._move_playhead(beats)
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
        if not c.looping:
            # An unlooped clip plays from loop_start to loop_end, and Live ignores a start_marker
            # past loop_start, so markers set the region through the loop points.
            loop_start = start_marker if loop_start is None else loop_start
            loop_end = end_marker if loop_end is None else loop_end
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
                                      pitch_fine=None, warping=None, warp_mode=None, source_bpm=None):
        """gain: 0..1 normalized (Live's clip gain slider). warp_mode: 0 Beats, 1 Tones, 2 Texture,
        3 Re-Pitch, 4 Complex, 6 Complex Pro. source_bpm: the tempo the file was recorded at; warps
        the whole file at that tempo so it follows the song tempo."""
        c = self._clip(track_index, slot_index, arrangement_clip_index)
        if not c.is_audio_clip:
            raise CommandError("clip is not an audio clip")
        if source_bpm is not None:
            self._warp_at_tempo(c, float(source_bpm))
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
                "pitch_fine": c.pitch_fine, "warping": c.warping, "warp_mode": int(c.warp_mode),
                "length": c.length}

    def _warp_at_tempo(self, c, bpm):
        """Warp the whole file at one tempo (e.g. an acapella recorded at 128 BPM): keep the first
        warp marker and replace the rest with one marker on the file's last sample."""
        if not hasattr(c, "add_warp_marker"):
            raise CommandError("this Live version can't set warp markers from a script (needs Live 11+)")
        c.warping = True
        markers = sorted(c.warp_markers, key=lambda m: m.beat_time)
        first_beat, first_sec = (markers[0].beat_time, markers[0].sample_time) if markers else (0.0, 0.0)
        seconds = c.sample_length / float(c.sample_rate)
        end_beat = first_beat + (seconds - first_sec) * bpm / 60.0
        try:
            marker = Live.Clip.WarpMarker(beat_time=end_beat, sample_time=seconds)
        except Exception:
            marker = {"beat_time": end_beat, "sample_time": seconds}
        c.add_warp_marker(marker)
        for m in markers[1:]:
            if abs(m.beat_time - end_beat) > 1e-6:
                # Live lists a marker 1/32 beat after the last real one; it can't be removed and
                # moves when the last one does, so a stale copy of it raises here.
                try:
                    c.remove_warp_marker(m.beat_time)
                except Exception:
                    pass
        # Unwarped, the end markers were in seconds; put them on the file's end in beats.
        for attr in ("end_marker", "loop_end"):
            try:
                setattr(c, attr, end_beat)
            except Exception:
                pass

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

    def _match_item(self, items, text):
        """Index of the menu entry meaning `text`: exact (any case), then the same number ('10'
        for '10.0', '2' for '2:1'), then the only entry starting with it ('A' for 'Auto')."""
        lowered = [i.lower() for i in items]
        t = text.strip().lower()
        if t in lowered:
            return lowered.index(t)
        want = self._parse_display(t)
        if want is not None:
            for i, item in enumerate(items):
                got = self._parse_display(item)
                if got is not None and abs(got[0] - want[0]) < 1e-9 and (not want[1] or got[1] in ("", want[1])):
                    return i
        starts = [i for i, item in enumerate(lowered) if item.startswith(t) or (item and t.startswith(item))]
        if len(starts) == 1:
            return starts[0]
        raise CommandError("%r not in %s" % (text, items))

    def cmd_set_device_parameter(self, track_index, device_index, parameter, value=None, display_value=None,
                                 kind="track"):
        """parameter: index or name. value: raw value. display_value: what the parameter should
        show: an entry of value_items for switches and menus ('On'), or a number with its unit for
        continuous controls ('350 Hz', '2.5 kHz', '-18 dB', '150 ms', '25L')."""
        d = self._device(track_index, device_index, kind)
        p = self._find_param(d, parameter)
        if display_value is not None and p.is_quantized:
            items = list(p.value_items)
            p.value = p.min + self._match_item(items, str(display_value))
        elif display_value is not None:
            self._set_param_display(p, str(display_value))
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

    def cmd_load_browser_item(self, uri, track_index=None, kind="track", position=None):
        """Load an item (device, preset, sample) onto a track, like double-clicking it in the browser.
        uri must come from search_browser or browse. position: where the new device goes in the
        chain (0 = first); Live adds it at the end, so it is moved there after loading."""
        item = self._browser_cache.get(uri)
        if item is None:
            raise CommandError("unknown uri; call search_browser or browse first")
        if track_index is not None:
            self.song.view.selected_track = self._track(track_index, kind)
        t = self.song.view.selected_track
        before = [_ptr(d) for d in t.devices]
        self.browser.load_item(item)
        result = {"loaded": item.name, "track": t.name}
        if position is not None:
            # A plugin can take a few ticks to appear in the chain.
            for _ in range(20):
                if len(t.devices) > len(before):
                    break
                yield
            new = [i for i, d in enumerate(t.devices) if _ptr(d) not in before]
            if not new:
                result["note"] = "no new device appeared on the track, so nothing was moved"
            else:
                self._move_device(t.devices[new[0]], t, position)
        result["devices"] = [d.name for d in t.devices]
        return result

    def _move_device(self, device, target, position):
        if not hasattr(self.song, "move_device"):
            raise CommandError("this Live version can't move devices from a script (needs Live 11+)")
        chain = [_ptr(d) for d in target.devices]
        current = chain.index(_ptr(device)) if _ptr(device) in chain else None
        if current is None:
            position = max(0, min(int(position), len(chain)))
        else:
            # Live counts the insertion point before taking the device out of its own chain, so a
            # move to a later slot needs one more to land at `position`.
            position = max(0, min(int(position), len(chain) - 1))
            if position > current:
                position += 1
        self.song.move_device(device, target, position)

    def cmd_move_device(self, track_index, device_index, position, kind="track",
                        target_track_index=None, target_kind=None):
        """Move a device to `position` in its own chain or another track's chain (0 = first)."""
        device = self._device(track_index, device_index, kind)
        target = self._track(track_index if target_track_index is None else target_track_index,
                             target_kind or kind)
        self._move_device(device, target, position)
        return {"track": target.name, "devices": [d.name for d in target.devices]}

    # ============================================================ automation

    # Live's API can't write breakpoints into a track's Arrangement automation lanes, so this
    # records them: it turns on automation and Arrangement recording, plays the span, and moves
    # each parameter along its points on every update tick (~10 Hz), like a hand on a controller.
    # Armed tracks are disarmed for the pass so no audio or MIDI is recorded.

    def _mixer_param(self, track, name):
        m = track.mixer_device
        n = str(name).strip().lower()
        if n in ("volume", "vol"):
            return m.volume
        if n in ("pan", "panning"):
            return m.panning
        if n.startswith("send") and len(n[4:].strip()) == 1 and n[4:].strip().isalpha():
            i = ord(n[4:].strip()) - ord("a")
            if i < len(m.sends):
                return m.sends[i]
        raise CommandError("mixer parameter must be 'volume', 'pan' or 'send A'..'send %s'"
                           % chr(ord("A") + max(0, len(m.sends) - 1)))

    def _lane(self, spec):
        kind = spec.get("kind", "track")
        track = self._track(spec.get("track_index", 0), kind)
        if spec.get("device_index") is None:
            param = self._mixer_param(track, spec.get("parameter", "volume"))
        else:
            param = self._find_param(self._device(spec.get("track_index", 0), spec["device_index"], kind),
                                     spec["parameter"])
        points = []
        for pt in spec.get("points") or []:
            if "display" in pt:
                text = str(pt["display"])
                value = (param.min + self._match_item(list(param.value_items), text) if param.is_quantized
                         else self._value_for_display(param, text))
            else:
                value = max(param.min, min(param.max, float(pt["value"])))
            points.append((float(pt["beat"]), value))
        if not points:
            raise CommandError("each lane needs points: [{beat, value or display}, ...]")
        points.sort()
        return {"track": track.name, "param": param, "points": points, "touched": False,
                "done": False, "last": None}

    @staticmethod
    def _value_at(points, beat):
        if beat <= points[0][0]:
            return points[0][1]
        for (b0, v0), (b1, v1) in zip(points, points[1:]):
            if beat <= b1:
                return v0 if b1 == b0 else v0 + (v1 - v0) * (beat - b0) / (b1 - b0)
        return points[-1][1]

    def cmd_record_automation(self, lanes, pre_roll_beats=None):
        """Record Arrangement automation for one or more parameters in a single playback pass.
        lanes: [{track_index, kind, device_index (omit for the mixer), parameter, points:
        [{beat, value or display}]}]; values move in straight lines between points. Runs as a
        job: poll job_status until its state is done."""
        song = self.song
        if song.is_playing:
            raise CommandError("stop playback first")
        if any(r["state"] in ("starting", "recording") for r in (j["status"] for j in self._jobs.values())):
            raise CommandError("an automation recording is already running")
        playing = [t.name for t in song.tracks if getattr(t, "playing_slot_index", -1) >= 0]
        if playing:
            raise CommandError("Session clips are playing on %s; stop them and press Back to Arrangement "
                               "first, or they'd be recorded into the Arrangement" % ", ".join(playing))
        parsed = [self._lane(spec) for spec in lanes]
        first = min(l["points"][0][0] for l in parsed)
        end = max(l["points"][-1][0] for l in parsed)
        bar = 4.0 * song.signature_numerator / song.signature_denominator
        pre = bar if pre_roll_beats is None else max(0.0, float(pre_roll_beats))
        start = max(0.0, first - pre)
        status = {"start_beat": start, "end_beat": end, "position": start,
                  "lanes": [{"track": l["track"], "parameter": l["param"].name,
                             "points": len(l["points"])} for l in parsed],
                  "seconds": round((end - start) * 60.0 / song.tempo, 1)}
        self._start_job(self._record_pass(parsed, start, end, status), status)
        return status

    def _record_pass(self, lanes, start, end, status):
        song = self.song
        armed = [t for t in song.tracks if getattr(t, "can_be_armed", False) and t.arm]
        saved = {"time": song.current_song_time, "loop": song.loop,
                 "automation": song.session_automation_record,
                 "punch_in": getattr(song, "punch_in", False), "punch_out": getattr(song, "punch_out", False)}
        # Ticks the pass may take before it's treated as stuck: twice its length at 10 Hz, plus slack.
        budget = int((end - start) * 60.0 / song.tempo * 20) + 200
        try:
            for t in armed:
                t.arm = False
            song.loop = False
            for attr in ("punch_in", "punch_out"):
                if hasattr(song, attr):
                    setattr(song, attr, False)
            for _ in self._move_playhead(start):
                yield
            song.session_automation_record = True
            song.record_mode = True
            if not song.is_playing:
                song.continue_playing()
            for _ in range(20):  # Live may start the transport on a later tick
                if song.is_playing:
                    break
                yield
            status["state"] = "recording"
            for _ in range(budget):
                yield
                if not song.is_playing:
                    raise CommandError("playback stopped at beat %.2f, before the end" % song.current_song_time)
                now = song.current_song_time
                status["position"] = round(now, 2)
                for lane in lanes:
                    self._drive(lane, now)
                if now >= end:
                    break
            else:
                raise CommandError("the pass didn't reach beat %s in time" % end)
            yield  # one more tick so the final values are written
        finally:
            for lane in lanes:
                if lane["touched"] and not lane["done"] and hasattr(lane["param"], "end_gesture"):
                    lane["param"].end_gesture()
            song.record_mode = False
            song.stop_playing()
            song.session_automation_record = saved["automation"]
            song.loop = saved["loop"]
            for attr in ("punch_in", "punch_out"):
                if hasattr(song, attr):
                    setattr(song, attr, saved[attr])
            for t in armed:
                t.arm = True
            song.current_song_time = saved["time"]

    def _drive(self, lane, now):
        points, param = lane["points"], lane["param"]
        if lane["done"] or now < points[0][0]:
            return
        if not lane["touched"]:
            if hasattr(param, "begin_gesture"):
                param.begin_gesture()
            lane["touched"] = True
        value = self._value_at(points, now)
        if param.is_quantized:
            value = float(round(value))
        # Also when the target holds still: automation already in the lane plays back and moves
        # the parameter, and only values set here are recorded over it.
        if value != lane["last"] or abs(param.value - value) > 1e-9:
            param.value = value
            lane["last"] = value
        if now >= points[-1][0]:
            if hasattr(param, "end_gesture"):
                param.end_gesture()
            lane["done"] = True

    # =========================================================== arrangement

    # Live's API can only create or remove a locator by toggling one at the playhead
    # (set_or_delete_cue); a cue's time is read-only. The playhead position is applied on a later
    # update tick, so these commands are generators: they move the playhead, wait until Live
    # reports it there, toggle, wait for the cue list to update, verify, then restore the playhead.

    def _cues(self):
        return sorted(self.song.cue_points, key=lambda c: c.time)

    def _cue_at(self, time):
        for c in self.song.cue_points:
            if abs(c.time - time) < 1e-3:
                return c
        return None

    def _locator_list(self):
        return [{"index": i, "name": c.name, "time": c.time} for i, c in enumerate(self._cues())]

    def _move_playhead(self, time, ticks=20):
        if self.song.is_playing:
            raise CommandError("stop playback first: Live places locators at the playhead, "
                               "which moves while playing")
        self.song.current_song_time = time
        for _ in range(ticks):
            yield
            if abs(self.song.current_song_time - time) < 1e-6:
                return
        raise CommandError("Live did not move the playhead to %s" % time)

    def cmd_get_locators(self):
        """Locators ordered by time; index is the position in this list."""
        return self._locator_list()

    def cmd_add_locator(self, time, name=None):
        time = float(time)
        existing = self._cue_at(time)
        if existing is not None:
            if name:
                existing.name = name
            return {"name": existing.name, "time": existing.time, "existing": True}
        original = self.song.current_song_time
        yield from self._move_playhead(time)
        self.song.set_or_delete_cue()
        yield
        cue = self._cue_at(time)
        yield from self._move_playhead(original)
        if cue is None:
            raise CommandError("Live did not create a locator at %s" % time)
        if name:
            cue.name = name
        return {"name": cue.name, "time": cue.time, "locators": self._locator_list()}

    def cmd_delete_locator(self, index):
        cues = self._cues()
        if not 0 <= index < len(cues):
            raise CommandError("locator index %d out of range (%d locators)" % (index, len(cues)))
        target = cues[index]
        name, time = target.name, target.time
        original = self.song.current_song_time
        yield from self._move_playhead(time)
        self.song.set_or_delete_cue()
        yield
        yield from self._move_playhead(original)
        if self._cue_at(time) is not None:
            raise CommandError("Live did not remove the locator at %s" % time)
        return {"deleted": name, "time": time, "locators": self._locator_list()}

    def cmd_rename_locator(self, index, name):
        cues = self._cues()
        if not 0 <= index < len(cues):
            raise CommandError("locator index %d out of range (%d locators)" % (index, len(cues)))
        cues[index].name = name
        return {"name": name, "time": cues[index].time}

    def cmd_jump_to_locator(self, index):
        cues = self._cues()
        if not 0 <= index < len(cues):
            raise CommandError("locator index %d out of range (%d locators)" % (index, len(cues)))
        cues[index].jump()
        return {"song_time": cues[index].time}

    def cmd_duplicate_clip_to_arrangement(self, track_index, time, slot_index=None, arrangement_clip_index=None):
        """Copy a Session clip, or an Arrangement clip on the same track, to `time` (beats)."""
        t = self._track(track_index)
        clip = self._clip(track_index, slot_index, arrangement_clip_index)
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

    def cmd_get_audio_clip_files(self, include_muted=False):
        """Every audio clip in the set (Session and Arrangement) with its source file, for the
        analyser. Muted tracks and deactivated clips are skipped unless include_muted."""
        out = []
        for i, t in enumerate(self.song.tracks):
            if getattr(t, "is_foldable", False) or not t.has_audio_output:
                continue
            if t.mute and not include_muted:
                continue
            for si, s in enumerate(t.clip_slots):
                if s.has_clip and s.clip.is_audio_clip:
                    out.append({"track_index": i, "track": t.name, "slot_index": si,
                                "name": s.clip.name, "file_path": s.clip.file_path})
            for n, c in enumerate(self._arrangement_clips(t)):
                if c.is_audio_clip and (include_muted or not c.muted):
                    out.append({"track_index": i, "track": t.name, "arrangement_clip_index": n,
                                "name": c.name, "file_path": c.file_path, "start": c.start_time})
        return out

    def cmd_get_arrangement_clips(self, track_index=None):
        """Every clip in the Arrangement (or on one track) as a flat list, ordered by track and
        then start time. arrangement_clip_index counts per track, so a clip is addressed by
        track_index plus arrangement_clip_index."""
        indices = [track_index] if track_index is not None else range(len(self.song.tracks))
        out = []
        for i in indices:
            t = self._track(i)
            for n, c in enumerate(self._arrangement_clips(t)):
                info = {"track_index": i, "track": t.name}
                info.update(self._arrangement_clip_summary(n, c))
                out.append(info)
        return out

    def cmd_back_to_arrangement(self):
        self.song.back_to_arranger = False
        return {"back_to_arranger": True}
