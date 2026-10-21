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
        return "%.2f" % v


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


class FakeTrack:
    has_midi_input = True
    has_audio_input = False
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


class FakeSong:
    def __init__(self):
        self.tempo = 120.0
        self.signature_numerator = self.signature_denominator = 4
        self.is_playing = False
        self.current_song_time = 0.0
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

    def start_playing(self):
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
        pass

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
        audio_effects=FakeItem("Audio Effects", [FakeItem("EQ Eight", device=True)]),
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
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=lambda **kw: types.SimpleNamespace(**kw))
    app = types.SimpleNamespace(get_major_version=lambda: 12, get_minor_version=lambda: 2,
                                get_bugfix_version=lambda: 0, browser=fake_browser())
    live.Application = types.SimpleNamespace(get_application=lambda: app)
    sys.modules["Live"] = live

    fw = types.ModuleType("_Framework")
    cs = types.ModuleType("_Framework.ControlSurface")
    cs.ControlSurface = FakeControlSurface
    sys.modules["_Framework"] = fw
    sys.modules["_Framework.ControlSurface"] = cs
