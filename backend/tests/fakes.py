"""Local fake Mindler API and fake SWAYAM site so failure scenarios run offline and deterministically."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Server:
    def __init__(self, handler):
        self.mode = "ok"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.owner = self
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.srv.shutdown()


# --------------------------------------------------------------------------- Mindler
DOMAINS = [("Engineering", "engineering"), ("Medical", "medical"), ("Bad Domain", "bad")]


class _MindlerHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass

    def do_GET(self):
        owner = self.server.owner
        if owner.mode == "list_garbage":
            return self._send(200, "<html>nope</html>", "text/html")
        if owner.mode == "list_badshape":
            return self._send(200, json.dumps({"data": "oops"}))
        n = getattr(owner, "domains", len(DOMAINS))
        items = [{"_source": {"_id": i, "career_domain_name": DOMAINS[i % 3][0] + ("" if i < 3 else f" {i}"),
                              "tagline": DOMAINS[i % 3][1] + ("" if i < 3 else f"-{i}"), "image": "x.png",
                              "description": "<p>About</p>"}} for i in range(n)]
        self._send(200, json.dumps({"data": items}))

    def do_POST(self):
        owner = self.server.owner
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        tagline = body.get("tagline", "")
        mode = owner.mode
        if mode == "429":
            return self._send(429, "{}")
        if mode == "timeout":
            time.sleep(4)
            return self._send(200, "{}")
        if mode == "garbage":
            return self._send(200, "<html>not json</html>", "text/html")
        if mode == "slow":
            time.sleep(1.0)
        if mode == "badshape" and tagline == "bad":
            return self._send(200, json.dumps({"data": "oops"}))
        details = [{"id": 1, "career_id": f"{tagline}-1", "career_name": f"{tagline} career"},
                   {"id": 2, "career_id": f"{tagline}-1", "career_name": "dup of first"}]
        self._send(200, json.dumps({"data": [{"_source": {"career_details": details}}]}))


def mindler_server(domains: int = 3) -> _Server:
    s = _Server(_MindlerHandler)
    s.domains = domains
    return s


# --------------------------------------------------------------------------- SWAYAM
def _detail(n: int, blank: bool) -> str:
    title = "&nbsp;" if blank else f"Course {n}"
    return f"""<html><body>
<h1>{title}</h1>
<aside>By Prof X<br>|<br>Test University<br>Learners enrolled: 1,234</aside>
<main class="flex-col">
  <button>Course Information</button><button>Summary</button>
  <div>Intended audience: UG students</div>
  <div>Start Date</div><div>01 Jan 2026</div>
</main></body></html>"""


class _SwayamHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        owner = self.server.owner
        try:
            if self.path.startswith("/explorer"):
                if owner.mode == "no_cards":
                    html = "<html><body><p>nothing here</p></body></html>"
                else:
                    cards = "".join(f'<div class="col-md-4"><a href="{owner.base}/c/{n}/preview">'
                                    f"<h4>Course {n}</h4></a><div>Inst</div><div>NPTEL</div>"
                                    f"<div>4 Weeks</div></div>" for n in range(1, 4))
                    html = ("<html><body><ul><li><a>Upcoming (Enrollment Open)</a></li>"
                            "<li><a>Ongoing (Enrollment Closed)</a></li></ul>"
                            f'<div class="course-list">{cards}</div></body></html>')
            else:
                n = int(self.path.split("/")[2])
                if n in owner.hang:
                    time.sleep(6)
                if owner.slow:
                    time.sleep(owner.slow)
                html = _detail(n, blank=n in owner.blank)
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass


def swayam_server() -> _Server:
    s = _Server(_SwayamHandler)
    s.hang, s.blank, s.slow = set(), set(), 0
    return s
