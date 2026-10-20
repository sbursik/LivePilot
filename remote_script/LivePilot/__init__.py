"""LivePilot - Ableton Live MIDI Remote Script.

Exposes Live's Python API over a local TCP socket so an AI agent (via the
LivePilot MCP server) can drive the session.
"""
from .control_surface import LivePilot


def create_instance(c_instance):
    return LivePilot(c_instance)
