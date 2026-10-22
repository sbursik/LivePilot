"""Minimal stand-ins for Ableton's `Live` and `_Framework` modules so the Remote Script
can be imported and exercised outside Live. Only what the tests touch is modelled."""
import sys
import types


class FakeParam:
    def __init__(self, name, value=0.85, lo=0.0, hi=1.0, kind="volume"):
        self.name, self.value, self.min, self.max = name, value, lo, hi
        self.is_quantized = False
        self._kind = kind

    def str_for_value(self, v):
        if self._kind == "volume":
            # Monotonic stand-in for Live's fader curve: 0.85 -> 0 dB, 1.0 -> +6 dB.
            if v <= 0:
                return "-inf dB"
            db = (v - 0.85) * 40.0 if v >= 0.85 else -70 * (1 - v / 0.85) ** 1.5
            return "%.1f dB" % db
        if self._kind == "db":  # value is the dB figure itself
            return "%.1f dB" % v
        if self._kind == "limiter_gain":  # like Live 12's Limiter: 0..1 is -24..24 dB, whole dB from 10
            db = -24.0 + 48.0 * v
            return "%.1f dB" % db if abs(db) < 10 else "%d dB" % round(db)
        if self._kind == "freq":  # log sweep like EQ Eight: 0 -> 20 Hz, 1 -> 20 kHz
            hz = 20.0 * 1000.0 ** v
            return "%.0f Hz" % hz if hz < 1000 else "%.2f kHz" % (hz / 1000)
        if self._kind == "time":  # 0 -> 1 ms, 1 -> 10 s
            ms = 10000.0 ** v
            return "%.1f ms" % ms if ms < 1000 else "%.2f s" % (ms / 1000)
        return "%.2f" % v


class FakeDevice:
    class_name, type, is_active = "AudioEffectGroupDevice", 2, True

    def __init__(self, name, parameters=(), class_name=None):
        self.name = name
        self.parameters = [FakeParam("Device On", 1.0)] + list(parameters)
        if class_name:
            self.class_name = class_name


class FakeStepParam(FakeParam):
    """A stepped control Live reports as continuous, with no value_items: Glue Compressor's
    Ratio, Attack and Release take raw values 0, 1, 2... and display these entries."""
    def __init__(self, name, items, index=0):
        super().__init__(name, float(index), 0.0, float(len(items) - 1))
        self._items = list(items)

    def str_for_value(self, v):
        return self._items[int(round(v))]


class FakeMenuParam(FakeParam):
    def __init__(self, name, items, index=0):
        super().__init__(name, float(index), 0.0, float(len(items) - 1))
        self.is_quantized, self.value_items = True, list(items)

    def str_for_value(self, v):
        return self.value_items[int(round(v))]


def live_device(name, legacy=False):
    """Live's own devices as the master-chain tools see them. legacy: Live 11's Limiter, whose
    gain is "Gain" and which has no Mode."""
    if name == "Limiter":
        params = [FakeParam("Gain", 0.0, -24.0, 24.0, kind="db") if legacy
                  else FakeParam("Input Gain", 0.5, kind="limiter_gain"),
                  FakeParam("Ceiling", 0.0, -24.0, 0.0, kind="db")]
        if not legacy:
            params.append(FakeMenuParam("Mode", ["Standard", "Soft Clip", "True Peak"]))
        return FakeDevice("Limiter", params, "Limiter")
    if name == "Glue Compressor":
        return FakeDevice("Glue Compressor", [
            FakeStepParam("Ratio", ["2", "4", "10"], 1),
            FakeStepParam("Attack", [".01", ".1", ".3", "1", "3", "10", "30"]),
            FakeStepParam("Release", [".1", ".2", ".4", ".6", ".8", "1.2", "A"]),
            FakeParam("Threshold", 0.0, -40.0, 0.0, kind="db")], "GlueCompressor")
    if name == "Utility":
        return FakeDevice("Utility", [FakeParam("Gain", 0.0, -35.0, 35.0, kind="db")], "StereoGain")
    return FakeDevice(name)


class FakeMixer:
    def __init__(self):
        self.volume = FakeParam("Track Volume")
        self.panning = FakeParam("Track Panning", 0.0, -1.0, 1.0, kind="pan")
        self.sends = []


class FakeNote:
    def __init__(self, pitch, start_time, duration, velocity, mute=False, probability=1.0):
        self.pitch, self.start_time, self.duration = pitch, start_time, duration
        self.velocity, self.mute, self.probability = velocity, mute, probability


class FakeClip:
    is_midi_clip = True
    is_audio_clip = False

    def __init__(self, length, start_time=None):
        self.name = ""
        self.is_arrangement_clip = start_time is not None
        self.start_time = start_time or 0.0
        self.end_time = self.start_time + length
        self.muted = False
        self.length = self.loop_end = self.end_marker = length
        self.loop_start = self.start_marker = 0.0
        self.looping = True
        self.is_playing = False
        self.color_index = 0
        self.signature_numerator = self.signature_denominator = 4
        self.notes = []

    def add_new_notes(self, specs):
        self.notes.extend(FakeNote(s.pitch, s.start_time, s.duration, s.velocity, s.mute) for s in specs)

    def get_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        return [n for n in self.notes if from_pitch <= n.pitch < from_pitch + pitch_span
                and from_time <= n.start_time < from_time + time_span]

    def remove_notes_extended(self, from_pitch, pitch_span, from_time, time_span):
        hit = self.get_notes_extended(from_pitch, pitch_span, from_time, time_span)
        self.notes = [n for n in self.notes if n not in hit]

    def apply_note_modifications(self, notes):
        pass  # notes are modified in place


class FakeSlot:
    def __init__(self):
        self.clip = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        self.clip = FakeClip(length)

    def delete_clip(self):
        self.clip = None

    def duplicate_clip_to(self, target):
        import copy
        target.clip = copy.copy(self.clip)


class FakeTrack:
    playing_slot_index = -1
    has_midi_input = True
    has_audio_input = False
    has_audio_output = True
    can_be_armed = True
    is_foldable = False

    def __init__(self, name, scenes=8):
        self.name = name
        self.mute = self.solo = self.arm = False
        self.mixer_device = FakeMixer()
        self.devices = []
        self.clip_slots = [FakeSlot() for _ in range(scenes)]
        self.arrangement_clips = []

    def delete_clip(self, clip):
        self.arrangement_clips.remove(clip)


class FakeGroupTrack(FakeTrack):
    is_foldable = True
    can_be_armed = False

    @property
    def arrangement_clips(self):
        raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")

    @arrangement_clips.setter
    def arrangement_clips(self, value):
        pass


class FakeCue:
    def __init__(self, song, time, name=""):
        self._song, self.time, self.name = song, time, name

    def jump(self):
        self._song.current_song_time = self.time


class FakeSong:
    # Like Live, a new playhead position is only applied on the next update tick, and reading
    # current_song_time returns the old position until then.
    @property
    def current_song_time(self):
        return self._song_time

    @current_song_time.setter
    def current_song_time(self, value):
        self._pending_time, self._pending_ticks = value, self.playhead_lag

    def tick(self):
        if self._pending_time is not None:
            self._pending_ticks -= 1
            if self._pending_ticks <= 0:
                self._song_time, self._pending_time = self._pending_time, None
        elif self.is_playing:
            self._song_time += self.beats_per_tick

    def set_or_delete_cue(self):
        at = [c for c in self.cue_points if c.time == self._song_time]
        if at:
            self.cue_points.remove(at[0])
        else:
            self.cue_points.append(FakeCue(self, self._song_time))

    def __init__(self):
        self.tempo = 120.0
        self.signature_numerator = self.signature_denominator = 4
        self.is_playing = False
        self._song_time, self._pending_time, self._pending_ticks = 0.0, None, 0
        self.playhead_lag = 1  # ticks before a playhead move lands; Live sometimes takes more
        self.beats_per_tick = 0.0  # how far the playhead moves per update tick while playing
        self.record_mode = self.session_automation_record = False
        self.punch_in = self.punch_out = False
        self.loop, self.loop_start, self.loop_length = False, 0.0, 16.0
        self.metronome = False
        self.tracks = [FakeTrack("1-MIDI")]
        self.return_tracks = []
        self.master_track = FakeTrack("Master")
        self.scenes = [types.SimpleNamespace(name="") for _ in range(8)]
        self.cue_points = []
        self.root_note, self.scale_name, self.scale_mode = 0, "Major", False
        self.scale_intervals = [0, 2, 4, 5, 7, 9, 11]
        self.view = types.SimpleNamespace(selected_track=self.tracks[0])

    def create_midi_track(self, index):
        t = FakeTrack("%d-MIDI" % (len(self.tracks) + 1))
        if index == -1:
            self.tracks.append(t)
        else:
            self.tracks.insert(index, t)

    def move_device(self, device, target, position):
        # Like Live, position is an insertion point counted before the device leaves its chain.
        if device in target.devices and target.devices.index(device) < position:
            position -= 1
        for t in self.tracks + self.return_tracks + [self.master_track]:
            if device in t.devices:
                t.devices.remove(device)
        target.devices.insert(position, device)
        return position

    def start_playing(self):
        self.is_playing = True

    def continue_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False


class FakeControlSurface:
    def __init__(self, c_instance):
        self._song = FakeSong()
        self.messages = []

    def song(self):
        return self._song

    def show_message(self, msg):
        self.messages.append(msg)

    def log_message(self, msg):
        self.messages.append(msg)

    def update_display(self):
        self._song.tick()

    def disconnect(self):
        pass


class FakeItem:
    def __init__(self, name, children=(), loadable=True, device=False):
        self.name, self.uri = name, "query:" + name
        self.children = list(children)
        self.is_folder = bool(children)
        self.is_loadable = loadable and not children
        self.is_device = device


def fake_browser(samples=40000):
    serum = FakeItem("Serum 2", device=True)
    return types.SimpleNamespace(
        instruments=FakeItem("Instruments", [FakeItem("Operator", device=True), FakeItem("Wavetable", device=True)]),
        audio_effects=FakeItem("Audio Effects", [FakeItem(n, device=True) for n in
                                                 ("EQ Eight", "Glue Compressor", "Limiter", "Utility")]),
        midi_effects=FakeItem("MIDI Effects", [FakeItem("Arpeggiator", device=True)]),
        drums=FakeItem("Drums", [FakeItem("808 Core Kit.adg")]),
        sounds=FakeItem("Sounds", [FakeItem("Serum Bass Lead.adv")]),
        # Big enough to exhaust a shared search budget, like a real sample library.
        samples=FakeItem("Samples", [FakeItem("Kick %d.wav" % i) for i in range(samples)]),
        clips=FakeItem("Clips", []),
        plugins=FakeItem("Plug-Ins", [FakeItem("VST3", [FakeItem("Xfer Records", [serum])])]),
        max_for_live=FakeItem("Max for Live", []),
        user_library=FakeItem("User Library", []),
        packs=FakeItem("Packs", []),
        current_project=FakeItem("Current Project", []),
        loaded=[],
        load_item=lambda item: None,
    )


def install():
    live = types.ModuleType("Live")
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=lambda **kw: types.SimpleNamespace(**kw),
                                      WarpMarker=lambda **kw: types.SimpleNamespace(**kw))
    app = types.SimpleNamespace(get_major_version=lambda: 12, get_minor_version=lambda: 2,
                                get_bugfix_version=lambda: 0, browser=fake_browser())
    live.Application = types.SimpleNamespace(get_application=lambda: app)
    sys.modules["Live"] = live

    fw = types.ModuleType("_Framework")
    cs = types.ModuleType("_Framework.ControlSurface")
    cs.ControlSurface = FakeControlSurface
    sys.modules["_Framework"] = fw
    sys.modules["_Framework.ControlSurface"] = cs


class RecordingParam(FakeParam):
    """Logs what Live would record into automation: values set while recording and playing."""

    def __init__(self, song, *args, **kw):
        self._song, self.recorded, self.gestures = song, [], []
        super().__init__(*args, **kw)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = v
        song = getattr(self, "_song", None)
        if song is not None and song.is_playing and song.record_mode and song.session_automation_record:
            self.recorded.append((song.current_song_time, v))

    def begin_gesture(self):
        self.gestures.append("begin")

    def end_gesture(self):
        self.gestures.append("end")
