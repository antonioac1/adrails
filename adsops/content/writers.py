"""Model backends for the guarded content pipeline (AdRails 0.12): who writes, who reviews, what it costs, and which
model ACTUALLY answered. PM-written security core (Order D).

Three backend kinds, all configured by the operator (profile "writers" / "reviewers" / "editor" lists):

  openrouter         https://openrouter.ai/api/v1/chat/completions, key from env (default OPENROUTER_API_KEY). Reported
                     cost (usage.cost) counts toward the run's cost cap.
  openai_compatible  any /chat/completions API: base_url + key_env (z.ai GLM, DeepSeek, OpenAI, a local server).
  command            a local CLI the operator configured (e.g. an OAuth/subscription login). The prompt goes through a
                     0600 temp file ("{prompt_file}" in cmd) or stdin, NEVER argv ("{nonce}" = a fresh id per
                     call, e.g. a session id). Its JSON output names the model that answered.

Rules enforced here:
  * Keys are read from the environment BY NAME at call time. They are never logged, returned, put in errors, or passed
    to a `command` backend (every configured key env is removed from the child's environment).
  * The answering model is read from the response (OpenAI-style "model", or the command's configured JSON path) and must
    be the requested model (a dated/version suffix is allowed). Anything else is a silent fallback: refused. A
    response that does not say which model answered is refused too (unverifiable).
  * Model family comes from the model id (family_of); a backend may declare "family" only for ids the table does not
    know, and then only the exact requested id is accepted.
  * Caps per run: calls, tokens, cost (USD) and a wall-clock deadline. Checked before every call; a call that pushes
    the run over a cap is recorded and the run stops (CapExceeded).
  * finish_reason "length" (truncated output) is an error, never a usable draft.
  * Retries are capped (0..3) and only for 429 / 5xx / timeouts.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from ..errors import ToolError, scrub

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
KINDS = ("openrouter", "openai_compatible", "command")
MAX_PROMPT = 400_000
MAX_OUTPUT = 5_000_000
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SUFFIX_RE = re.compile(r"^[-:@]?(?:\d{4}-?\d{2}-?\d{2}|\d{4})$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# vendor prefix (OpenRouter style "vendor/model") or bare model-name prefix -> family. Order matters (first match wins).
_VENDORS = {"openai": "openai", "anthropic": "anthropic", "google": "google", "z-ai": "zhipu", "zai": "zhipu",
            "zhipu": "zhipu", "thudm": "zhipu", "deepseek": "deepseek", "meta-llama": "meta", "meta": "meta",
            "mistralai": "mistral", "mistral": "mistral", "qwen": "qwen", "alibaba": "qwen", "x-ai": "xai",
            "xai": "xai", "moonshotai": "moonshot", "moonshot": "moonshot", "cohere": "cohere", "microsoft": "microsoft",
            "amazon": "amazon", "nvidia": "nvidia", "minimax": "minimax"}
_PREFIXES = (("gpt-", "openai"), ("gpt", "openai"), ("chatgpt", "openai"), ("o1", "openai"), ("o3", "openai"),
             ("o4", "openai"), ("codex", "openai"), ("claude", "anthropic"), ("gemini", "google"), ("gemma", "google"),
             ("glm", "zhipu"), ("chatglm", "zhipu"), ("deepseek", "deepseek"), ("llama", "meta"), ("mistral", "mistral"),
             ("mixtral", "mistral"), ("codestral", "mistral"), ("magistral", "mistral"), ("qwen", "qwen"),
             ("qwq", "qwen"), ("grok", "xai"), ("kimi", "moonshot"), ("command-r", "cohere"), ("phi-", "microsoft"),
             ("nova-", "amazon"), ("minimax", "minimax"))


class BackendError(ToolError):
    """One backend failed (network, HTTP status, truncated, unparseable). The chain may try the next backend."""


class Refused(ToolError):
    """A rule refused the call or its answer (silent fallback, unverifiable model, same family). Recorded as a hold."""


class CapExceeded(ToolError):
    """The run hit a call / token / cost / time cap. Nothing further is called."""


def family_of(model_id):
    """-> family string ("openai", "anthropic", "zhipu", ...) or None when the id is not recognised."""
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    m = model_id.strip().lower()
    if m.startswith("openrouter/"):
        m = m[len("openrouter/"):]
    if "/" in m:
        vendor, _, name = m.partition("/")
        if vendor in _VENDORS:
            return _VENDORS[vendor]
        m = name.rsplit("/", 1)[-1]
    for prefix, fam in _PREFIXES:
        if m.startswith(prefix):
            return fam
    return None


def _bare(model_id):
    m = (model_id or "").strip().lower()
    return m.rsplit("/", 1)[-1]


def model_matches(requested, answered):
    """True when `answered` is the requested model: same id (vendor prefix optional on either side) or the same id
    plus a date/version suffix ("gpt-5" -> "gpt-5-2025-08-07"), and never a different family."""
    if not isinstance(answered, str) or not answered.strip() or not isinstance(requested, str):
        return False
    rq, an = _bare(requested), _bare(answered)
    if not rq or not an:
        return False
    fr, fa = family_of(requested), family_of(answered)
    if fr and fa and fr != fa:
        return False
    if "/" in answered and "/" in requested:
        if answered.strip().lower().split("/", 1)[0] != requested.strip().lower().split("/", 1)[0]:
            return False
    return an == rq or (an.startswith(rq) and bool(_SUFFIX_RE.match(an[len(rq):])))


def backend_family(b):
    """Family of a configured backend: from its model id, else its declared "family"."""
    return family_of(b.get("model")) or (str(b["family"]).strip().lower() if b.get("family") else None)


def validate_backend(b, role="writer"):
    """-> normalised copy of one backend config, or ToolError with a plain reason."""
    if not isinstance(b, dict):
        raise ToolError("%s backend must be an object" % role)
    kind = b.get("kind")
    if kind not in KINDS:
        raise ToolError("%s backend kind must be one of %s" % (role, ", ".join(KINDS)))
    name = str(b.get("name") or kind)
    if not _NAME_RE.match(name):
        raise ToolError("%s backend name must match [a-z0-9_-]{1,40}" % role)
    model = b.get("model")
    if not isinstance(model, str) or not _MODEL_RE.match(model):
        raise ToolError("%s backend %s: model id is required ([A-Za-z0-9._:/@+-], <= 128 chars)" % (role, name))
    out = {"name": name, "kind": kind, "model": model, "role": role,
           "max_tokens": int(b.get("max_tokens") or 16000), "timeout_s": int(b.get("timeout_s") or 600),
           "temperature": float(b.get("temperature") if b.get("temperature") is not None else 0.3),
           "retries": int(b.get("retries") if b.get("retries") is not None else 1)}
    if not 256 <= out["max_tokens"] <= 200000 or not 5 <= out["timeout_s"] <= 3600 or not 0 <= out["retries"] <= 3:
        raise ToolError("%s backend %s: max_tokens 256..200000, timeout_s 5..3600, retries 0..3" % (role, name))
    if not 0.0 <= out["temperature"] <= 2.0:
        raise ToolError("%s backend %s: temperature 0..2" % (role, name))
    if b.get("family"):
        fam = str(b["family"]).strip().lower()
        known = family_of(model)
        if known and known != fam:
            raise ToolError("%s backend %s: declared family %r contradicts model %r (%s)" % (role, name, fam, model, known))
        out["family"] = fam
    if not backend_family(out):
        raise ToolError("%s backend %s: unknown model family for %r; set \"family\" explicitly" % (role, name, model))
    price = b.get("price_per_mtok")
    if price is not None:
        if not isinstance(price, dict) or not all(isinstance(price.get(k, 0), (int, float)) and price.get(k, 0) >= 0
                                                  for k in ("in", "out")):
            raise ToolError("%s backend %s: price_per_mtok must be {\"in\": USD, \"out\": USD}" % (role, name))
        out["price_per_mtok"] = {"in": float(price.get("in", 0)), "out": float(price.get("out", 0))}
    if kind in ("openrouter", "openai_compatible"):
        env = b.get("key_env") or ("OPENROUTER_API_KEY" if kind == "openrouter" else None)
        if not isinstance(env, str) or not _ENV_RE.match(env) or env == "ADRAILS_APPROVAL_SECRET":
            raise ToolError("%s backend %s: key_env must be an environment variable NAME (A-Z0-9_), never the key" % (role, name))
        out["key_env"] = env
        url = OPENROUTER_URL if kind == "openrouter" else str(b.get("base_url") or "").rstrip("/")
        if kind == "openai_compatible":
            if not url:
                raise ToolError("%s backend %s: base_url is required" % (role, name))
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
        p = urllib.parse.urlsplit(url)
        loop = (p.hostname or "") in _LOOPBACK
        if p.scheme != "https" and not (p.scheme == "http" and loop):
            raise ToolError("%s backend %s: base_url must be https (plain http only for localhost)" % (role, name))
        if p.username or p.password or p.query:
            raise ToolError("%s backend %s: base_url must not carry credentials or a query string" % (role, name))
        out["url"] = url
    else:
        cmd = b.get("cmd")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd) or len(cmd) > 64:
            raise ToolError("%s backend %s: cmd must be a non-empty list of strings (argv, no shell)" % (role, name))
        if any("{prompt}" in x for x in cmd):
            raise ToolError("%s backend %s: the prompt never goes in argv; use {prompt_file} or stdin" % (role, name))
        out["cmd"] = list(cmd)
        out["output"] = b.get("output") or "json"
        if out["output"] != "json":
            raise ToolError("%s backend %s: output must be \"json\" (the answering model must be readable)" % (role, name))
        for key in ("text_paths", "model_paths"):
            v = b.get(key)
            if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
                raise ToolError("%s backend %s: %s (list of JSON paths) is required" % (role, name, key))
            out[key] = list(v)
        for key in ("fallback_paths", "tools_paths"):
            v = b.get(key) or []
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ToolError("%s backend %s: %s must be a list of JSON paths" % (role, name, key))
            out[key] = list(v)
        if b.get("cwd"):
            out["cwd"] = os.path.expanduser(str(b["cwd"]))
        pe = b.get("pass_env") or []
        if not isinstance(pe, list) or not all(isinstance(x, str) and _ENV_RE.match(x) for x in pe) or \
                "ADRAILS_APPROVAL_SECRET" in pe:
            raise ToolError("%s backend %s: pass_env must list env var NAMES (never the approval secret)" % (role, name))
        out["pass_env"] = list(pe)
    return out


def validate_chains(writers, reviewers, editor=None):
    """-> (writers, reviewers, editor) normalised. REFUSES any reviewer/editor of the same family as any writer in the
    chain: whichever writer ends up drafting, the review is by another family."""
    if not isinstance(writers, list) or not writers:
        raise ToolError("at least one writer backend is required")
    if not isinstance(reviewers, list) or not reviewers:
        raise ToolError("at least one reviewer backend is required: the adversarial review is mandatory")
    if len(writers) > 5 or len(reviewers) > 5:
        raise ToolError("at most 5 writers and 5 reviewers")
    ws = [validate_backend(b, "writer") for b in writers]
    rs = [validate_backend(b, "reviewer") for b in reviewers]
    ed = validate_backend(editor, "editor") if editor else None
    wf = {backend_family(w) for w in ws}
    for r in rs + ([ed] if ed else []):
        if backend_family(r) in wf:
            raise Refused("%s %s (%s) is the same model family as a writer (%s): the adversarial review must come from "
                          "a different family" % (r["role"], r["name"], r["model"], ", ".join(sorted(wf))))
    return ws, rs, ed


class Budget:
    """Per-run caps. `check()` before every call; `charge(reply)` after it."""

    def __init__(self, caps=None, clock=time.monotonic):
        c = caps or {}
        self.max_calls = int(c.get("max_calls", 8))
        self.max_tokens = int(c.get("max_tokens", 250000))
        self.max_cost = float(c.get("max_cost_usd", 2.0))
        self.deadline = clock() + float(c.get("deadline_s", 2400))
        self.clock = clock
        self.calls = 0
        self.tokens = 0
        self.cost = 0.0
        self.cost_unknown_calls = 0

    def remaining_s(self):
        return self.deadline - self.clock()

    def check(self):
        if self.calls >= self.max_calls:
            raise CapExceeded("call cap reached (%d calls)" % self.max_calls)
        if self.tokens >= self.max_tokens:
            raise CapExceeded("token cap reached (%d tokens)" % self.tokens)
        if self.cost >= self.max_cost:
            raise CapExceeded("cost cap reached ($%.4f of $%.2f)" % (self.cost, self.max_cost))
        if self.remaining_s() <= 5:
            raise CapExceeded("run deadline reached")

    def charge(self, rep):
        self.calls += 1
        self.tokens += int(rep.get("tokens_in") or 0) + int(rep.get("tokens_out") or 0)
        if rep.get("cost_usd") is None:
            self.cost_unknown_calls += 1
        else:
            self.cost += float(rep["cost_usd"])

    def over(self):
        """A reason string when the last charge pushed the run past a cap, else None."""
        if self.tokens > self.max_tokens:
            return "token cap exceeded (%d > %d)" % (self.tokens, self.max_tokens)
        if self.cost > self.max_cost:
            return "cost cap exceeded ($%.4f > $%.2f)" % (self.cost, self.max_cost)
        return None

    def summary(self):
        return {"calls": self.calls, "tokens": self.tokens, "cost_usd": round(self.cost, 6),
                "cost_unknown_calls": self.cost_unknown_calls,
                "caps": {"calls": self.max_calls, "tokens": self.max_tokens, "cost_usd": self.max_cost}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Model APIs are never followed through a redirect: urllib would re-send the Authorization header to the new
    location (review D-a#2)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method, url, headers, body, timeout):
    """-> (status, bytes). Error bodies are never read into messages; redirects are not followed."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read(MAX_OUTPUT + 1)
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BackendError("network error (%s)" % type(e).__name__) from None


def _path_get(obj, path):
    """Dotted JSON path; a segment ending in [] maps over a list and joins strings ("result.payloads[].text")."""
    cur = [obj]
    for seg in path.split("."):
        many = seg.endswith("[]")
        key = seg[:-2] if many else seg
        nxt = []
        for c in cur:
            v = c.get(key) if isinstance(c, dict) else None
            if many:
                nxt.extend(v if isinstance(v, list) else [])
            elif v is not None:
                nxt.append(v)
        cur = nxt
    if not cur:
        return None
    if len(cur) == 1:
        return cur[0]
    if all(isinstance(x, str) for x in cur):
        return "".join(cur)
    return cur


def _first(obj, paths):
    for p in paths:
        v = _path_get(obj, p)
        if v not in (None, "", [], {}):
            return v
    return None


def _nonneg_int(v):
    try:
        x = int(v or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(x, 10_000_000))


# QA minor: when OpenRouter does not report usage.cost, the dollar cap still counts the call, at the configured
# price_per_mtok or at this deliberately high default (USD per million tokens), so the cap can only trip early, never late.
CONSERVATIVE_PRICE = {"in": 15.0, "out": 75.0}


def _cost(b, usage):
    c = usage.get("cost") if isinstance(usage, dict) else None
    if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c < 1e6:
        return float(c)
    price = b.get("price_per_mtok") or (CONSERVATIVE_PRICE if b.get("kind") == "openrouter" else None)
    if price and isinstance(usage, dict):
        return (_nonneg_int(usage.get("prompt_tokens")) * price["in"] + _nonneg_int(usage.get("completion_tokens"))
                * price["out"]) / 1e6
    return None


def _call_http(b, system, user, budget, http, sleep):
    key = os.environ.get(b["key_env"], "")
    if not key:
        raise BackendError("environment variable %s is not set" % b["key_env"])
    body = {"model": b["model"], "temperature": b["temperature"], "max_tokens": b["max_tokens"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if b["kind"] == "openrouter":
        body["usage"] = {"include": True}   # OpenRouter reports the real cost of the call
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key, "User-Agent": "adrails-content"}
    if b["kind"] == "openrouter":
        headers["X-Title"] = "AdRails"
    timeout = max(5.0, min(float(b["timeout_s"]), budget.remaining_s()))
    status, raw = 0, b""
    for attempt in range(b["retries"] + 1):
        try:
            status, raw = http("POST", b["url"], headers, data, timeout)
        except BackendError:
            if attempt >= b["retries"] or budget.remaining_s() < 30:
                raise
            sleep(min(20, 2 ** attempt * 2))
            continue
        if status == 429 or status >= 500:
            if attempt < b["retries"] and budget.remaining_s() > 30:
                budget.calls += 1                         # a retry is a request: it counts toward max_calls
                budget.check()
                sleep(min(20, 2 ** attempt * 2))
                continue
        break
    del key, headers
    if status != 200:
        raise BackendError("HTTP %d from %s" % (status, b["name"]))
    if len(raw) > MAX_OUTPUT:
        raise BackendError("response too large")
    try:
        d = json.loads(raw)
        choice = d["choices"][0]
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise BackendError("unparseable response from %s" % b["name"]) from None
    usage = d.get("usage") if isinstance(d.get("usage"), dict) else {}
    model = d.get("model") if isinstance(d.get("model"), str) else None
    return {"text": text if isinstance(text, str) else "", "model_answered": model[:128] if model else None,
            "provider": scrub(d["provider"], 60) if isinstance(d.get("provider"), str) else b["kind"],
            "finish": str(choice.get("finish_reason") or "")[:20].lower() or None, "fallback_used": False,
            "tokens_in": _nonneg_int(usage.get("prompt_tokens")), "tokens_out": _nonneg_int(usage.get("completion_tokens")),
            "cost_usd": _cost(b, usage), "tools_used": []}


_SECRETISH = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)


def _child_env(secret_envs, keep=()):
    """The command backend's environment: everything a CLI login needs (HOME, PATH, its own config vars) minus every
    configured model key env and anything that looks like a secret, unless the operator lists it in pass_env."""
    env = dict(os.environ)
    for k in list(env):
        if k in keep:
            continue
        if k in secret_envs or k == "ADRAILS_APPROVAL_SECRET" or _SECRETISH.search(k):
            env.pop(k, None)
    return env


def _call_command(b, system, user, budget, run_cmd, secret_envs):
    prompt = "SYSTEM INSTRUCTIONS:\n" + system + "\n\nTASK:\n" + user
    fd, pf = tempfile.mkstemp(prefix="adrails-prompt-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        os.chmod(pf, 0o600)
        uses_file = any("{prompt_file}" in x for x in b["cmd"])
        nonce = "adrails-%d-%s" % (int(time.time()), os.urandom(3).hex())     # "{nonce}": a fresh session id per call
        argv = [x.replace("{prompt_file}", pf).replace("{nonce}", nonce) for x in b["cmd"]]
        timeout = max(5.0, min(float(b["timeout_s"]), budget.remaining_s()))
        try:
            rc, out = run_cmd(argv, None if uses_file else prompt, timeout, b.get("cwd"),
                              _child_env(secret_envs, b.get("pass_env") or ()))
        except subprocess.TimeoutExpired:
            raise BackendError("command timed out after %ds" % int(timeout)) from None
        except OSError as e:
            raise BackendError("command could not start (%s)" % type(e).__name__) from None
    finally:
        try:
            os.unlink(pf)
        except OSError:
            pass
    if len(out) > MAX_OUTPUT:
        raise BackendError("command output too large")
    try:
        d = json.loads(out)
    except ValueError:
        raise BackendError("command output is not JSON (rc=%s)" % rc) from None
    text = _first(d, b["text_paths"])
    model = _first(d, b["model_paths"])
    fb = _first(d, b["fallback_paths"]) if b["fallback_paths"] else None
    tools = _first(d, b["tools_paths"]) if b["tools_paths"] else None
    if rc != 0:
        raise BackendError("command exited rc=%s" % rc)
    return {"text": text if isinstance(text, str) else "", "model_answered": model if isinstance(model, str) else None,
            "provider": "command", "finish": None, "fallback_used": bool(fb),
            "tools_used": tools if isinstance(tools, list) else ([] if not tools else [str(tools)]),
            "tokens_in": 0, "tokens_out": 0, "cost_usd": _cost(b, {})}


def _run_cmd(argv, stdin_text, timeout, cwd, env):
    p = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
                       shell=False)
    return p.returncode, p.stdout or ""


def verify(b, rep, exclude_family=None):
    """Raise Refused unless the reply came from the requested model of an allowed family, with no fallback or tools."""
    ans = rep.get("model_answered")
    if not ans:
        raise Refused("%s did not report which model answered: unverifiable, refused" % b["name"])
    if rep.get("fallback_used"):
        raise Refused("%s reports a fallback model was used (%s): refused" % (b["name"], scrub(str(ans), 120)))
    if rep.get("tools_used"):
        raise Refused("%s used tools during the call: refused" % b["name"])
    if not model_matches(b["model"], ans):
        raise Refused("silent fallback: %s asked for %s but %s answered: refused" % (b["name"], b["model"], scrub(str(ans), 120)))
    fam = family_of(ans) or (b.get("family") if _bare(ans) == _bare(b["model"]) else None)
    if not fam:
        raise Refused("%s: cannot tell the family of answering model %s: refused" % (b["name"], scrub(str(ans), 120)))
    if exclude_family and fam == exclude_family:
        raise Refused("%s answered with %s, the writer's family (%s): refused" % (b["name"], scrub(str(ans), 120), fam))
    return fam


def call(b, system, user, budget, *, exclude_family=None, http=None, run_cmd=None, sleep=time.sleep, secret_envs=()):
    """One verified call. -> reply dict. Raises BackendError / Refused / CapExceeded."""
    if not isinstance(system, str) or not isinstance(user, str) or len(system) + len(user) > MAX_PROMPT:
        raise BackendError("prompt missing or larger than %d chars" % MAX_PROMPT)
    if exclude_family and backend_family(b) == exclude_family:
        raise Refused("%s (%s) is the writer's family: refused" % (b["name"], b["model"]))
    budget.check()
    t0 = time.monotonic()
    try:
        if b["kind"] == "command":
            rep = _call_command(b, system, user, budget, run_cmd or _run_cmd, set(secret_envs))
        else:
            rep = _call_http(b, system, user, budget, http or _http, sleep)
    except BackendError:
        budget.calls += 1
        raise
    rep.update({"backend": b["name"], "kind": b["kind"], "model_requested": b["model"],
                "ms": int((time.monotonic() - t0) * 1000)})
    budget.charge(rep)
    rep["family"] = verify(b, rep, exclude_family)
    if rep.get("finish") == "length":
        raise BackendError("%s output truncated at max_tokens (finish=length)" % b["name"])
    if not rep["text"].strip():
        raise BackendError("%s returned an empty answer" % b["name"])
    over = budget.over()
    if over:
        raise CapExceeded(over)
    return rep


def public(rep):
    """The reply metadata safe to store (no text)."""
    return {k: rep.get(k) for k in ("backend", "kind", "model_requested", "model_answered", "family", "provider",
                                    "tokens_in", "tokens_out", "cost_usd", "finish", "ms")}


def call_chain(backends, system, user, budget, *, exclude_family=None, accept=None, **kw):
    """Try each configured backend in order until one gives a verified reply that `accept(text)` (optional) takes.
    -> (reply, attempts). A configured chain is an explicit, recorded fallback; a provider-side swap is refused.
    Raises CapExceeded at once; raises BackendError("all ...") when every backend failed (attempts in .attempts)."""
    attempts = []
    for b in backends:
        try:
            rep = call(b, system, user, budget, exclude_family=exclude_family, **kw)
            if accept is not None:
                why = accept(rep["text"])
                if why:
                    raise BackendError("%s: %s" % (b["name"], why))
            attempts.append(dict(public(rep), ok=True))
            return rep, attempts
        except CapExceeded as e:
            attempts.append({"backend": b["name"], "model_requested": b["model"], "ok": False, "error": scrub(str(e), 300)})
            e.attempts = attempts
            raise
        except (BackendError, Refused) as e:
            attempts.append({"backend": b["name"], "model_requested": b["model"], "ok": False,
                             "refused": isinstance(e, Refused), "error": scrub(str(e), 300)})
    err = BackendError("all %d backends failed: %s" % (len(backends), "; ".join(a["error"] for a in attempts)[:900]))
    err.attempts = attempts
    raise err


def secret_env_names(*chains):
    return sorted({b["key_env"] for ch in chains for b in (ch or []) if b and b.get("key_env")})


# ── selftest ────────────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    n = 0

    def ck(cond, msg):
        nonlocal n
        assert cond, "writers.selftest: " + msg
        n += 1

    def raises(fn, exc, msg):
        try:
            fn()
        except exc:
            return ck(True, msg)
        ck(False, msg + " (no %s)" % exc.__name__)

    ck(family_of("openai/gpt-5") == "openai" and family_of("gpt-6-astra") == "openai", "openai family")
    ck(family_of("z-ai/glm-4.6") == "zhipu" and family_of("glm-5.3") == "zhipu", "glm family")
    ck(family_of("deepseek-flash") == "deepseek" and family_of("deepseek/deepseek-chat") == "deepseek", "deepseek")
    ck(family_of("anthropic/claude-sonnet-4.5") == "anthropic" and family_of("google/gemini-2.5-pro") == "google", "a/g")
    ck(family_of("perplexity/sonar") is None and family_of("") is None and family_of(None) is None, "unknown family")
    ck(model_matches("openai/gpt-5", "openai/gpt-5") and model_matches("gpt-5", "gpt-5-2025-08-07"), "match + date")
    ck(model_matches("glm-5.3", "glm-5.3") and model_matches("openai/gpt-6-astra", "gpt-6-astra"), "bare vs vendor")
    ck(not model_matches("openai/gpt-5", "openai/gpt-5-mini"), "gpt-5-mini is not gpt-5")
    ck(not model_matches("openai/gpt-5", "anthropic/claude-sonnet-4.5"), "other model is a fallback")
    ck(not model_matches("anthropic/claude-x", "openai/claude-x"), "vendor swap refused")
    ck(not model_matches("glm-5.3", None) and not model_matches("glm-5.3", ""), "no answered model")

    glm = {"name": "glm", "kind": "openai_compatible", "base_url": "https://api.z.ai/api/coding/paas/v4",
           "key_env": "ZAI_TEST_KEY", "model": "glm-5.3"}
    ds = {"name": "ds", "kind": "openai_compatible", "base_url": "https://api.deepseek.com/v1",
          "key_env": "DS_TEST_KEY", "model": "deepseek-flash"}
    orr = {"name": "or-review", "kind": "openrouter", "model": "openai/gpt-5", "key_env": "OR_TEST_KEY"}
    cmd = {"name": "astra", "kind": "command", "model": "openai/gpt-6-astra",
           "cmd": ["openclaw", "agent", "--json", "--message-file", "{prompt_file}"],
           "text_paths": ["result.payloads[].text"], "model_paths": ["result.meta.executionTrace.winnerModel"],
           "fallback_paths": ["result.meta.executionTrace.fallbackUsed"], "tools_paths": ["result.meta.tools"]}
    ws, rs, ed = validate_chains([glm, ds], [orr, cmd])
    ck(ws[0]["url"].endswith("/chat/completions") and rs[0]["url"] == OPENROUTER_URL, "urls normalised")
    raises(lambda: validate_chains([glm], [dict(glm, name="glm2")]), Refused, "same-family reviewer refused at config")
    raises(lambda: validate_chains([glm, ds], [dict(ds, name="ds2")]), Refused, "reviewer = backup writer family refused")
    raises(lambda: validate_chains([glm], []), ToolError, "no reviewer refused")
    raises(lambda: validate_chains([glm], [orr], editor=dict(glm, name="ed")), Refused, "same-family editor refused")
    raises(lambda: validate_backend(dict(glm, key_env="sk-abc123")), ToolError, "key value in key_env refused")
    raises(lambda: validate_backend(dict(glm, base_url="http://api.example.com")), ToolError, "http base_url refused")
    ck(validate_backend(dict(glm, base_url="http://127.0.0.1:9/v1"))["url"].startswith("http://127.0.0.1"), "loopback ok")
    raises(lambda: validate_backend(dict(cmd, cmd=["x", "{prompt}"])), ToolError, "prompt in argv refused")
    raises(lambda: validate_backend(dict(cmd, model_paths=[])), ToolError, "command without model path refused")
    raises(lambda: validate_backend(dict(glm, model="perplexity/sonar")), ToolError, "unknown family needs declaration")
    ck(validate_backend(dict(glm, model="perplexity/sonar", family="perplexity"))["family"] == "perplexity", "declared")
    raises(lambda: validate_backend(dict(glm, family="openai")), ToolError, "contradicting family refused")

    os.environ["ZAI_TEST_KEY"] = "zk-test-0000"
    os.environ["OR_TEST_KEY"] = "or-test-1111"
    seen = []

    def fake_http(replies):
        def h(method, url, headers, body, timeout):
            seen.append((url, headers.get("Authorization"), json.loads(body)))
            r = replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r[0], json.dumps(r[1]).encode()
        return h

    def ok(model, text="draft", finish="stop", cost=None, tokens=(100, 200)):
        u = {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}
        if cost is not None:
            u["cost"] = cost
        return 200, {"model": model, "choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": u}

    bud = Budget({"max_calls": 5, "max_cost_usd": 1.0, "max_tokens": 10000})
    rep = call(ws[0], "s", "u", bud, http=fake_http([ok("glm-5.3")]))
    ck(rep["family"] == "zhipu" and rep["model_answered"] == "glm-5.3" and bud.calls == 1, "verified writer call")
    ck(seen[-1][1] == "Bearer zk-test-0000" and "ZAI" not in json.dumps(public(rep)), "key only in the header")
    raises(lambda: call(ws[0], "s", "u", Budget(), http=fake_http([ok("glm-4.5-air")])), Refused, "silent fallback refused")
    raises(lambda: call(ws[0], "s", "u", Budget(), http=fake_http([(200, {"choices": [{"message": {"content": "x"}}]})])),
           Refused, "no model in response refused")
    raises(lambda: call(ws[0], "s", "u", Budget(), http=fake_http([ok("glm-5.3", finish="length")])), BackendError,
           "truncated output is an error")
    raises(lambda: call(rs[0], "s", "u", Budget(), exclude_family="openai", http=fake_http([ok("openai/gpt-5")])),
           Refused, "reviewer of the writer family refused before the call")
    raises(lambda: call(dict(rs[0], model="anthropic/claude-sonnet-4.5"), "s", "u", Budget(), exclude_family="zhipu",
                        http=fake_http([ok("z-ai/glm-4.6")])), Refused, "answered in the writer family refused")
    b2 = Budget({"max_cost_usd": 0.05})
    rep = call(rs[0], "s", "u", b2, exclude_family="zhipu", http=fake_http([ok("openai/gpt-5", cost=0.01)]))
    ck(abs(b2.cost - 0.01) < 1e-9 and seen[-1][2]["usage"] == {"include": True}, "openrouter cost counted")
    raises(lambda: call(rs[0], "s", "u", b2, exclude_family="zhipu", http=fake_http([ok("openai/gpt-5", cost=0.08)])),
           CapExceeded, "cost cap exceeded after the call")
    raises(lambda: call(rs[0], "s", "u", b2, http=fake_http([ok("openai/gpt-5")])), CapExceeded, "no calls past the cap")
    b3 = Budget({"max_calls": 1})
    call(ws[0], "s", "u", b3, http=fake_http([ok("glm-5.3")]))
    raises(lambda: call(ws[0], "s", "u", b3, http=fake_http([ok("glm-5.3")])), CapExceeded, "call cap")
    sleeps = []
    rep = call(ws[0], "s", "u", Budget(), http=fake_http([(429, {}), ok("glm-5.3")]), sleep=sleeps.append)
    ck(rep["text"] == "draft" and sleeps == [2], "429 retried once")
    raises(lambda: call(ws[0], "s", "u", Budget(), http=fake_http([(500, {}), (500, {})]), sleep=sleeps.append),
           BackendError, "retries capped")
    os.environ.pop("DS_TEST_KEY", None)
    raises(lambda: call(ws[1], "s", "u", Budget(), http=fake_http([])), BackendError, "missing key env")
    try:
        call(ws[1], "s", "u", Budget(), http=fake_http([]))
    except BackendError as e:
        ck("DS_TEST_KEY" in str(e) and "zk-" not in str(e), "error names the env, never a value")
    os.environ["DS_TEST_KEY"] = "ds-test-2222"
    rep, att = call_chain(ws, "s", "u", Budget(), http=fake_http([ok("glm-5.3", finish="length"), ok("deepseek-flash")]))
    ck(rep["backend"] == "ds" and att[0]["ok"] is False and "truncated" in att[0]["error"], "chain falls to backup")
    try:
        call_chain(ws, "s", "u", Budget(), http=fake_http([ok("glm-x"), ok("gpt-4o")]))
        ck(False, "all refused must raise")
    except BackendError as e:
        ck(len(e.attempts) == 2 and all(a["refused"] for a in e.attempts), "every refusal recorded")
    rep, att = call_chain(ws, "s", "u", Budget(), accept=lambda t: None if t.startswith("{") else "not JSON",
                          http=fake_http([ok("glm-5.3", text="oops"), ok("deepseek-flash", text="{}")]))
    ck(rep["backend"] == "ds", "accept() rejects unusable text and moves on")

    # command backend: prompt via 0600 file, keys stripped from the child env, model read back, fallback refused
    got = {}

    def fake_run(answer, rc=0):
        def r(argv, stdin_text, timeout, cwd, env):
            pf = argv[-1]
            got.update(argv=argv, stdin=stdin_text, mode=oct(os.stat(pf).st_mode & 0o777),
                       prompt=open(pf, encoding="utf-8").read(), env_keys=set(env))
            return rc, json.dumps(answer)
        return r

    trace = {"winnerModel": "gpt-6-astra", "fallbackUsed": False}
    ans = {"result": {"payloads": [{"text": "{\"verdict\": "}, {"text": "\"approve\"}"}], "meta": {"executionTrace": trace}}}
    rep = call(rs[1], "SYS", "USER", Budget(), exclude_family="zhipu", run_cmd=fake_run(ans),
               secret_envs=["ZAI_TEST_KEY", "OR_TEST_KEY"])
    ck(rep["text"] == "{\"verdict\": \"approve\"}" and rep["family"] == "openai", "command reply joined + verified")
    ck(got["mode"] == "0o600" and "USER" in got["prompt"] and got["stdin"] is None, "prompt via 0600 file, not stdin")
    ck(all("USER" not in a for a in got["argv"]) and not os.path.exists(got["argv"][-1]), "not in argv; file removed")
    ck("ZAI_TEST_KEY" not in got["env_keys"] and "OR_TEST_KEY" not in got["env_keys"], "API keys not passed to command")
    fb = {"result": {"payloads": [{"text": "x"}], "meta": {"executionTrace": dict(trace, fallbackUsed=True)}}}
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=fake_run(fb)), Refused, "command fallback refused")
    other = {"result": {"payloads": [{"text": "x"}], "meta": {"executionTrace": dict(trace, winnerModel="glm-5.3")}}}
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=fake_run(other)), Refused, "command answered by another model")
    tools = {"result": {"payloads": [{"text": "x"}], "meta": {"executionTrace": trace, "tools": ["exec"]}}}
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=fake_run(tools)), Refused, "tool use refused")
    nomodel = {"result": {"payloads": [{"text": "x"}], "meta": {}}}
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=fake_run(nomodel)), Refused, "unverifiable command refused")

    def notjson(argv, stdin_text, timeout, cwd, env):
        return 0, "plain text"
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=notjson), BackendError, "non-JSON command output")
    sb = dict(cmd, cmd=["cat"])
    sb = validate_backend(sb, "reviewer")
    rep_stdin = {}

    def stdin_run(argv, stdin_text, timeout, cwd, env):
        rep_stdin["stdin"] = stdin_text
        return 0, json.dumps(ans)
    call(sb, "s", "PROMPT-X", Budget(), run_cmd=stdin_run)
    ck("PROMPT-X" in (rep_stdin["stdin"] or ""), "stdin used when no {prompt_file}")
    ck(secret_env_names(ws, rs) == ["DS_TEST_KEY", "OR_TEST_KEY", "ZAI_TEST_KEY"], "secret env names")
    # review D-a fixes
    neg = (200, {"model": "glm-5.3", "choices": [{"message": {"content": "x"}, "finish_reason": "LENGTH"}],
                 "usage": {"prompt_tokens": -5000, "completion_tokens": "x"}})
    raises(lambda: call(ws[0], "s", "u", Budget(), http=fake_http([neg])), BackendError, "LENGTH (any case) = truncated")
    b4 = Budget()
    call(ws[0], "s", "u", b4, http=fake_http([(200, dict(neg[1], choices=[{"message": {"content": "x"},
                                                                         "finish_reason": "stop"}]))]))
    ck(b4.tokens == 0, "negative / junk usage never lowers the token count")

    def failing(argv, stdin_text, timeout, cwd, env):
        return 1, json.dumps(ans)
    raises(lambda: call(rs[1], "s", "u", Budget(), run_cmd=failing), BackendError, "non-zero exit is an error even with text")
    os.environ["SOME_SERVICE_TOKEN"] = "tok-x"
    os.environ["OPENCLAW_PROFILE"] = "p"
    call(rs[1], "s", "u", Budget(), run_cmd=fake_run(ans))
    ck("SOME_SERVICE_TOKEN" not in got["env_keys"] and "OPENCLAW_PROFILE" in got["env_keys"], "secret-looking env stripped")
    pe = validate_backend(dict(cmd, pass_env=["SOME_SERVICE_TOKEN"]), "reviewer")
    call(pe, "s", "u", Budget(), run_cmd=fake_run(ans))
    ck("SOME_SERVICE_TOKEN" in got["env_keys"], "pass_env keeps an operator-listed var")
    raises(lambda: validate_backend(dict(cmd, pass_env=["ADRAILS_APPROVAL_SECRET"])), ToolError, "approval secret never passed")
    raises(lambda: validate_backend(dict(glm, key_env="ADRAILS_APPROVAL_SECRET")), ToolError, "approval secret is not a key")
    os.environ.pop("SOME_SERVICE_TOKEN", None)
    os.environ.pop("OPENCLAW_PROFILE", None)
    b5 = Budget({"max_calls": 2})
    raises(lambda: call(ws[0], "s", "u", b5, http=fake_http([(500, {}), (500, {})]), sleep=lambda s: None),
           ToolError, "retries count toward max_calls")
    ck(isinstance(_OPENER, urllib.request.OpenerDirector), "model calls use the no-redirect opener")
    for k in ("ZAI_TEST_KEY", "OR_TEST_KEY", "DS_TEST_KEY"):
        os.environ.pop(k, None)
    os.environ["OR_TEST_KEY"] = "or-test-1111"
    b6 = Budget({"max_cost_usd": 0.05})
    raises(lambda: call(rs[0], "s", "u", b6, http=fake_http([ok("openai/gpt-5", tokens=(1000, 1000))])), CapExceeded,
           "OpenRouter without usage.cost is charged at a conservative price, so the dollar cap still trips (QA)")
    ck(b6.cost_unknown_calls == 0 and b6.cost > 0.05, "estimated cost counted")
    os.environ.pop("OR_TEST_KEY", None)
    return n
