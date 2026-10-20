"""TCP client for the LivePilot Remote Script running inside Ableton Live."""
import itertools
import json
import os
import socket
import threading

HOST = os.environ.get("LIVEPILOT_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVEPILOT_PORT", "9877"))


class LiveError(RuntimeError):
    """Live (or the Remote Script) rejected a command."""


class LiveConnection:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 25.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        if self._sock is None:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except OSError as e:
                raise LiveError(
                    f"Cannot reach Ableton Live on {self.host}:{self.port}. Is Live open with the "
                    "'LivePilot' control surface selected in Settings > Link, Tempo & MIDI?"
                ) from e
            self._buf = b""
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def send(self, command: str, **params):
        with self._lock:
            for attempt in (1, 2):
                sock = self._connect()
                cmd_id = next(self._ids)
                payload = json.dumps({"id": cmd_id, "type": command, "params": params}) + "\n"
                try:
                    sock.sendall(payload.encode("utf-8"))
                    response = self._read_line(sock)
                    break
                except (OSError, ConnectionError):
                    # Live may have reloaded the script; reconnect once.
                    self.close()
                    if attempt == 2:
                        raise LiveError("Lost connection to Ableton Live")
            msg = json.loads(response)
            if msg.get("status") != "ok":
                raise LiveError(msg.get("error", "unknown error"))
            return msg.get("result")

    def _read_line(self, sock: socket.socket) -> bytes:
        while b"\n" not in self._buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line
