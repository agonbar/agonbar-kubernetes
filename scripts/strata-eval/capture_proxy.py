"""A recording HTTP proxy between an agent harness and the model server.

run_agent.py puts one in front of Strata for every task so that every harness is measured
the same way, whatever its own log format: one JSON line per model call with the request
body as sent, the usage the server reported (prompt, cached, completion tokens), the tool
calls the model answered with, the finish reason and the timing.

It changes one thing in the traffic: a streamed /v1/chat/completions request gets
stream_options.include_usage, so the last chunk carries the token counts. Calls are
serialised (Strata serves one at a time anyway), so the timing of a call is its own.
After max_calls model calls it answers 429, a uniform stop for harnesses stuck in a loop.

  p = CaptureProxy("http://127.0.0.1:18092", "/tmp/x/task.calls.jsonl", max_calls=80)
  p.start(); ... harness talks to http://127.0.0.1:%d % p.port ...; p.stop()
"""
import http.client, json, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATHS = ("/chat/completions", "/messages", "/responses")
HOP = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive"}


def _usage(obj, acc):
    """Fold every usage block of an OpenAI chat, Anthropic or Responses payload into acc."""
    for u in (obj.get("usage"), (obj.get("message") or {}).get("usage"), (obj.get("response") or {}).get("usage")):
        if isinstance(u, dict):
            acc.update({k: v for k, v in u.items() if v is not None})
    if isinstance(obj.get("timings"), dict):
        acc["timings"] = obj["timings"]


def _scan(obj, out):
    """Tool call names and finish reason from one response object or stream event."""
    _usage(obj, out["usage"])
    for ch in obj.get("choices") or []:
        for tc in (ch.get("delta") or ch.get("message") or {}).get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name")
            if name:
                out["toolCalls"].append(name)
        if ch.get("finish_reason"):
            out["finish"] = ch["finish_reason"]
    if obj.get("type") == "content_block_start" and obj.get("content_block", {}).get("type") == "tool_use":
        out["toolCalls"].append(obj["content_block"].get("name"))
    if obj.get("type") == "message" and isinstance(obj.get("content"), list):  # non-streamed Anthropic
        out["toolCalls"] += [b.get("name") for b in obj["content"] if b.get("type") == "tool_use"]
        out["finish"] = obj.get("stop_reason")
    if obj.get("type") == "message_delta":
        out["finish"] = (obj.get("delta") or {}).get("stop_reason")
    resp = obj.get("response") if obj.get("type") == "response.completed" else (obj if obj.get("object") == "response" else None)
    if resp:
        out["toolCalls"] += [i.get("name") for i in resp.get("output") or [] if i.get("type") in ("function_call", "custom_tool_call")]
        out["finish"] = resp.get("status")


class CaptureProxy:
    def __init__(self, upstream, log_path, max_calls=80):
        self.up = urllib.parse.urlsplit(upstream)
        self.log_path, self.max_calls = log_path, max_calls
        self.calls, self.lock, self.log_lock = 0, threading.Lock(), threading.Lock()
        proxy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                proxy.forward(self, None)

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                proxy.forward(self, self.rfile.read(n))

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]

    def start(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def forward(self, h, body):
        path = urllib.parse.urlsplit(h.path).path
        is_model = body is not None and path.endswith(MODEL_PATHS)
        req = None
        if is_model:
            try:
                req = json.loads(body)
            except ValueError:
                req = None
            if isinstance(req, dict) and path.endswith("/chat/completions") and req.get("stream"):
                req.setdefault("stream_options", {})["include_usage"] = True
                body = json.dumps(req).encode()
            with self.lock:
                self.calls += 1
                if self.calls > self.max_calls:
                    msg = json.dumps({"error": {"type": "rate_limit_error", "message": "capture proxy: more than %d model calls" % self.max_calls}}).encode()
                    h.send_response(429)
                    h.send_header("content-type", "application/json")
                    h.send_header("content-length", str(len(msg)))
                    h.end_headers()
                    h.wfile.write(msg)
                    self._log({"t": time.time(), "path": path, "status": 429, "refused": True})
                    return
                self._relay(h, path, body, req)
        else:
            self._relay(h, path, body, None)

    def _relay(self, h, path, body, req):
        hdrs = {k: v for k, v in h.headers.items() if k.lower() not in HOP}
        if body is not None:
            hdrs["content-length"] = str(len(body))
        conn = http.client.HTTPConnection(self.up.hostname, self.up.port, timeout=3600)
        t0 = time.time()
        rec = {"t": t0, "path": path, "req": req, "usage": {}, "toolCalls": [], "finish": None}
        try:
            conn.request(h.command, self.up.path.rstrip("/") + h.path, body=body, headers=hdrs)
            r = conn.getresponse()
            h.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP:
                    h.send_header(k, v)
            h.send_header("connection", "close")
            h.end_headers()
            buf, first = bytearray(), None
            while True:
                chunk = r.read1(65536)
                if not chunk:
                    break
                first = first or time.time()
                buf += chunk
                h.wfile.write(chunk)
                h.wfile.flush()
            rec["status"] = r.status
        except Exception as e:  # the harness sees a dropped connection, the log keeps why
            rec["status"], rec["proxyError"] = None, repr(e)[:300]
            buf, first = bytearray(), None
        finally:
            conn.close()
        if req is None:
            return
        rec["ttfbSec"] = round((first or time.time()) - t0, 2)
        rec["sec"] = round(time.time() - t0, 2)
        text = buf.decode("utf-8", "replace")
        if rec.get("status") != 200:
            rec["errorBody"] = text[:400]
        for line in text.splitlines() if "data:" in text[:200] or text.startswith("event:") else [text]:
            line = line[5:].strip() if line.startswith("data:") else line.strip()
            if line.startswith("{"):
                try:
                    _scan(json.loads(line), rec)
                except ValueError:
                    pass
        self._log(rec)

    def _log(self, rec):
        with self.log_lock, open(self.log_path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
