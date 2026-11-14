# LivePilot

Direct Ableton Live in plain language. An AI agent (Claude Desktop, Claude Code, or any MCP client)
creates tracks, writes MIDI, loads devices, mixes, and arranges your set through a bridge running
inside Live.

```
 AI client ──MCP (stdio)──▶ livepilot server ──TCP 127.0.0.1:9877──▶ LivePilot Remote Script ──▶ Live API
 (Claude)                   server/livepilot/                         remote_script/LivePilot/
```

Works on Windows and macOS with Live 11 and 12. (Arrangement clip creation needs Live 12.1+.)

## Setup

1. **Install the server** (Python 3.10+):
   ```sh
   # Windows
   python -m venv .venv
   .venv/Scripts/pip install -e ".[dev]"

   # macOS (if python3 is missing: brew install python, or use python.org's installer)
   python3 -m venv .venv
   .venv/bin/pip install -e ".[dev]"
   ```
2. **Install the Remote Script** into Live's User Library. This also writes `.mcp.json` with
   this machine's Python path, which is why that file isn't in git:
   ```sh
   .venv/Scripts/python scripts/install.py   # macOS: .venv/bin/python scripts/install.py
   ```
   The User Library is `Documents\Ableton\User Library` on Windows and `~/Music/Ableton/User Library`
   on macOS. If you moved it, pass `--user-library <path>`.
3. **Enable it in Live**: Settings → Link, Tempo & MIDI → Control Surface → **LivePilot**
   (Input and Output: None). The status bar shows `LivePilot: listening on 127.0.0.1:9877`.
4. **Connect an AI client.**
   - *Claude Code*: run `claude` in this folder. `.mcp.json` registers the server.
   - *Claude Desktop*: add this to `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`,
     macOS: `~/Library/Application Support/Claude/`), using the path `install.py` printed:
     ```json
     {"mcpServers": {"livepilot": {
         "command": "/path/to/live-pilot/.venv/bin/python",
         "args": ["-m", "livepilot"]}}}
     ```

Then try asking:
- "Make a 124 BPM house groove: drum rack with a 2-bar beat, an A minor offbeat bass on Operator, and chords."
- "Side-chain the bass to the kick, high-pass the hats at 300 Hz, and send the chords to a reverb return."
- "Build a 64-bar arrangement with locators for Intro, Build, Drop and Outro."

## Tools

63 MCP tools grouped into: session and transport, tracks and mixer, routing, clips, MIDI notes
(add/remove/quantize/humanize/transpose/velocity), generators (drums in 9 styles, basslines, diatonic
chords), scenes, devices and parameters, sidechain, browser search and loading, and arrangement
(locators, placing and editing arrangement clips), and the audio analyser below. `raw_command` reaches any Remote Script command
directly (`list_commands` lists them).

## Audio analyser

The AI can't hear, so these tools measure audio and give it numbers to reason with:

| Tool | What it does |
|---|---|
| `analyze_clip` | Analyses an audio clip in the set (Session or Arrangement), limited to the part the clip plays when warped |
| `analyze_audio_file` | Analyses any wav/aif/flac/mp3/ogg file: mixdowns, stems, references, samples |
| `compare_to_reference` | Compares a mix with a reference: tonal balance after loudness matching, loudness, dynamics, width, low-end mono and key, with suggestions |
| `analyze_frequency_space` | Shows which bands the set's audio fills, which are open, and which parts crowd each band |
| `score_samples` | Ranks samples (files or a folder) by how well they fill open bands and fit the song key |
| `warp_to_grid` | Tracks every beat and the downbeat of stems whose tempo drifts (e.g. Suno exports) and warps each clip with a marker on every bar, so the music's bars sit on Live's grid |

Measurements: integrated, short-term and momentary loudness (LUFS, per ITU-R BS.1770-4),
loudness range, true peak, crest factor, energy per band and octave, spectral centroid and
brightness, stereo correlation, width and low-end mono, tempo, onset density, key with Camelot
compatibility, and energy over time.

The analyser reads audio files, so MIDI and instrument tracks (e.g. Serum) aren't measured until
you bounce or export them. The frequency-space tools say which tracks they couldn't include.

## Mix and master

| Tool | What it does |
|---|---|
| `gain_stage` | Measures each audio track's clips and works out (or sets) the fader that brings it to a target loudness, as a starting balance |
| `build_master_chain` | Adds Glue Compressor and Limiter to the master, with the ceiling at the target's true-peak limit |
| `match_loudness` | Measures an exported mixdown and adjusts the master Limiter's gain (or a Utility before a plugin like Ozone) to hit a target: streaming −14, Apple −16, club −8 or broadcast −23 LUFS |
| `set_device_parameter` | Takes values with units (`"350 Hz"`, `"-18 dB"`, `"150 ms"`) as well as raw values |
| `move_device` | Reorders a chain; the load tools also take a `position` |
| `record_automation` | Writes Arrangement automation (sweeps, builds, fades, send throws) by recording it in real time, several parameters in one pass |
| `warp_to_grid` | Tracks the beats of stems whose tempo drifts (e.g. AI-generated songs) and warps each clip bar by bar onto Live's grid |

Live's script API can't render or export audio, so loudness is checked from a mixdown you
export: export, run `match_loudness`, and repeat until it reports on target.

## Development

```sh
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

The tests run the real Remote Script and socket protocol against a fake Live (`tests/fake_live.py`),
so no Live instance is needed. Remote Script errors are logged to Live's `Log.txt`.

After changing `remote_script/`, run `python scripts/install.py` again and reselect the control
surface (or restart Live).

## macOS checklist

Now verified on a Mac:

1. Set up as above, run `pytest` (the tests don't need Live).
2. Run `install.py` and confirm the script lands in `~/Music/Ableton/User Library/Remote Scripts/LivePilot`.
3. Select LivePilot as a control surface; check the status bar message.
4. Run `claude` in the repo, ask it to get the session info, then create a track, a clip and some notes.
5. If anything fails, check `~/Library/Preferences/Ableton/Live 12.x/Log.txt` for `LivePilot` errors.

## Roadmap

- [x] Bridge (Remote Script) and MCP server
- [x] Audio analyser: band energy, brightness, rhythmic density, key, LUFS, reference matching, sample-fit scoring
- [x] Mix and master helpers: gain staging to target levels, master chain, LUFS targeting
- [ ] Standalone desktop app with built-in chat (user's own Anthropic API key) and installers for both OSes
