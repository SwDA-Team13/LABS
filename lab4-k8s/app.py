import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer


APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_COLOR = os.getenv("APP_COLOR", "blue")
PORT = int(os.getenv("PORT", "8080"))


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/":
            self._send_json(
                200,
                {
                    "version": APP_VERSION,
                    "color": APP_COLOR,
                    "hostname": socket.gethostname(),
                    "message": f"Hello from version {APP_VERSION}",
                },
            )
            return

        self._send_json(404, {"error": "not found"})

    def _send_json(self, status_code, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = HTTPServer(("", PORT), RequestHandler)
    print(
        f"Listening on :{PORT} version={APP_VERSION} color={APP_COLOR}",
        flush=True,
    )
    server.serve_forever()
