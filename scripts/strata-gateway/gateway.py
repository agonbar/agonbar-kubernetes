"""strata-gateway: a priority queue in front of Strata, which runs one generation at a time.

Sidecar in Strata's pod. It listens where Strata used to (8080, the Service's port) and forwards to Strata on
UPSTREAM. Generation requests (POST to GENERATE paths) wait their turn here with at most one in flight upstream;
everything else (health, models, status, the web UI) passes straight through.

Priority, highest first: hot, batch, research, junior. A client says which with the header `X-Strata-Priority`, or
with an API key `prio-<name>` (Bearer or x-api-key) when it can only set a key. Missing or unknown: hot.

Who goes next when the turn frees up:
  1. the highest priority with someone waiting;
  2. within it, the same conversation as the request that just ended, so Strata's prefix cache stays warm across an
     agent's turns. A conversation is the hash of its system prompt and first message, so no client has to send an id;
  3. otherwise first come, first served.
After a request ends the turn is held HOLD_S for that conversation's next turn (an agent runs its tools in between),
unless someone of higher priority is waiting. STREAK_MAX consecutive turns of one conversation end its preference, so
equals still get through.

Streaming is passed through chunk by chunk. A streamed request that waits more than BEAT_S gets its 200 and SSE headers
and a `: queued` comment every BEAT_S, as Strata itself does while queued, so idle timeouts on the client do not fire;
an error from Strata after that is sent as an SSE error event. A client that hangs up leaves the queue, or, in flight,
closes the upstream connection, which Strata takes as a cancel.

GET /gateway/status: the queue by priority and what is in flight.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import os
import time

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, TCPConnector, web

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8081")
PORT = int(os.environ.get("PORT", "8080"))
HOLD_S = float(os.environ.get("HOLD_S", "3"))
STREAK_MAX = int(os.environ.get("STREAK_MAX", "8"))
BEAT_S = float(os.environ.get("BEAT_S", "10"))

PRIORITIES = ("hot", "batch", "research", "junior")
RANK = {name: i for i, name in enumerate(PRIORITIES)}
GENERATE = {"/v1/chat/completions", "/v1/completions", "/v1/messages", "/v1/responses", "/v1/images/generations"}
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-connection",
       "content-length", "host"}

log = logging.getLogger("strata-gateway")


def priority_of(headers) -> str:
    name = headers.get("X-Strata-Priority", "").strip().lower()
    if not name:
        key = headers.get("x-api-key") or headers.get("Authorization", "").removeprefix("Bearer ").strip()
        name = key.removeprefix("prio-") if key.startswith("prio-") else ""
    return name if name in RANK else "hot"


def conversation_of(body: dict) -> str:
    """Hash of what every turn of one conversation starts with (OpenAI chat, Anthropic messages, Responses)."""
    head = [body.get("system"), body.get("instructions")]
    for field in ("messages", "input"):
        if isinstance(body.get(field), list):
            head += body[field][:2]
    if isinstance(body.get("prompt"), str):
        head.append(body["prompt"][:4096])
    return hashlib.sha1(json.dumps(head, sort_keys=True, default=str).encode()).hexdigest()[:10]


class Waiter:
    def __init__(self, seq: int, prio: str, conv: str, path: str):
        self.seq, self.prio, self.rank, self.conv, self.path = seq, prio, RANK[prio], conv, path
        self.since = time.monotonic()
        self.granted: asyncio.Future = asyncio.get_running_loop().create_future()


class Scheduler:
    def __init__(self, hold_s: float = HOLD_S, streak_max: int = STREAK_MAX):
        self.hold_s, self.streak_max = hold_s, streak_max
        self.waiting: list[Waiter] = []
        self.busy: Waiter | None = None
        self.started = 0.0
        self.last: Waiter | None = None
        self.hold_until = 0.0
        self.streak = 0
        self.timer: asyncio.TimerHandle | None = None
        self.served = {p: 0 for p in PRIORITIES}
        self.seq = itertools.count()

    def new_waiter(self, prio: str, conv: str, path: str) -> Waiter:
        w = Waiter(next(self.seq), prio, conv, path)
        self.waiting.append(w)
        self._dispatch()
        return w

    def leave(self, w: Waiter):
        """The waiter is done: release the turn if it had it, or drop it from the queue."""
        if self.busy is w:
            self.busy, self.last = None, w
            self.hold_until = time.monotonic() + self.hold_s
            log.info("done  %-8s conv=%s %s in %.1fs", w.prio, w.conv, w.path, time.monotonic() - self.started)
        elif w in self.waiting:
            self.waiting.remove(w)
            log.info("gone  %-8s conv=%s left the queue after %.1fs", w.prio, w.conv, time.monotonic() - w.since)
        self._dispatch()

    def _pick(self, now: float) -> Waiter | None:
        top = min(w.rank for w in self.waiting)
        equals = [w for w in self.waiting if w.rank == top]          # in arrival order
        if self.last is not None and self.streak < self.streak_max:
            same = [w for w in equals if w.conv == self.last.conv]
            if same:
                return same[0]
            if now < self.hold_until and top >= self.last.rank:      # its next turn may be a moment away
                return None
        return equals[0]

    def _dispatch(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.busy is not None or not self.waiting:
            return
        now = time.monotonic()
        w = self._pick(now)
        if w is None:
            self.timer = asyncio.get_running_loop().call_at(self.hold_until, self._dispatch)
            return
        self.waiting.remove(w)
        self.streak = self.streak + 1 if self.last is not None and w.conv == self.last.conv else 1
        self.busy, self.started = w, now
        self.served[w.prio] += 1
        log.info("start %-8s conv=%s %s after %.1fs queued (waiting: %s)", w.prio, w.conv, w.path,
                 now - w.since, self.counts())
        w.granted.set_result(None)

    def counts(self) -> dict:
        return {p: sum(w.prio == p for w in self.waiting) for p in PRIORITIES}

    def status(self) -> dict:
        now = time.monotonic()
        b = self.busy
        return {
            "in_flight": b and {"priority": b.prio, "conversation": b.conv, "path": b.path,
                                "running_s": round(now - self.started, 1)},
            "queued": self.counts(),
            "waiting": [{"priority": w.prio, "conversation": w.conv, "path": w.path,
                         "waited_s": round(now - w.since, 1)} for w in self.waiting],
            "holding_for": (self.last.conv if self.last and now < self.hold_until and self.waiting else None),
            "served": self.served,
        }


def upstream_headers(request: web.Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "x-strata-priority"}


def unavailable(e: Exception) -> dict:
    return {"error": {"type": "unavailable", "message": f"strata is not answering: {e}"}}


def sse_error(path: str, status: int, body: bytes) -> bytes:
    try:
        err = json.loads(body)
    except ValueError:
        err = {"error": {"type": "api_error", "message": body.decode(errors="replace")[:2000]}}
    if path == "/v1/messages":
        err.setdefault("type", "error")
    data = json.dumps(err)
    if path in ("/v1/messages", "/v1/responses"):
        return f"event: error\ndata: {data}\n\n".encode()
    return f"data: {data}\n\n".encode()


async def relay(request: web.Request, up, resp: web.StreamResponse | None) -> web.StreamResponse:
    """Copy Strata's answer to the client as it arrives. `resp` is set when the SSE headers already went out."""
    if resp is None:
        resp = web.StreamResponse(status=up.status, reason=up.reason)
        for k, v in up.headers.items():
            if k.lower() not in HOP:
                resp.headers.add(k, v)
        await resp.prepare(request)
    elif up.status != 200:
        await resp.write(sse_error(request.path, up.status, await up.read()))
        return resp
    async for chunk in up.content.iter_any():
        await resp.write(chunk)
    await resp.write_eof()
    return resp


async def handle(request: web.Request) -> web.StreamResponse:
    app = request.app
    session: ClientSession = app["session"]
    sched: Scheduler = app["sched"]
    body = await request.read()
    url = UPSTREAM + request.rel_url.path_qs
    headers = upstream_headers(request)

    if request.method != "POST" or request.path not in GENERATE:
        try:
            up = await session.request(request.method, url, headers=headers, data=body or None)
        except ClientConnectionError as e:          # Strata restarting, or still loading the model
            return web.json_response(unavailable(e), status=503)
        async with up:
            return await relay(request, up, None)

    try:
        parsed = json.loads(body) if body else {}
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    w = sched.new_waiter(priority_of(request.headers), conversation_of(parsed), request.path)
    resp = None
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(w.granted), BEAT_S)
                break
            except asyncio.TimeoutError:
                if not parsed.get("stream"):
                    continue
                if resp is None:
                    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                                       "X-Accel-Buffering": "no"})
                    await resp.prepare(request)
                await resp.write(b": queued\n\n")
        try:
            up = await session.post(url, headers=headers, data=body)
        except ClientConnectionError as e:
            if resp is None:
                return web.json_response(unavailable(e), status=503)
            await resp.write(sse_error(request.path, 503, json.dumps(unavailable(e)).encode()))
            return resp
        async with up:
            return await relay(request, up, resp)
    finally:
        sched.leave(w)


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app["sched"].status())


async def on_startup(app: web.Application):
    app["sched"] = Scheduler()
    # No total or read timeout: a long prompt can keep Strata busy for minutes, and the client decides when to give up.
    app["session"] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=10), auto_decompress=False,
                                   connector=TCPConnector(limit=0, force_close=True))


async def on_cleanup(app: web.Application):
    await app["session"].close()


def make_app() -> web.Application:
    app = web.Application(client_max_size=256 << 20)
    app.router.add_get("/gateway/status", status)
    app.router.add_route("*", "/{tail:.*}", handle)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log.info("listening on :%d, upstream %s, hold %.1fs, streak %d, beat %.0fs", PORT, UPSTREAM, HOLD_S, STREAK_MAX,
             BEAT_S)
    # handler_cancellation: a client that hangs up cancels its handler, which leaves the queue or closes upstream
    web.run_app(make_app(), port=PORT, handler_cancellation=True, access_log=None, print=None)
