"""
HTTP reverse proxy for DashScope API.
Container can't do TLS to dashscope.aliyuncs.com (SNI blocked),
but host machine can. This proxy receives HTTP from container,
forwards as HTTPS to DashScope, returns response.

Usage:
  python dashscope_proxy.py

Then in docker-compose.yml, set:
  DASHSCOPE_BASE_URL=http://host.docker.internal:19200/v1
"""
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.request
import ssl

DASHSCOPE_HOST = "dashscope.aliyuncs.com"
DASHSCOPE_BASE = f"https://{DASHSCOPE_HOST}"
LISTEN_PORT = 19200


class ProxyHandler(BaseHTTPRequestHandler):
    def _proxy(self):
        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Build target URL
        target_url = f"{DASHSCOPE_BASE}{self.path}"

        # Build request
        req = urllib.request.Request(target_url, data=body, method=self.command)

        # Forward headers (except Host which urllib will set)
        for key, val in self.headers.items():
            if key.lower() in ("host", "content-length"):
                continue
            req.add_header(key, val)

        # Make request with TLS
        ctx = ssl.create_default_context()
        try:
            resp = urllib.request.urlopen(req, context=ctx, timeout=120)
            status = resp.status
            resp_body = resp.read()
            resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            resp_body = e.read()
            resp_headers = e.headers
        except Exception as e:
            status = 502
            resp_body = json.dumps({"error": {"message": str(e)}}).encode()
            resp_headers = {}

        # Send response
        self.send_response(status)
        for key, val in resp_headers.items():
            if key.lower() in ("transfer-encoding", "connection"):
                continue
            self.send_header(key, val)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_PATCH = _proxy

    def log_message(self, format, *args):
        print(f"[PROXY] {self.client_address[0]} - {format % args}", flush=True)


def main():
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    print(f"DashScope reverse proxy on http://0.0.0.0:{LISTEN_PORT} -> {DASHSCOPE_BASE}", flush=True)
    print(f"Container should use base_url: http://host.docker.internal:{LISTEN_PORT}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
