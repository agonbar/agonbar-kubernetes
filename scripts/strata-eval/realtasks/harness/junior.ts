// junior.ts: a harness for a mid-size local model working as a junior on a git repo.
//
// Four mechanisms, aimed at what Strata got wrong on the realtasks benchmark:
//   plan    the model declares steps and the files it will touch (with a reason) before any
//           write; write/edit outside that list is blocked until it calls plan again.
//   budget  every tool result ends with the calls and minutes left; past 70 % it is told to
//           close, past 90 % only read-only tools and finish run.
//   finish  the only way to end. It rejects: changed files without a reason, new untracked files
//           outside the plan, causal claims without file:line or command evidence, and declared
//           verification commands that fail (it reruns them). When everything passes it shows
//           the full diff and asks for one confirmation. Past the hard limit, or after 3 rejections, it
//           accepts anyway and lists what is still wrong.
//   bash    every command gets a timeout (300 s or the time left), pi has none by default.
//   settle  if the model stops talking without finish, it gets sent back (twice at most).
//
// Config (env): JUNIOR_MAX_CALLS (model calls, default 100), JUNIOR_DEADLINE (epoch seconds),
// JUNIOR_SECONDS (the time budget the deadline came from), JUNIOR_BASE (commit the diff is taken
// against, default HEAD at start), JUNIOR_REPORT (path for the accepted finish as JSON, outside the repo).
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { isAbsolute, relative, resolve } from "node:path";
import { createHash } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const MAX_CALLS = Number(process.env.JUNIOR_MAX_CALLS || 100);
const DEADLINE = Number(process.env.JUNIOR_DEADLINE || 0); // 0 = no time limit
const REPORT = process.env.JUNIOR_REPORT || "";
const SOFT = 0.7;
const HARD = 0.9;
const READ_ONLY = new Set(["read", "grep", "find", "ls", "finish", "plan"]);
const DIFF_MAX_LINES = 400;
const MAX_REJECTS = 3;
const BASH_TIMEOUT = 300; // seconds, for every bash call // after this many rejected finish calls, accept and list the problems

const RULES = `Trabajas como programador junior. Una persona revisará tu trabajo como un PR.
- Primero llama a \`plan\` con los pasos y los ficheros que vas a tocar, cada uno con su motivo. Solo puedes escribir en esos ficheros; si necesitas otro, vuelve a llamar a \`plan\`.
- Haz solo lo que pide la tarea. Nada de funciones, casos, verbos, ficheros, documentación ni refactors que no se pidan. Los ficheros temporales van en /tmp, nunca en el repo.
- No borres recursos, datos ni ficheros que la tarea no pida borrar.
- Toda afirmación sobre una causa necesita evidencia: \`ruta:línea\` de un fichero que hayas leído o la salida de un comando que hayas ejecutado. Si no la tienes, escríbela como hipótesis.
- Termina SIEMPRE con \`finish\`. Con cada llamada verás cuántas te quedan; cuando te avise, cierra.`;

function git(cwd: string, ...args: string[]): string {
	try {
		return execFileSync("git", args, { cwd, encoding: "utf8", maxBuffer: 64 << 20, stdio: ["ignore", "pipe", "ignore"] });
	} catch {
		return "";
	}
}

function rel(cwd: string, p: string): string {
	const abs = isAbsolute(p) ? p : resolve(cwd, p);
	return relative(cwd, abs);
}

interface State {
	base: string;
	calls: number;
	plan: Map<string, string> | null;
	finished: boolean;
	nudges: number;
	confirmHash: string; // diff hash shown to the model, awaiting confirmation
	verified: Map<string, { code: number; tail: string }>; // command -> result, valid for confirmHash
	rejects: number;
}

export default function (pi: ExtensionAPI) {
	const st: State = { base: "", calls: 0, plan: null, finished: false, nudges: 0, confirmHash: "", verified: new Map(), rejects: 0 };

	const fraction = () => {
		const byCalls = st.calls / MAX_CALLS;
		const byTime = DEADLINE ? 1 - (DEADLINE - Date.now() / 1000) / Number(process.env.JUNIOR_SECONDS || 1800) : 0;
		return Math.max(byCalls, byTime);
	};
	const minutesLeft = () => (DEADLINE ? Math.max(0, Math.round((DEADLINE - Date.now() / 1000) / 60)) : null);
	const budgetLine = () => {
		const left = Math.max(0, MAX_CALLS - st.calls);
		const m = minutesLeft();
		const f = fraction();
		let s = `[presupuesto: llamada ${st.calls}/${MAX_CALLS}, quedan ${left}${m === null ? "" : ` y ${m} min`}]`;
		if (f >= HARD) s += " LÍMITE: solo puedes leer y llamar a finish. Llama a finish ahora; lo que no hayas comprobado va en not_verified.";
		else if (f >= SOFT) s += " Empieza a cerrar: comprueba lo que tengas y llama a finish.";
		return s;
	};

	pi.on("session_start", (_e, ctx) => {
		st.base = process.env.JUNIOR_BASE || git(ctx.cwd, "rev-parse", "HEAD").trim();
	});

	pi.on("before_agent_start", (event) => {
		event.systemPromptOptions.sections.junior_rules = RULES;
	});

	pi.on("turn_start", () => {
		st.calls += 1;
	});

	pi.on("tool_call", (event, ctx) => {
		if (st.finished) return { block: true, reason: "Ya llamaste a finish; la tarea está entregada." };
		if (fraction() >= HARD && !READ_ONLY.has(event.toolName)) {
			return { block: true, reason: "Se acabó el presupuesto. Llama a finish ya; pon en not_verified lo que falte." };
		}
		if (event.toolName === "bash") {
			// pi's bash has no default timeout; a hung command would eat the whole budget
			const input = event.input as { timeout?: number };
			const left = DEADLINE ? Math.floor(DEADLINE - Date.now() / 1000 - 60) : BASH_TIMEOUT;
			input.timeout = Math.max(10, Math.min(input.timeout ?? BASH_TIMEOUT, BASH_TIMEOUT, left));
		}
		if (event.toolName === "write" || event.toolName === "edit") {
			const path = String((event.input as { path?: string }).path ?? "");
			const r = rel(ctx.cwd, path);
			if (r.startsWith("..") || isAbsolute(r)) {
				if (resolve(ctx.cwd, path).startsWith("/tmp/")) return undefined;
				return { block: true, reason: `Fuera del repo: ${path}. Solo puedes escribir en el repo (o en /tmp para temporales).` };
			}
			if (!st.plan) return { block: true, reason: "Antes de escribir, llama a plan con los pasos y los ficheros que vas a tocar." };
			if (!st.plan.has(r)) {
				return {
					block: true,
					reason: `${r} no está en tu plan (${[...st.plan.keys()].join(", ")}). ¿Lo pide la tarea? Si sí, llama a plan otra vez añadiéndolo con el motivo; si no, no lo toques.`,
				};
			}
		}
		return undefined;
	});

	pi.on("tool_result", (event) => {
		if (event.toolName === "finish") return undefined;
		return { content: [...event.content, { type: "text" as const, text: `\n${budgetLine()}` }], structuredContent: event.structuredContent };
	});

	pi.on("agent_before_settle", (event) => {
		if (st.finished || st.nudges >= 2 || event.outcome !== "completed") return undefined;
		st.nudges += 1;
		return {
			continue: true,
			entries: [
				{
					type: "custom_message" as const,
					customType: "junior_nudge",
					display: true,
					content: "No has llamado a finish. La tarea solo se entrega con finish: llámalo ahora con el resumen, la evidencia y lo que no hayas comprobado.",
				},
			],
		};
	});

	pi.registerTool({
		name: "plan",
		label: "Plan",
		description:
			"Declara el plan: pasos breves y los ficheros del repo que vas a crear o modificar, cada uno con su motivo. Obligatorio antes de escribir. Llámalo otra vez para ampliar la lista.",
		parameters: Type.Object({
			steps: Type.Array(Type.String(), { description: "Pasos, uno por línea, máximo 6" }),
			files: Type.Array(Type.Object({ path: Type.String(), why: Type.String() }), {
				description: "Ficheros que vas a tocar, rutas relativas al repo",
			}),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const m = st.plan ?? new Map<string, string>();
			for (const f of params.files) m.set(rel(ctx.cwd, f.path), f.why);
			st.plan = m;
			return {
				content: [{ type: "text", text: `Plan registrado. Puedes escribir en: ${[...m.keys()].join(", ")}.` }],
				details: undefined,
			};
		},
	});

	pi.registerTool({
		name: "finish",
		label: "Finish",
		description:
			"Entrega la tarea. Comprueba el diff y la evidencia y vuelve a ejecutar tus comandos de verificación. Si algo no cuadra te lo devuelve para corregir. Si todo cuadra te enseña el diff completo y tienes que llamarlo otra vez con confirm=true.",
		parameters: Type.Object({
			summary: Type.String({ description: "Qué cambiaste y por qué, en 2-5 líneas" }),
			files: Type.Array(Type.Object({ path: Type.String(), why: Type.String() }), {
				description: "Todos los ficheros cambiados o nuevos del diff, cada uno con su motivo",
			}),
			claims: Type.Array(
				Type.Object({
					claim: Type.String({ description: "Una afirmación sobre la causa o el comportamiento" }),
					evidence: Type.String({
						description: "ruta:línea de un fichero actual (p. ej. src/a.go:42), o el comando que ejecutaste y lo que dio (p. ej. `$ git show HEAD:src/a.go | sed -n 42p` -> ...)",
					}),
				}),
				{ description: "Cada afirmación causal con su evidencia. Vacío si no hay ninguna." },
			),
			verification: Type.Array(Type.String(), {
				description: "Comandos de shell que demuestran que funciona (tests, lint, eval), solo el comando, sin comentarios. finish los vuelve a ejecutar sobre el estado actual.",
			}),
			not_verified: Type.Array(Type.String(), { description: "Lo que no pudiste comprobar y por qué" }),
			confirm: Type.Optional(Type.Boolean({ description: "true tras revisar el diff que te enseñó finish" })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			try {
				return finish(params, ctx.cwd);
			} catch (e) {
				return { content: [{ type: "text", text: `finish falló por dentro (${String(e).slice(0, 200)}). Vuelve a llamarlo.` }], details: undefined };
			}
		},
	});

	// biome-ignore lint: params mirrors the finish schema
	function finish(params: any, cwd: string) {
		{
			const forced = fraction() >= HARD || st.rejects >= MAX_REJECTS;
			const problems: string[] = [];

			// what changed: tracked changes against the base plus untracked files
			const changed = git(cwd, "diff", "--name-only", st.base || "HEAD").split("\n").filter(Boolean);
			const untracked = git(cwd, "ls-files", "--others", "--exclude-standard").split("\n").filter(Boolean);
			const newFiles = new Set(git(cwd, "diff", "--name-only", "--diff-filter=A", st.base || "HEAD").split("\n").filter(Boolean));
			for (const u of untracked) newFiles.add(u);
			const all = [...new Set([...changed, ...untracked])].sort();
			const declared = new Map(params.files.map((f) => [rel(cwd, f.path), f.why]));

			for (const f of all) {
				if (!declared.has(f)) problems.push(`${f} está en el diff y no lo explicas en files. Explícalo o reviértelo (git checkout / rm).`);
				if (newFiles.has(f) && !(st.plan?.has(f))) {
					problems.push(`${f} es nuevo y no estaba en tu plan. Si es temporal o de prueba, bórralo; si lo pide la tarea, añádelo al plan con el motivo.`);
				}
			}
			for (const f of declared.keys()) if (!all.includes(f)) problems.push(`${f} está en files pero no cambió. Quítalo de files.`);

			// evidence for each causal claim
			const cited: string[] = [];
			for (const c of params.claims) {
				const ev = c.evidence.trim();
				const fl = ev.match(/([\w./@+-]+):(\d+)(?:-(\d+))?/);
				if (fl) {
					const p = resolve(cwd, fl[1]);
					if (!existsSync(p)) {
						problems.push(`Evidencia de "${c.claim}": ${fl[1]} no existe.`);
						continue;
					}
					const lines = readFileSync(p, "utf8").split("\n");
					const a = Number(fl[2]);
					const b = Math.min(Number(fl[3] || fl[2]), a + 4);
					if (a < 1 || a > lines.length) {
						problems.push(`Evidencia de "${c.claim}": ${fl[1]} tiene ${lines.length} líneas, no ${a}.`);
						continue;
					}
					cited.push(`${fl[1]}:${a}: ${lines.slice(a - 1, b).join("\n    ").slice(0, 300)}\n  -> ${c.claim}`);
				} else if (/[`$]/.test(ev) || params.verification.some((v: string) => ev.includes(v))) {
					cited.push(`${ev.slice(0, 120)}\n  -> ${c.claim}`);
				} else {
					problems.push(`"${c.claim}" no tiene evidencia válida (${ev.slice(0, 80)}). Cita ruta:línea o un comando de verification, o quita la afirmación.`);
				}
			}

			// rerun verification commands (cached per diff, so confirm does not run them twice)
			const diff = git(cwd, "diff", st.base || "HEAD");
			const newText = untracked
				.map((f) => {
					try {
						const t = readFileSync(resolve(cwd, f), "utf8");
						return `--- nuevo: ${f}\n${t.split("\n").slice(0, 80).join("\n")}`;
					} catch {
						return `--- nuevo: ${f} (no se puede leer como texto)`;
					}
				})
				.join("\n");
			const hash = createHash("sha1").update(diff + newText).digest("hex");
			if (hash !== st.confirmHash) st.verified = new Map();
			const runs: string[] = [];
			for (const cmd of params.verification) {
				let r = st.verified.get(cmd);
				if (!r) {
					const left = DEADLINE ? DEADLINE - Date.now() / 1000 - 60 : 600;
					const p = spawnSync("bash", ["-c", cmd], {
						cwd,
						encoding: "utf8",
						timeout: Math.floor(Math.max(20, Math.min(600, left)) * 1000),
						maxBuffer: 32 << 20,
						stdio: ["ignore", "pipe", "pipe"],
					});
					const out = `${p.stdout ?? ""}${p.stderr ?? ""}`.trimEnd().split("\n").slice(-15).join("\n");
					r = { code: p.status ?? 124, tail: p.error ? `${String(p.error)}\n${out}` : out };
					st.verified.set(cmd, r);
				}
				runs.push(`$ ${cmd}  -> exit ${r.code}${r.code ? `\n${r.tail}` : ""}`);
				if (r.code === 2 && /syntax error/.test(r.tail))
					problems.push(`\`${cmd}\` no es un comando de shell válido. En verification va solo el comando, sin comentarios.`);
				else if (r.code !== 0 && !params.not_verified.some((n: string) => n.includes(cmd) || n.toLowerCase().includes("falla")))
					problems.push(`\`${cmd}\` sale con ${r.code}. Arréglalo, o si no puedes, dilo en not_verified.`);
			}
			// files the verification run itself created (__pycache__, test output) are not the model's
			const before = new Set(untracked);
			for (const f of git(cwd, "ls-files", "--others", "--exclude-standard").split("\n").filter(Boolean))
				if (!before.has(f)) rmSync(resolve(cwd, f), { force: true });
			if (params.verification.length === 0 && params.not_verified.length === 0)
				problems.push("No das ningún comando de verificación ni dices qué no comprobaste.");

			const report = { ...params, changed: all, problems, forced, calls: st.calls };
			if (problems.length && !forced) {
				st.confirmHash = "";
				st.rejects += 1;
				return {
					content: [{ type: "text", text: `finish rechazado. Corrige esto y vuelve a llamar a finish:\n- ${problems.join("\n- ")}\n${runs.length ? `\nVerificación:\n${runs.join("\n")}` : ""}\n${budgetLine()}` }],
					details: undefined,
				};
			}
			if (!forced && !(params.confirm && st.confirmHash === hash)) {
				st.confirmHash = hash;
				const dl = diff.split("\n");
				const shown = dl.length > DIFF_MAX_LINES ? `${dl.slice(0, DIFF_MAX_LINES).join("\n")}\n[... ${dl.length - DIFF_MAX_LINES} líneas más]` : diff;
				const untrackedShown = untracked.length ? `\nFicheros nuevos sin seguimiento:\n${newText}` : "";
				return {
					content: [
						{
							type: "text",
							text: `Revisa tu entrega antes de confirmar.\n\nDiff:\n${shown}${untrackedShown}\n\nEvidencia citada:\n${cited.join("\n") || "(ninguna)"}\n\nVerificación:\n${runs.join("\n") || "(ninguna)"}\n\nComprueba:\n1. ¿Cada línea del diff la pide la tarea? Quita lo que no.\n2. ¿Cada línea citada demuestra de verdad su afirmación?\n3. ¿El resumen y los ficheros (p. ej. ANSWER.md) dicen lo mismo, sin contradicciones?\nSi algo falla, corrígelo y llama a finish de nuevo. Si todo está bien, llama a finish con los mismos datos y confirm=true.\n${budgetLine()}`,
						},
					],
					details: undefined,
				};
			}
			st.finished = true;
			if (REPORT) writeFileSync(REPORT, JSON.stringify(report, null, 1));
			const text = `${forced ? "Entregado por límite de presupuesto, con problemas pendientes" : "Entregado"}.\n${params.summary}${problems.length ? `\nPendiente:\n- ${problems.join("\n- ")}` : ""}`;
			return { content: [{ type: "text", text }], details: report, terminate: true };
		}
	}
}
