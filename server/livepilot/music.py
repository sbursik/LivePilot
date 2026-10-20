"""Pure-Python music helpers: scales, chords, drum and bass pattern generators.

All generators return note dicts in the wire format used by add_notes:
{"pitch", "start", "duration", "velocity"} with times in beats.
"""
import random

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}

SCALES = {
    "major": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10],
    "harmonic_minor": [0, 2, 3, 5, 7, 8, 11],
    "dorian": [0, 2, 3, 5, 7, 9, 10],
    "phrygian": [0, 1, 3, 5, 7, 8, 10],
    "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10],
    "pentatonic_major": [0, 2, 4, 7, 9],
    "pentatonic_minor": [0, 3, 5, 7, 10],
    "blues": [0, 3, 5, 6, 7, 10],
}

# General MIDI / Ableton Drum Rack default pad mapping.
DRUMS = {"kick": 36, "rim": 37, "snare": 38, "clap": 39, "closed_hat": 42, "open_hat": 46,
         "low_tom": 45, "mid_tom": 47, "high_tom": 50, "crash": 49, "ride": 51, "shaker": 70}

# 16-step grids per bar (1 = hit). Velocity accents are applied afterwards.
DRUM_STYLES = {
    "tech_house": {
        "kick":       "x...x...x...x...",
        "clap":       "....x.......x...",
        "closed_hat": "xx.xxx.xxx.xxx.x",
        "open_hat":   "..x...x...x...x.",
        "rim":        "...x.....x....x.",
        "shaker":     "x.xxx.xxx.xxx.xx",
    },
    "four_on_the_floor": {
        "kick":       "x...x...x...x...",
        "clap":       "....x.......x...",
        "closed_hat": "..x...x...x...x.",
        "open_hat":   "..............x.",
    },
    "house": {
        "kick":       "x...x...x...x...",
        "clap":       "....x.......x...",
        "closed_hat": "xxxxxxxxxxxxxxxx",
        "open_hat":   "..x...x...x...x.",
    },
    "techno": {
        "kick":       "x...x...x...x...",
        "rim":        "...x..x....x..x.",
        "closed_hat": "..x...x...x...x.",
        "clap":       "....x.......x...",
    },
    "hiphop": {
        "kick":       "x.....x...x.....",
        "snare":      "....x.......x...",
        "closed_hat": "x.x.x.x.x.x.x.x.",
    },
    "boom_bap": {
        "kick":       "x......x..x.....",
        "snare":      "....x.......x...",
        "closed_hat": "x.x.x.x.x.x.x.xx",
    },
    "trap": {
        "kick":       "x......x..x.....",
        "snare":      "........x.......",
        "closed_hat": "x.x.x.x.xxx.x.x.",
    },
    "breakbeat": {
        "kick":       "x.........x.....",
        "snare":      "....x.......x..x",
        "closed_hat": "x.x.x.x.x.x.x.x.",
    },
    "drum_and_bass": {
        "kick":       "x.........x.....",
        "snare":      "....x.......x...",
        "closed_hat": "x.x.x.x.x.x.x.x.",
    },
    "reggaeton": {
        "kick":       "x...x...x...x...",
        "snare":      "...x..x....x..x.",
        "closed_hat": "x.x.x.x.x.x.x.x.",
    },
}


def note_number(name: str, octave: int = 3) -> int:
    """'C' octave 3 -> 60 (Ableton convention: C3 = middle C = 60)."""
    name = name.strip().capitalize()
    name = _FLATS.get(name, name)
    return NOTE_NAMES.index(name) + (octave + 2) * 12


def scale_pitches(root: str, scale: str, octave: int = 3, octaves: int = 1) -> list[int]:
    base = note_number(root, octave)
    steps = SCALES[scale]
    return [base + 12 * o + s for o in range(octaves) for s in steps] + [base + 12 * octaves]


# Voices that never get random hits or drops, so the groove's backbone stays intact.
_ANCHORS = {"kick", "clap", "snare"}


def _vary(steps: str, amount: float, rng: random.Random) -> str:
    """Randomly drop or add off-grid hits. amount 0..1."""
    out = []
    for i, ch in enumerate(steps):
        if ch == "x" and i % 4 != 0 and rng.random() < amount * 0.3:
            out.append(".")
        elif ch == "." and rng.random() < amount * 0.15:
            out.append("x")
        else:
            out.append(ch)
    return "".join(out)


def drum_pattern(style: str = "house", bars: int = 1, swing: float = 0.0, fill: bool = False,
                 variation: float = 0.0, seed: int | None = None) -> list[dict]:
    """Return a drum pattern. swing 0..1 delays off-beat 16ths up to a 1/32 note.
    variation 0..1 changes hats and percussion from bar to bar (every other bar answers the
    first), so a multi-bar loop doesn't sound copy-pasted. fill adds a roll at the very end."""
    if style not in DRUM_STYLES:
        raise ValueError(f"unknown style {style!r}; choose from {sorted(DRUM_STYLES)}")
    rng = random.Random(seed)
    grid = DRUM_STYLES[style]
    fill_voice = "snare" if "snare" in grid else "clap"
    notes = []
    for bar in range(bars):
        last_bar = bar == bars - 1
        for voice, steps in grid.items():
            if variation and voice not in _ANCHORS and bar % 2 == 1:
                steps = _vary(steps, variation, rng)
            if fill and last_bar and voice == fill_voice:
                steps = steps[:12] + "xxxx"
            for i, ch in enumerate(steps):
                if ch != "x":
                    continue
                start = bar * 4 + i * 0.25
                if i % 2 == 1:
                    start += swing * 0.125
                accent = 110 if i % 4 == 0 else 90
                if voice in ("closed_hat", "shaker"):
                    # Offbeat 8ths loudest, 16ths in between quieter: the classic hat "push".
                    accent = 92 if i % 4 == 2 else (60 if i % 2 else 75)
                elif voice == "rim":
                    accent = 80
                if fill and last_bar and voice == fill_voice and i >= 12:
                    accent = 70 + (i - 12) * 15
                notes.append({"pitch": DRUMS[voice], "start": round(start, 4), "duration": 0.25,
                              "velocity": max(1, min(127, accent + rng.randint(-6, 6)))})
    return notes


def bassline(root: str = "A", scale: str = "minor", bars: int = 2, octave: int = 1,
             style: str = "offbeat", progression: list[int] | None = None,
             seed: int | None = None) -> list[dict]:
    """Generate a bassline. progression: scale degrees (1-based) per bar, e.g. [1, 6, 4, 5]."""
    rng = random.Random(seed)
    pitches = scale_pitches(root, scale, octave, octaves=2)
    progression = progression or [1]
    notes = []
    for bar in range(bars):
        degree = progression[bar % len(progression)] - 1
        r = pitches[degree % len(SCALES[scale])]
        fifth = pitches[(degree + 4) % len(SCALES[scale])]
        t0 = bar * 4
        if style == "offbeat":
            for beat in range(4):
                notes.append({"pitch": r, "start": t0 + beat + 0.5, "duration": 0.4, "velocity": 105})
        elif style == "rolling":
            for i in range(16):
                if i % 4 == 0:
                    continue
                notes.append({"pitch": r + (12 if i % 8 == 7 else 0), "start": t0 + i * 0.25,
                              "duration": 0.22, "velocity": 95 + (10 if i % 2 else 0)})
        elif style == "root_eighths":
            for i in range(8):
                notes.append({"pitch": r, "start": t0 + i * 0.5, "duration": 0.45,
                              "velocity": 110 if i % 2 == 0 else 90})
        elif style == "walking":
            line = [r, pitches[(degree + 2) % len(SCALES[scale])], fifth,
                    pitches[(degree + 5) % len(SCALES[scale])]]
            for beat, p in enumerate(line):
                notes.append({"pitch": p, "start": t0 + beat, "duration": 0.9, "velocity": 100})
        elif style == "tech_house":
            # Stays off the kick (beats 1-4), rolls on 16ths, with octave and fifth pops.
            hits = [(0.5, r, 0.2, 110), (0.75, r, 0.15, 80), (1.5, r + 12, 0.15, 100),
                    (2.25, r, 0.15, 85), (2.5, r, 0.2, 110), (3.25, fifth, 0.15, 90),
                    (3.5, r, 0.2, 105)]
            for start, p, dur, vel in hits:
                if rng.random() < 0.15 and start not in (0.5, 2.5):
                    continue  # occasional gaps keep repeats from sounding looped
                notes.append({"pitch": p, "start": t0 + start, "duration": dur,
                              "velocity": max(1, min(127, vel + rng.randint(-6, 6)))})
        elif style == "syncopated":
            hits = [0, 0.75, 1.5, 2.5, 3.0, 3.5]
            for h in hits:
                p = fifth if rng.random() < 0.25 else r
                notes.append({"pitch": p, "start": t0 + h, "duration": 0.3,
                              "velocity": 100 + rng.randint(-8, 8)})
        else:
            raise ValueError("style must be offbeat, rolling, root_eighths, walking, tech_house or syncopated")
    return notes


CHORD_QUALITIES = {"maj": [0, 4, 7], "min": [0, 3, 7], "dim": [0, 3, 6], "aug": [0, 4, 8],
                   "maj7": [0, 4, 7, 11], "min7": [0, 3, 7, 10], "7": [0, 4, 7, 10],
                   "sus2": [0, 2, 7], "sus4": [0, 5, 7]}


# (start, duration) hits within one beat-group of 4 beats, used by the rhythmic chord modes.
CHORD_RHYTHMS = {
    "sustained": None,
    "stabs": [(0.5, 0.2), (1.5, 0.2), (2.5, 0.2), (3.5, 0.2)],
    "offbeat_stabs": [(0.5, 0.15), (1.25, 0.15), (2.5, 0.15), (3.0, 0.15), (3.75, 0.15)],
    "pulse": [(i * 0.5, 0.35) for i in range(8)],
}


def chord_progression(root: str = "C", scale: str = "minor", degrees: list[int] | None = None,
                      beats_per_chord: float = 4.0, octave: int = 3, sevenths: bool = False,
                      velocity: int = 90, rhythm: str = "sustained") -> list[dict]:
    """Diatonic chords stacked in thirds from the scale. degrees are 1-based.
    rhythm: sustained, stabs (offbeat 8ths), offbeat_stabs (syncopated, tech house style), pulse."""
    degrees = degrees or [1, 6, 3, 7]
    steps = SCALES[scale]
    if len(steps) != 7:
        raise ValueError("chord_progression needs a 7-note scale")
    if rhythm not in CHORD_RHYTHMS:
        raise ValueError(f"rhythm must be one of {sorted(CHORD_RHYTHMS)}")
    base = note_number(root, octave)
    notes = []
    for i, deg in enumerate(degrees):
        idx = deg - 1
        size = 4 if sevenths else 3
        pitches = [base + steps[(idx + 2 * k) % 7] + 12 * ((idx + 2 * k) // 7) for k in range(size)]
        t0 = i * beats_per_chord
        if CHORD_RHYTHMS[rhythm] is None:
            hits = [(0.0, beats_per_chord)]
        else:
            hits = [(b + 4 * g, d) for g in range(int(beats_per_chord // 4) or 1)
                    for b, d in CHORD_RHYTHMS[rhythm] if b + 4 * g < beats_per_chord]
        for start, dur in hits:
            for pitch in pitches:
                notes.append({"pitch": pitch, "start": t0 + start, "duration": dur, "velocity": velocity})
    return notes


# Live 12's scale names -> keys of SCALES (Live names not listed here have no generator support).
LIVE_SCALES = {"Major": "major", "Minor": "minor", "Dorian": "dorian", "Phrygian": "phrygian",
               "Lydian": "lydian", "Mixolydian": "mixolydian", "Harmonic Minor": "harmonic_minor",
               "Major Pentatonic": "pentatonic_major", "Minor Pentatonic": "pentatonic_minor",
               "Minor Blues": "blues"}
SCALE_TO_LIVE = {v: k for k, v in LIVE_SCALES.items()}
