"""Tests for gateway.py against a fake, slow Strata. Run: python test_gateway.py (needs aiohttp).

The fake streams SSE at the pace each request asks for and records the order in which requests reach it, so the
tests check what the scheduler let through, and when.
"""
import asyncio
import json
import os
import time

os.environ.update(UPSTREAM="http://127.0.0.1:18581", HOLD_S="0.3", STREAK_MAX="2", BEAT_S="0.2")
import gateway  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402

GW = "http://127.0.0.1:18580"
started, cancelled = [], []


async def fake_strata(request: web.Request):
    body = await request.json()
    tid = body["test_id"]
    started.append(tid)
    if body.get("fail"):
        return web.json_response({"error": {"type": "invalid_request_error", "message": "bad"}}, status=400)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    try:
        for i in range(body.get("chunks", 2)):
            await resp.write(f"data: {tid}-{i}\n\n".encode())
            await asyncio.sleep(body.get("delay", 0.05))
        await resp.write(b"data: [DONE]\n\n")
    except asyncio.CancelledError:
        cancelled.append(tid)
        raise
    return resp


def req(tid, conv, prio=None, path="/v1/chat/completions", key=None, **kw):
    headers = {}
    if prio:
        headers["X-Strata-Priority"] = prio
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"test_id": tid, "stream": True, "messages": [{"role": "user", "content": conv}], **kw}
    return path, headers, body


async def call(s: ClientSession, path, headers, body) -> tuple[int, str, list]:
    """Returns the status, the body, and when each chunk arrived (monotonic)."""
    times, parts = [], []
    async with s.post(GW + path, headers=headers, json=body) as r:
        async for chunk in r.content.iter_any():
            times.append(time.monotonic())
            parts.append(chunk.decode())
        return r.status, "".join(parts), times


async def burst(s, first, rest, gap=0.05):
    """Start `first`, then each of `rest` `gap` apart while it runs; wait for all."""
    tasks = [asyncio.create_task(call(s, *first))]
    for r in rest:
        await asyncio.sleep(gap)
        tasks.append(asyncio.create_task(call(s, *r)))
    return await asyncio.gather(*tasks)


async def test_priority_order(s):
    started.clear()
    await burst(s, req("J1", "j1", "junior", delay=0.3),
                [req("J2", "j2", "junior"), req("J3", "j3", "junior"), req("R1", "r1", "research"),
                 req("B1", "b1", key="prio-batch"), req("H1", "h1"), req("X1", "x1", "nonsense")])
    # unknown and missing priorities count as hot; within hot, first come first served
    assert started == ["J1", "H1", "X1", "B1", "R1", "J2", "J3"], started


async def test_same_conversation_kept_together(s):
    started.clear()
    a1 = asyncio.create_task(call(s, *req("A1", "convA", "junior", delay=0.2)))
    await asyncio.sleep(0.05)
    b1 = asyncio.create_task(call(s, *req("B1", "convB", "junior")))
    await a1
    await asyncio.sleep(0.1)  # the agent runs its tools, within HOLD_S
    await call(s, *req("A2", "convA", "junior"))
    await b1
    assert started == ["A1", "A2", "B1"], started


async def test_streak_limit(s):
    started.clear()
    await burst(s, req("C1", "convC", "research", delay=0.2),
                [req("D1", "convD", "research"), req("C2", "convC", "research"), req("C3", "convC", "research")])
    assert started == ["C1", "C2", "D1", "C3"], started  # STREAK_MAX=2: C1, C2, then D1 gets its turn


async def test_hold_does_not_block_higher(s):
    started.clear()
    j = asyncio.create_task(call(s, *req("J", "convJ", "junior", delay=0.1)))
    await asyncio.sleep(0.02)
    j2 = asyncio.create_task(call(s, *req("J2", "other", "junior")))
    await j
    t = time.monotonic()
    await call(s, *req("H", "convH"))     # arrives during the hold for convJ: must not wait for it
    assert time.monotonic() - t < 0.25
    await j2
    assert started == ["J", "H", "J2"], started


async def test_cancel_while_queued_and_in_flight(s):
    started.clear(), cancelled.clear()
    long = asyncio.create_task(call(s, *req("L", "convL", "hot", chunks=20, delay=0.05)))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(call(s, *req("Q", "convQ", "junior")))
    await asyncio.sleep(0.1)
    queued.cancel()
    await asyncio.sleep(0.05)
    async with s.get(GW + "/gateway/status") as r:
        st = await r.json()
    assert st["queued"]["junior"] == 0 and st["in_flight"]["priority"] == "hot", st
    long.cancel()
    await asyncio.sleep(0.2)
    assert cancelled == ["L"], cancelled
    assert "Q" not in started, started
    async with s.get(GW + "/gateway/status") as r:
        st = await r.json()
    assert st["in_flight"] is None and st["queued"]["junior"] == 0, st


async def test_streams_without_buffering(s):
    status, text, times = await call(s, *req("S", "convS", chunks=3, delay=0.3))
    assert status == 200 and "S-0" in text and "[DONE]" in text
    assert times[-1] - times[0] > 0.5, times  # the chunks came spread out, not all at the end


async def test_keepalive_while_queued_and_late_error(s):
    started.clear()
    results = await burst(s, req("K1", "convK", delay=0.5),
                          [req("K2", "convK2", "junior"),
                           req("E1", "convE", "junior", path="/v1/messages", fail=True)])
    _, k2, _ = results[1]
    assert k2.startswith(": queued") and "K2-1" in k2, k2
    status, e1, _ = results[2]
    # the 200 had gone out while it waited, so Strata's 400 arrives as an SSE error event
    assert status == 200 and "event: error" in e1 and '"type": "error"' in e1, e1


async def test_non_generation_passes_through(s):
    started.clear()
    blocker = asyncio.create_task(call(s, *req("G", "convG", delay=0.3)))
    await asyncio.sleep(0.05)
    t = time.monotonic()
    async with s.post(GW + "/v1/messages/count_tokens", json={"test_id": "count", "stream": False}) as r:
        await r.read()
    assert time.monotonic() - t < 0.2  # not queued behind the generation
    await blocker


async def test_conversation_key_stable_across_turns(s):
    conv = gateway.conversation_of
    cc = {"type": "ephemeral"}
    sys_ = [{"type": "text", "text": "You are Claude Code", "cache_control": cc}]
    turn1 = {"system": sys_, "messages": [{"role": "user", "content": [{"type": "text", "text": "fix it",
                                                                         "cache_control": cc}]},
                                          {"role": "system", "content": [{"type": "text", "text": "# Environment",
                                                                          "cache_control": cc}]}]}
    turn2 = {"system": sys_, "messages": [{"role": "user", "content": [{"type": "text", "text": "fix it"}]},
                                          {"role": "system", "content": "# Environment"},
                                          {"role": "assistant", "content": "calling a tool"},
                                          {"role": "user", "content": [{"type": "text", "text": "result",
                                                                        "cache_control": cc}]}]}
    other = {"system": sys_, "messages": [{"role": "user", "content": [{"type": "text", "text": "other task"}]}]}
    assert conv(turn1) == conv(turn2) != conv(other)
    oa1 = {"messages": [{"role": "system", "content": "pi"}, {"role": "user", "content": "task"}]}
    oa2 = {"messages": oa1["messages"] + [{"role": "assistant", "content": "x"}, {"role": "tool", "content": "y"}]}
    assert conv(oa1) == conv(oa2) != conv({"messages": [{"role": "system", "content": "pi"},
                                                         {"role": "user", "content": "other"}]})


async def main():
    fake = web.Application()
    fake.router.add_route("*", "/{tail:.*}", fake_strata)
    runners = [web.AppRunner(fake, handler_cancellation=True),
               web.AppRunner(gateway.make_app(), handler_cancellation=True)]
    for runner, port in zip(runners, (18581, 18580)):
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    try:
        async with ClientSession() as s:
            for t in tests:
                await t(s)
                await asyncio.sleep(0.4)  # let the hold of the last test run out
                print("ok  ", t.__name__)
    finally:
        for runner in runners:
            await runner.cleanup()
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    asyncio.run(main())
