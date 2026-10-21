"""Control surface: socket server + main-thread command dispatch.

Live's API may only be touched from Live's main thread. Socket threads put
commands on a queue; update_display() (called by Live roughly every 100 ms
on the main thread) drains the queue and runs each command. Commands that
must wait for Live to apply a change run across several ticks (see _dispatch).

Wire protocol: newline-delimited JSON.
  request:  {"id": 1, "type": "set_tempo", "params": {"bpm": 124}}
  response: {"id": 1, "status": "ok", "result": {...}}
            {"id": 1, "status": "error", "error": "..."}
"""
import json
import queue
import socket
import threading
import traceback
import types

from _Framework.ControlSurface import ControlSurface

from .handlers import Handlers

HOST = "127.0.0.1"
PORT = 9877
COMMAND_TIMEOUT = 20.0
MAX_COMMAND_TICKS = 50  # ~5 s at Live's ~10 Hz update rate


class LivePilot(ControlSurface):
    def __init__(self, c_instance):
        ControlSurface.__init__(self, c_instance)
        self._commands = queue.Queue()
        self._pending = None  # (msg, reply, generator, steps) of a multi-tick command
        self._handlers = Handlers(self)
        self._running = True
        self._server = None
        try:
            self._start_server()
            self.show_message("LivePilot: listening on %s:%d" % (HOST, PORT))
        except Exception:
            self.log_message("LivePilot: failed to start server\n" + traceback.format_exc())
            self.show_message("LivePilot: could not open port %d" % PORT)

    # ------------------------------------------------------------------ server

    def _start_server(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((HOST, PORT))
        server.listen(5)
        server.settimeout(1.0)
        self._server = server
        thread = threading.Thread(target=self._accept_loop, name="LivePilotAccept")
        thread.daemon = True
        thread.start()

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._client_loop, args=(conn,), name="LivePilotClient")
            thread.daemon = True
            thread.start()

    def _client_loop(self, conn):
        conn.settimeout(None)
        buf = b""
        try:
            while self._running:
                try:
                    data = conn.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._handle_line(line)
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_line(self, line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError as e:
            return {"id": None, "status": "error", "error": "invalid JSON: %s" % e}
        reply = queue.Queue(maxsize=1)
        self._commands.put((msg, reply))
        try:
            return reply.get(timeout=COMMAND_TIMEOUT)
        except queue.Empty:
            return {"id": msg.get("id"), "status": "error", "error": "timed out waiting for Live's main thread"}

    # ------------------------------------------------------------- main thread

    def update_display(self):
        ControlSurface.update_display(self)
        # A multi-tick command (see _dispatch) owns the main thread until it finishes, so
        # commands can't interleave with it, e.g. two locator commands moving the playhead.
        if self._pending is not None:
            if not self._advance():
                return
        while True:
            try:
                msg, reply = self._commands.get_nowait()
            except queue.Empty:
                break
            if self._dispatch(msg, reply):
                return  # started a multi-tick command; the rest of the queue waits

    def _dispatch(self, msg, reply):
        """Run a command and reply. A handler that is a generator runs one step per update tick,
        which lets it wait for Live to apply a change (Live applies some setters, like the
        playhead position, only on a later tick). Returns True if the command is still running."""
        try:
            result = self._handlers.run(msg.get("type"), msg.get("params") or {})
        except Exception as e:
            reply.put(self._error(msg, e))
            return False
        if isinstance(result, types.GeneratorType):
            self._pending = (msg, reply, result, 0)
            return not self._advance()
        reply.put({"id": msg.get("id"), "status": "ok", "result": result})
        return False

    def _advance(self):
        """Step the pending command once. Returns True when it has finished and replied."""
        msg, reply, gen, steps = self._pending
        try:
            if steps >= MAX_COMMAND_TICKS:
                raise RuntimeError("command did not finish within %d update ticks" % MAX_COMMAND_TICKS)
            next(gen)
        except StopIteration as done:
            response = {"id": msg.get("id"), "status": "ok", "result": done.value}
        except Exception as e:
            gen.close()
            response = self._error(msg, e)
        else:
            self._pending = (msg, reply, gen, steps + 1)
            return False
        self._pending = None
        reply.put(response)
        return True

    def _error(self, msg, e):
        self.log_message("LivePilot: error in %s\n%s" % (msg.get("type"), traceback.format_exc()))
        return {"id": msg.get("id"), "status": "error", "error": "%s: %s" % (type(e).__name__, e)}

    def disconnect(self):
        self._running = False
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        ControlSurface.disconnect(self)
