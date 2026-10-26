"""Endpoints the strata-eval scripts can talk to, and how to reach them.

Keys are read from files, never from the command line, so they do not end up in
shell history or process listings:
  ~/.cache/strata-eval/gemma.key    copy of /run/agenix/llm-api-key (work-vm-00 llama-server)
  ~/.cache/strata-eval/litellm.key  ESG dev key, from sensei-ovh ns dev secret chino-external-env
                                    LITELLM_API_KEYS["ESG"] (see setup-keys.sh)
Strata needs no key; reach it with
  kubectl --context lamg port-forward -n strata svc/strata 18091:8080

Strata reasons by default on both APIs. Off per request: OpenAI
chat_template_kwargs.enable_thinking=false (or reasoning_effort "none"), Anthropic
thinking.type=disabled. STRATA_NOTHINK_BODY / STRATA_NOTHINK_BODY_ANTHROPIC override them.

Strata decodes greedy when a request carries no sampling fields (no `sampling` block in its
run config), and Claude Code sends none. A sampling preset is appended to a target name with
"+": `strata-nothink+qwen-instruct` merges SAMPLING["qwen-instruct"] into both API bodies, and
the results files carry the full name. Strata takes these fields on both APIs (min_p and the
penalties too, on /v1/messages); top_k is clamped to 64.
"""
import json, os

KEYDIR = os.path.expanduser("~/.cache/strata-eval")
LITELLM = "https://litellm.senseivision.ai"


def _key(name):
    p = os.path.join(KEYDIR, name)
    return open(p).read().strip() if os.path.exists(p) else ""


def _nothink():
    return json.loads(os.environ.get("STRATA_NOTHINK_BODY", '{"chat_template_kwargs": {"enable_thinking": false}}'))


def _nothink_anthropic():
    return json.loads(os.environ.get("STRATA_NOTHINK_BODY_ANTHROPIC", '{"thinking": {"type": "disabled"}}'))


# root: server root (without /v1). extra: merged into every OpenAI request body.
# extra_anthropic: merged into every /v1/messages body (defaults to extra).
# litellm: the gateway has fallbacks, so ask it not to fall back and record which group answered.
# The Copilot kimi rungs were dropped on 2026-10-16: kimi-k2.7-code is no longer offered to
# LiteLLM's Copilot integrator and kimi-k3 answers 402 (monthly quota). Both gemini rungs bill Vertex.
TARGETS = {
    "strata": dict(root=os.environ.get("STRATA_URL", "http://127.0.0.1:18091"), model=os.environ.get("STRATA_MODEL", "qwen3.8-flash-next-iq3_xxs"),
                   key="none", extra={}),
    "strata-nothink": dict(root=os.environ.get("STRATA_URL", "http://127.0.0.1:18091"), model=os.environ.get("STRATA_MODEL", "qwen3.8-flash-next-iq3_xxs"),
                           key="none", extra=_nothink(), extra_anthropic=_nothink_anthropic()),
    "gemma": dict(root="http://127.0.0.1:8080", model="gemma-4-12B-it-qat-UD-Q4_K_XL.gguf", key=_key("gemma.key"), extra={}),
    "gemini-flash-lite": dict(root=LITELLM, model="gemini-3.1-flash-lite", key=_key("litellm.key"),
                              extra={"disable_fallbacks": True}, litellm=True),
    "gemini-3.5-flash": dict(root=LITELLM, model="gemini-3.5-flash", key=_key("litellm.key"),
                             extra={"disable_fallbacks": True}, litellm=True),
}


# Sampling presets. qwen-*: the "Best Practices" of https://huggingface.co/Qwen/Qwen3.8-Flash-Next
# (Unsloth's guide repeats them). The card's presence_penalty assumes vLLM/SGLang, which count every
# generated token; Strata counts the last penalty_last_n (default 64). greedy = what every client
# got before (temperature 0, or no sampling fields at all).
SAMPLING = {
    "greedy": {"temperature": 0},
    "qwen-instruct": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0, "presence_penalty": 1.5},
    "qwen-instruct-nopp": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0},
    "qwen-think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0},
}


def get(name):
    base, _, preset = name.partition("+")
    if base not in TARGETS:
        raise SystemExit("unknown target %r, one of: %s" % (base, ", ".join(TARGETS)))
    if preset and preset not in SAMPLING:
        raise SystemExit("unknown sampling preset %r, one of: %s" % (preset, ", ".join(SAMPLING)))
    t = dict(TARGETS[base], name=name)
    t.setdefault("extra_anthropic", t["extra"])
    if preset:
        t["sampling"] = SAMPLING[preset]
        t["extra"] = dict(t["extra"], **t["sampling"])
        t["extra_anthropic"] = dict(t["extra_anthropic"], **t["sampling"])
    return t
