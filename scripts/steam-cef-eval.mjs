#!/usr/bin/env node
// Evaluate JavaScript in a Steam client's SharedJSContext over CEF remote
// debugging (the port Decky Loader enables, 127.0.0.1:8080 on the device).
// Forward it first, then pass the code as an argument or on stdin:
//
//   ssh -i ~/.ssh/nas -fN -L 18080:127.0.0.1:8080 deck@192.168.1.236
//   scripts/steam-cef-eval.mjs 'SteamClient.System.GetSystemInfo()'
//
// Promises are awaited and the result is printed as JSON.
import { readFileSync } from "node:fs";

const port = process.env.CEF_PORT ?? "18080";
const code = process.argv[2] ?? readFileSync(0, "utf8");

const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const ctx = targets.find((t) => t.title === "SharedJSContext");
if (!ctx) throw new Error("no SharedJSContext target");

const ws = new WebSocket(ctx.webSocketDebuggerUrl.replace(/127\.0\.0\.1:\d+/, `127.0.0.1:${port}`));
await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
ws.send(JSON.stringify({
  id: 1,
  method: "Runtime.evaluate",
  params: { expression: `(async () => (${code}))()`, awaitPromise: true, returnByValue: true },
}));
const msg = await new Promise((ok) => { ws.onmessage = (e) => ok(JSON.parse(e.data)); });
ws.close();

const r = msg.result;
if (r?.exceptionDetails) {
  console.error(r.exceptionDetails.exception?.description ?? JSON.stringify(r.exceptionDetails));
  process.exit(1);
}
console.log(JSON.stringify(r?.result?.value ?? r?.result, null, 2));
