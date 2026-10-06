import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

_STARTUP_GRACE_SECONDS = 30


def _make_handler(get_slack_handler: Callable, startup_time: float) -> type:
    import time

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._handle_health()
            else:
                self.send_response(404)
                self.end_headers()

        def _handle_health(self):
            in_startup = (time.monotonic() - startup_time) < _STARTUP_GRACE_SECONDS

            slack_connected = False
            handler = get_slack_handler()
            if handler is not None:
                try:
                    slack_connected = bool(handler.client.is_connected)
                except Exception:
                    pass

            if in_startup or slack_connected:
                code = 200
                status = "starting" if (in_startup and not slack_connected) else "ok"
            else:
                code = 503
                status = "degraded"

            body = json.dumps({
                "status": status,
                "slack_connected": slack_connected,
            }).encode()

            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # suppress per-request access logs

    return HealthHandler


def start_health_server(get_slack_handler: Callable, port: int = 8080) -> None:
    import time
    startup_time = time.monotonic()
    handler_class = _make_handler(get_slack_handler, startup_time)
    server = HTTPServer(("0.0.0.0", port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
