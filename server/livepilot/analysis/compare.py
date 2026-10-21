"""Comparisons built on features.analyze: reference matching, frequency space, sample fit."""
from __future__ import annotations

import numpy as np

from .features import BANDS, NOTE_NAMES, OCTAVE_CENTERS, _r

_BAND_NAMES = [b[0] for b in BANDS]


# ----------------------------------------------------------------- keys

def camelot(root_note: int, mode: str) -> tuple[int, str]:
    """Camelot wheel position: C major = 8B, A minor = 8A."""
    if mode == "major":
        n = (8 + 7 * root_note) % 12
        letter = "B"
    else:
        n = (8 + 7 * (root_note - 9)) % 12
        letter = "A"
    return (n or 12), letter


def key_compatibility(a: tuple[int, str] | None, b: tuple[int, str] | None) -> tuple[float | None, str]:
    """1.0 same key, 0.8 relative or neighbouring key on the Camelot wheel, 0.4 two steps,
    0.0 clashing. None when either key is unknown."""
    if a is None or b is None:
        return None, "key unknown"
    (na, la), (nb, lb) = camelot(*a), camelot(*b)
    step = min((na - nb) % 12, (nb - na) % 12)
    if step == 0 and la == lb:
        return 1.0, "same key"
    if step == 0:
        return 0.8, "relative major/minor"
    if step == 1 and la == lb:
        return 0.8, "neighbouring key (a fifth apart)"
    if step <= 2:
        return 0.4, "loosely related key"
    return 0.0, "clashing key"


def key_name(root_note: int, mode: str) -> str:
    n, letter = camelot(root_note, mode)
    return f"{NOTE_NAMES[root_note]} {mode} ({n}{letter})"


# ------------------------------------------------------------ reference

def compare_to_reference(mix: dict, ref: dict) -> dict:
    """Differences that matter when matching a mix to a reference, with suggestions.
    Tonal balance is compared after loudness normalisation (each profile is relative to its
    own total), so a quieter mix isn't read as 'lacking every band'."""
    out: dict = {"mix": mix["file"], "reference": ref["file"]}
    suggestions = []

    mo, ro = mix["spectrum"]["_octaves"], ref["spectrum"]["_octaves"]
    diffs = mo - ro
    out["tonal_balance_diff_db"] = {str(c): _r(d) for c, d in zip(OCTAVE_CENTERS, diffs)}
    for c, d in zip(OCTAVE_CENTERS, diffs):
        if abs(d) >= 2.0:
            verb = "cut" if d > 0 else "boost"
            more = "more" if d > 0 else "less"
            suggestions.append(f"{c:g} Hz octave has {abs(d):.1f} dB {more} than the reference: "
                               f"consider a gentle {verb} of about {abs(d) / 2:.1f}-{abs(d):.1f} dB there.")
    out["tonal_match_pct"] = _r(max(0.0, 100 - 10 * float(np.mean(np.abs(diffs)))), 0)

    ml, rl = mix.get("loudness", {}), ref.get("loudness", {})
    lufs = (ml.get("integrated_lufs"), rl.get("integrated_lufs"))
    if None not in lufs:
        d = lufs[0] - lufs[1]
        out["loudness_diff_lu"] = _r(d)
        if abs(d) >= 1:
            suggestions.append(f"Mix is {abs(d):.1f} LU {'louder' if d > 0 else 'quieter'} than the reference "
                               f"({lufs[0]} vs {lufs[1]} LUFS). Match loudness before judging tone.")
    for name, label in (("crest_db", "Crest factor"), ("loudness_range_lu", "Loudness range")):
        a, b = ml.get(name), rl.get(name)
        if a is not None and b is not None:
            out[f"{name}_diff"] = _r(a - b)
    if ml.get("crest_db") is not None and rl.get("crest_db") is not None:
        d = ml["crest_db"] - rl["crest_db"]
        if d > 3:
            suggestions.append(f"Mix is {d:.1f} dB more dynamic (crest) than the reference: it may need more "
                               "bus compression or limiting to compete.")
        elif d < -3:
            suggestions.append(f"Mix is {-d:.1f} dB less dynamic than the reference: it may be over-compressed.")
    tp = ml.get("true_peak_dbtp")
    if tp is not None and tp > -1:
        suggestions.append(f"Mix true peak is {tp} dBTP; keep the limiter ceiling at -1 dBTP or lower.")

    ms, rs = mix.get("stereo", {}), ref.get("stereo", {})
    if ms.get("channels") == 2 and rs.get("channels") == 2:
        out["correlation"] = {"mix": ms["correlation"], "reference": rs["correlation"]}
        out["side_to_mid_db"] = {"mix": ms["side_to_mid_db"], "reference": rs["side_to_mid_db"]}
        d = ms["side_to_mid_db"] - rs["side_to_mid_db"]
        if abs(d) >= 3:
            suggestions.append(f"Mix is {'wider' if d > 0 else 'narrower'} than the reference "
                               f"({abs(d):.1f} dB side-to-mid difference).")
        if ms["low_end_correlation"] < 0.8 <= rs["low_end_correlation"]:
            suggestions.append("Reference keeps the low end mono but the mix doesn't: mono the bass below ~120 Hz.")

    mc, rc = mix["spectrum"]["centroid_hz"], ref["spectrum"]["centroid_hz"]
    out["centroid_hz"] = {"mix": mc, "reference": rc}

    mk, rk = mix.get("key", {}), ref.get("key", {})
    if mk.get("key") and rk.get("key"):
        score, why = key_compatibility((mk["root_note"], mk["mode"]), (rk["root_note"], rk["mode"]))
        out["keys"] = {"mix": mk["key"], "reference": rk["key"], "relation": why}

    out["suggestions"] = suggestions or ["Close to the reference on all measured points."]
    return out


# -------------------------------------------------------- frequency space

def frequency_space(contexts: list[dict]) -> dict:
    """How the given files share out the spectrum. Each file counts equally (clip gains and
    faders aren't known from files), so this shows which bands parts occupy, not final levels."""
    usable = [c for c in contexts if "spectrum" in c]
    if not usable:
        return {"files": 0, "bands": [], "open_bands": []}
    total = np.sum([c["spectrum"]["_shares"] for c in usable], axis=0)
    occupancy = total / max(total.max(), 1e-12)
    bands = []
    for (name, lo, hi), occ in zip(BANDS, occupancy):
        owners = sorted(usable, key=lambda c: -c["spectrum"]["_shares"][_BAND_NAMES.index(name)])[:3]
        bands.append({"band": name, "range_hz": [lo, hi], "occupancy": _r(occ, 2),
                      "main_sources": [o.get("label", o["file"]) for o in owners
                                       if o["spectrum"]["_shares"][_BAND_NAMES.index(name)] > 0.15]})
    open_bands = [b["band"] for b in bands if b["occupancy"] < 0.25]
    crowded = [b["band"] for b in bands if b["occupancy"] > 0.75 and len(b["main_sources"]) >= 2]
    return {"files": len(usable), "bands": bands, "open_bands": open_bands, "crowded_bands": crowded,
            "_occupancy": occupancy}


def score_sample(candidate: dict, space: dict, song_key: tuple[int, str] | None) -> dict:
    """0-100: how much of the candidate's energy lands where the context leaves room, adjusted for
    key compatibility when the candidate is tonal."""
    shares = candidate["spectrum"]["_shares"]
    occupancy = space.get("_occupancy")
    if occupancy is None:
        occupancy = np.zeros(len(BANDS))
    fill = float(np.sum(shares * (1 - occupancy)))
    clash = float(np.sum(shares * occupancy))
    score = 100 * fill
    reasons = []
    main = int(np.argmax(shares))
    reasons.append(f"main energy in {BANDS[main][0]} ({BANDS[main][1]}-{BANDS[main][2]} Hz), "
                   f"where the mix occupancy is {occupancy[main]:.2f}")

    k = candidate.get("key", {})
    compat, why = (None, "atonal")
    if k.get("key"):
        compat, why = key_compatibility((k["root_note"], k["mode"]), song_key)
        if compat is not None:
            score *= 0.5 + 0.5 * compat
            reasons.append(f"{k['key']} vs song key: {why}")
    return {"file": candidate["file"], "score": _r(score, 0), "fill": _r(fill, 2), "clash": _r(clash, 2),
            "key": k.get("key"), "key_relation": why,
            "density": candidate.get("rhythm", {}).get("density"), "reasons": reasons}
