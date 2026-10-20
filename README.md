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
   python -m venv .venv
   .venv/Scripts/pip install -e .        # macOS: .venv/bin/pip install -e .
   ```
2. **Install the Remote Script** into Live's User Library:
   ```sh
   python scripts/install.py
   ```
3. **Enable it in Live**: Settings → Link, Tempo & MIDI → Control Surface → **LivePilot**
   (Input and Output: None). The status bar shows `LivePilot: listening on 127.0.0.1:9877`.
4. **Connect an AI client.**
   - *Claude Code*: run `claude` in this folder. `.mcp.json` registers the server.
   - *Claude Desktop*: add this to `claude_desktop_config.json`:
     ```json
     {"mcpServers": {"livepilot": {
         "command": "C:/path/to/live-pilot/.venv/Scripts/python.exe",
         "args": ["-m", "livepilot"]}}}
     ```

Then try asking:
- "Make a 124 BPM house groove: drum rack with a 2-bar beat, an A minor offbeat bass on Operator, and chords."
- "Side-chain the bass to the kick, high-pass the hats at 300 Hz, and send the chords to a reverb return."
- "Build a 64-bar arrangement with locators for Intro, Build, Drop and Outro."

## Tools

55 MCP tools grouped into: session and transport, tracks and mixer, routing, clips, MIDI notes
(add/remove/quantize/humanize/transpose/velocity), generators (drums in 9 styles, basslines, diatonic
chords), scenes, devices and parameters, sidechain, browser search and loading, and arrangement
(locators, placing clips, arrangement clips). `raw_command` reaches any Remote Script command
directly (`list_commands` lists them).

## Development

```sh
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

The tests run the real Remote Script and socket protocol against a fake Live (`tests/fake_live.py`),
so no Live instance is needed. Remote Script errors are logged to Live's `Log.txt`.

After changing `remote_script/`, run `python scripts/install.py` again and reselect the control
surface (or restart Live).

## Roadmap

- [x] Bridge (Remote Script) and MCP server
- [ ] Audio analyser: band energy, brightness, rhythmic density, key, LUFS, reference matching, sample-fit scoring
- [ ] Mix and master helpers: gain staging to target levels, master chain, LUFS targeting
- [ ] Standalone desktop app with built-in chat (user's own Anthropic API key) and installers for both OSes
