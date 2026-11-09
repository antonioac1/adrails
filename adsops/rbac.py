"""Config-driven role-based access control for AdRails MCP tools.

Deny-by-default once enabled: a tool call is permitted only if the calling
principal's roles contribute an fnmatch glob pattern matching the tool name.
Missing or disabled config allows everything, so existing deployments keep
working unchanged (backward compatible). Unknown principals, and a missing
principal, resolve to zero roles and therefore zero tools.
Safety: no I/O and no import-time side effects; refusals raise RailViolation
with scrubbed text (no tokens/PII leak) and happen *before* the tool body runs,
so a denied call has no side effects. Typos in config raise ToolError early.
Tools use ``rbac.check(tool_name)`` or ``wrap(fn, tool_name, rbac)``.
"""

import fnmatch

try:  # package import (normal case)
    from adsops.errors import ToolError, RailViolation, scrub
except Exception:  # pragma: no cover - lets this file run standalone for selftest
    class ToolError(Exception):
        """Bad input / user-visible failure."""

    class RailViolation(Exception):
        """A safety rail refused the operation."""

    def scrub(text, limit=1500):  # noqa: D103 - minimal fallback
        return str(text)[:limit]


# --- built-in role pattern tables -------------------------------------------
# Keep these as tuples (immutable) so a caller cannot mutate the defaults.
VIEWER = (
    "adsops_campaigns",
    "adsops_status",
    "adsops_terms",
    "adsops_usage",
    "adsops_*_report",
    "adsops_*diagnos*",
    "adsops_audit*",
    "adsops_history",
    # AEO reads only: aeo_indexnow pings external indexes, so it is an OPERATOR tool (the maintainers review 2026-11-05)
    "aeo_site_audit",
    "aeo_page_audit",
    "aeo_llms_txt",
    "aeo_schema_generate",
    "aeo_visibility_record",
    "aeo_ai_traffic",
    "aeo_content_gaps",
    "adsops_whoami",
    "adsops_sbom",
    "adsops_approvals",
    # direct-mail analysis (reads local files; mail_scans keeps a local state file)
    "mail_qr",
    "mail_scans",
    "mail_response",
    "mail_holdout_plan",
    "mail_compare",
    "mail_breakeven",
    "channel_cac",
    # ChatGPT Ads (OpenAI Ads) reads
    "adsops_openai_account",
    "adsops_openai_status",
    "adsops_openai_insights",
    "adsops_openai_geo_lookup",
    "adsops_openai_plan",
    "adsops_openai_ad_check",
    "adsops_openai_ab_report",
    "adsops_competitors_report",
    "adsops_lp_diagnose",
    "adsops_negatives_suggest",
    "adsops_monitor_check",
    "adsops_brief",
    "adsops_fleet",
    "adsops_rsa_validate",
    "adsops_keyword_ideas",
    "adsops_rules_check",
    "adsops_realloc_suggest",
    "adsops_drift",
    "adsops_conv_audit",
    "adsops_leads_due",
    "adsops_local_schema",
    "adsops_local_nap",
    "adsops_local_area_page",
    "adsops_reviews",
    "adsops_lsa_report",
    "adsops_lsa_readiness",
    "adsops_update_check",
    "adsops_playbook",
    "adsops_results",
    "adsops_gbp",
    # 0.10 measurement reads (local store + read-only Search Console)
    "adsops_metrics",
    "adsops_search_console",
    "adsops_impact",
)

OPERATOR_EXTRA = (
    "adsops_metrics_pull",   # calls Google APIs (quota) and writes the local store
    "adsops_plan",
    "adsops_apply",
    "adsops_set",
    "adsops_rollback",
    "adsops_approval_request",
    "adsops_offline_conv_plan",
    "aeo_indexnow",
    "mail_list_clean",
    "mail_codes_assign",
    "mail_labels",
    "adsops_openai_apply",
    "adsops_openai_upload_image",
    "adsops_openai_brand_update",
    "adsops_openai_conversions_setup",
    "adsops_openai_test_event",
    "adsops_openai_draft_campaign",
    "adsops_openai_send_conversions",
    "adsops_openai_audience",
    "adsops_local_profile",
    "adsops_local_plan",
    "adsops_leads",
    "adsops_lsa_leads",
    "adsops_lsa_dispute",
    "adsops_paid",          # creates quotes / reads results; it never pays (the owner or an owner-budgeted wallet does)
)

BUILTIN_ROLES = {
    "viewer": VIEWER,
    "operator": VIEWER + OPERATOR_EXTRA,
    "approver": VIEWER + ("adsops_approval_decide",),
    "guard": VIEWER + ("adsops_guard", "adsops_openai_guard"),
    "admin": ("*",),
}


def _norm_patterns(value, role_name):
    """Validate/coerce one role's pattern list; raises ToolError on garbage."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ToolError(f"rbac role '{role_name}' must be a list of glob patterns")
    out = []
    for pat in value:
        if not isinstance(pat, str) or not pat.strip():
            raise ToolError(f"rbac role '{role_name}' contains a non-string/empty pattern")
        out.append(pat.strip())
    if not out:
        raise ToolError(f"rbac role '{role_name}' has no patterns (would allow nothing)")
    return tuple(out)


class Rbac:
    """Resolve a principal's roles and answer 'may this principal call tool X?'."""

    def __init__(self, cfg_rbac=None, principal=None):
        # Accept None (no RBAC block) or a dict; anything else is a config bug.
        if cfg_rbac is None:
            self._cfg = {}
        elif isinstance(cfg_rbac, dict):
            self._cfg = dict(cfg_rbac)
        else:
            raise ToolError("rbac config must be an object/dict or None")

        self.enabled = bool(self._cfg.get("enabled", False))

        # Explicit argument wins over cfg["principal"] (caller passes env value).
        p = principal if principal is not None else self._cfg.get("principal")
        if p is not None and not isinstance(p, str):
            raise ToolError("rbac principal must be a string")
        self.principal = (p.strip() or None) if isinstance(p, str) else None

        # Role table = builtins overlaid by cfg["roles"] (full replacement per role).
        overrides = self._cfg.get("roles") or {}
        if not isinstance(overrides, dict):
            raise ToolError("rbac 'roles' must be an object of role -> patterns")
        table = dict(BUILTIN_ROLES)
        for name, pats in overrides.items():
            if not isinstance(name, str) or not name.strip():
                raise ToolError("rbac role names must be non-empty strings")
            table[name.strip()] = _norm_patterns(pats, name.strip())

        principals = self._cfg.get("principals") or {}
        if not isinstance(principals, dict):
            raise ToolError("rbac 'principals' must be an object of principal -> roles")

        # Resolve this principal's roles (deduped, order preserved).
        raw = principals.get(self.principal, []) if self.principal else []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            raise ToolError(f"rbac roles for principal '{self.principal}' must be a list")
        roles, seen = [], set()
        for role in raw:
            if not isinstance(role, str) or not role.strip():
                raise ToolError(f"rbac principal '{self.principal}' has a non-string role")
            role = role.strip()
            if role not in table:
                # Disabled config stays permissive/backward compatible: ignore typos.
                if self.enabled:
                    raise ToolError(f"unknown rbac role '{role}' for principal '{self.principal}'")
                continue
            if role not in seen:
                seen.add(role)
                roles.append(role)
        self.roles = tuple(roles)

        # Flattened glob patterns for this principal (deduped, order preserved).
        pats, seen_p = [], set()
        for role in self.roles:
            for pat in table[role]:
                if pat not in seen_p:
                    seen_p.add(pat)
                    pats.append(pat)
        self._patterns = tuple(pats)

    # -- queries ------------------------------------------------------------
    def allowed(self, tool) -> bool:
        """True if this principal may call `tool`. Disabled config -> always True."""
        if not self.enabled:
            return True
        if not isinstance(tool, str) or not tool:
            return False  # empty/non-string tool names are never callable
        if not self.roles:
            return False  # unknown / missing principal -> deny everything
        for pat in self._patterns:
            if fnmatch.fnmatchcase(tool, pat):  # case-sensitive on purpose
                return True
        return False

    def check(self, tool) -> bool:
        """Raise RailViolation if `tool` is not permitted; else return True."""
        if self.allowed(tool):
            return True
        roles = ",".join(self.roles) if self.roles else "none"
        who = self.principal or "<unset>"
        # scrub() defensively: config text must never carry a secret outward.
        raise RailViolation(scrub(f"principal '{who}' (roles: {roles}) may not call {tool}", limit=300))

    def describe(self) -> dict:
        """JSON-safe summary for adsops_status / debugging."""
        return {
            "enabled": self.enabled,
            "principal": self.principal,
            "roles": list(self.roles),
            "allowed_patterns": sorted(self._patterns) if self.enabled else ["*"],
        }

    @classmethod
    def from_config(cls, cfg, principal=None, env=None):
        """Build from a whole server config dict; env may supply ADRAILS_PRINCIPAL."""
        rbac_cfg = cfg.get("rbac") if isinstance(cfg, dict) else None
        if principal is None and isinstance(env, dict):
            principal = env.get("ADRAILS_PRINCIPAL") or None
        return cls(rbac_cfg, principal)


def wrap(fn, tool_name, rbac):
    """Return `args -> fn(args)` that runs rbac.check(tool_name) first.

    The check happens before any work, so a refused call has zero side effects.
    `rbac=None` means "no RBAC configured" and calls straight through.
    """

    def guarded(args=None):
        if rbac is not None:
            rbac.check(tool_name)
        return fn(args)

    # Preserve introspection without requiring fn to be a real function.
    guarded.__name__ = getattr(fn, "__name__", "guarded")
    guarded.__doc__ = getattr(fn, "__doc__", None)
    guarded.__wrapped__ = fn
    return guarded


# ---------------------------------------------------------------------------
def selftest() -> int:
    """Exercise RBAC happy paths, wildcards, overrides and refusals. No I/O."""
    checks = 0

    def ok(cond, msg):
        nonlocal checks
        checks += 1
        if not cond:
            raise AssertionError(f"check {checks} failed: {msg}")

    base = {
        "lane-a": ["admin"],
        "grokbot": ["viewer"],
        "ops": ["operator"],
        "appr": ["approver"],
        "grd": ["guard"],
    }

    # 1-2: missing / disabled config is fully backward compatible.
    r = Rbac(None, None)
    ok(r.allowed("adsops_apply") and not r.enabled, "missing config allows all")
    ok(Rbac({"enabled": False, "principal": "ghost", "principals": base}, "ghost").allowed("adsops_apply"),
       "disabled config allows unknown principal")

    # 3: viewer reads, including every wildcard form.
    r = Rbac({"enabled": True, "principals": base}, "grokbot")
    ok(r.allowed("adsops_campaigns"), "viewer plain read allowed")
    ok(r.allowed("adsops_search_terms_report"), "viewer '*_report' glob")
    ok(r.allowed("aeo_site_audit") and r.allowed("adsops_audit_log") and not r.allowed("aeo_indexnow"), "viewer aeo reads + audit globs; indexnow is operator")
    ok(r.allowed("adsops_performance_diagnose"), "viewer '*diagnos*' glob")
    ok(not r.allowed("adsops_apply"), "viewer denied write tool")

    # 4: refusal message names principal + tool and is raised, not returned.
    try:
        r.check("adsops_apply")
        ok(False, "check should have raised")
    except RailViolation as exc:
        msg = str(exc)
        ok("grokbot" in msg and "adsops_apply" in msg and len(msg) <= 300, "denial message scrubbed/short")

    # 5-6: operator / approver / guard separation of duties.
    ops = Rbac({"enabled": True, "principals": base}, "ops")
    ok(ops.allowed("adsops_plan") and ops.allowed("adsops_apply") and ops.allowed("adsops_offline_conv_plan"),
       "operator write tools allowed")
    ok(not ops.allowed("adsops_approval_decide"), "operator cannot self-approve")
    appr = Rbac({"enabled": True, "principals": base}, "appr")
    ok(appr.allowed("adsops_approval_decide") and not appr.allowed("adsops_apply"), "approver approves only")
    grd = Rbac({"enabled": True, "principals": base}, "grd")
    ok(grd.allowed("adsops_guard") and not grd.allowed("adsops_set"), "guard role scoped")

    # 7: admin wildcard reaches even unknown tools, but not an empty name.
    adm = Rbac({"enabled": True, "principals": base}, "lane-a")
    ok(adm.allowed("made_up_tool"), "admin wildcard matches anything")
    ok(not adm.allowed("") and not adm.allowed(None), "empty/non-string tool denied")

    # 8: unknown principal -> deny; describe is empty and JSON-safe.
    ghost = Rbac({"enabled": True, "principals": base}, "ghost")
    gd = ghost.describe()
    ok(not ghost.allowed("adsops_campaigns") and gd["roles"] == [] and gd["allowed_patterns"] == [],
       "unknown principal denied")

    # 9: principal falls back to cfg["principal"]; a truly absent one denies.
    ok(Rbac({"enabled": True, "principal": "grokbot", "principals": base}).allowed("adsops_status"),
       "principal taken from config")
    ok(not Rbac({"enabled": True, "principals": base}, None).allowed("adsops_status"),
       "no principal -> deny all")

    # 10: unknown role is a hard config error when enabled, tolerated when disabled.
    try:
        Rbac({"enabled": True, "principals": {"x": ["wizard"]}}, "x")
        ok(False, "unknown role should raise")
    except ToolError:
        ok(True, "unknown role rejected at construction")
    ok(Rbac({"enabled": False, "principals": {"x": ["wizard"]}}, "x").allowed("adsops_apply"),
       "disabled config ignores unknown role")

    # 11: a cfg["roles"] override fully replaces that role's patterns.
    ov = Rbac({"enabled": True, "principals": {"v": ["viewer"]}, "roles": {"viewer": ["adsops_status"]}}, "v")
    ok(ov.allowed("adsops_status") and not ov.allowed("adsops_campaigns"), "role override replaces patterns")

    # 12: multi-role union + describe shape / determinism.
    multi = Rbac({"enabled": True, "principals": {"m": ["viewer", "approver", "viewer"]}}, "m")
    ok(multi.allowed("adsops_campaigns") and multi.allowed("adsops_approval_decide"), "multi-role union")
    d = multi.describe()
    ok(d["enabled"] is True and d["principal"] == "m" and d["roles"] == ["viewer", "approver"], "describe fields")
    ok(d["allowed_patterns"] == sorted(set(d["allowed_patterns"])) and isinstance(d["allowed_patterns"], list),
       "patterns sorted, unique, list")

    # 13: disabled describe advertises the implicit '*'.
    dd = Rbac(None, None).describe()
    ok(dd == {"enabled": False, "principal": None, "roles": [], "allowed_patterns": ["*"]}, "describe when disabled")

    # 14: wrap gates the call before fn runs, and passes args through.
    calls = []
    def fn(args):
        calls.append(args)
        return {"ok": True, "args": args}

    g = wrap(fn, "adsops_apply", ops)
    ok(g({"x": 1}) == {"ok": True, "args": {"x": 1}} and calls == [{"x": 1}], "wrap calls fn when allowed")
    ok(g.__name__ == "fn" and g.__wrapped__ is fn, "wrap preserves introspection")
    denied = wrap(fn, "adsops_apply", Rbac({"enabled": True, "principals": base}, "grokbot"))
    try:
        denied({"y": 2})
        ok(False, "wrap should refuse")
    except RailViolation:
        ok(len(calls) == 1, "wrap refuses before touching fn")

    # 15: malformed configs are rejected loudly, never silently permissive.
    for bad in (["x"], "nope", 42):
        try:
            Rbac(bad, "a")
            ok(False, "non-object rbac config should raise")
        except ToolError:
            ok(True, "non-object rbac config rejected")
    try:
        Rbac({"enabled": True, "principals": ["v"]}, "v")
        ok(False, "principals list should raise")
    except ToolError:
        ok(True, "principals must be an object")

    # 16: case sensitivity (glob matching must not be locale/case folded).
    ok(not Rbac({"enabled": True, "principals": base}, "grokbot").allowed("ADSOPS_CAMPAIGNS"),
       "matching is case-sensitive")

    # 17: from_config wiring with an env-supplied principal.
    fc = Rbac.from_config({"rbac": {"enabled": True, "principals": base}},
                          env={"ADRAILS_PRINCIPAL": "grokbot"})
    ok(fc.allowed("adsops_campaigns") and not fc.allowed("adsops_apply"), "from_config uses env principal")
    ok(Rbac.from_config({}).allowed("anything"), "from_config without rbac block allows all")
    ok(Rbac.from_config({"rbac": {"enabled": True, "principals": base}}, principal="ops").allowed("adsops_apply"),
       "explicit principal beats env")

    return checks


if __name__ == "__main__":
    import sys
    n = selftest()
    print(f"ALL PASS ({n} checks)", file=sys.stderr)
