"""Cost-per-lead / cost-per-client across ad and offline channels on one ruler.

Inputs are plain rows a caller loads from CSV/CRM/Google Ads/ChatGPT Ads exports:
cost rows and lead rows with UTM, tracking-code, self-reported or referrer data.
Everything is pure: no network, files, clock or environment access, and every
external effect (if any) is injectable by the caller.  Attribution is explicit
(first matching rule wins) so a lead is never double-counted, and every ratio is
guarded against a zero denominator.  Money outputs are floats rounded to 2 dp;
micros inputs are converted to currency units.  An MCP tool should call
`attribute()` for single leads, `channel_report()` for the dashboard, and
`breakeven()` / `piece_cost()` for direct-mail planning.
"""

import re
from datetime import date
from urllib.parse import urlsplit

from adsops.errors import ToolError, RailViolation, scrub  # noqa: F401  (public error contract)

_MAIL_RE = re.compile(r"\b(postcard|mail|letter)s?\b", re.I)
_GOOGLE_RE = re.compile(r"google", re.I)
_AI_RE = re.compile(r"\b(chatgpt|openai|perplexity|claude|ai)\b", re.I)
_REFERRAL_RE = re.compile(r"\b(referral|refer|friend|word[-\s]of[-\s]mouth)\b", re.I)

_AI_HOSTS = {"chatgpt.com", "chat.openai.com", "openai.com", "perplexity.ai", "claude.ai", "anthropic.com"}
_MAIL_SOURCES = {"direct_mail", "direct-mail", "mail", "postcard", "post"}
_PAID_MEDIA = {"cpc", "ppc", "paid", "paid_search"}
_DEFAULT_RULES = ("code", "utm", "self_reported", "referrer")

# Friendly aliases so a cost row written "google" joins leads attributed "google_ads".
_CHANNEL_ALIASES = {
    "google": "google_ads", "adwords": "google_ads", "googleads": "google_ads",
    "meta": "meta_ads", "facebook": "meta_ads", "instagram": "meta_ads",
    "mail": "direct_mail", "postcard": "direct_mail",
    "chatgpt": "chatgpt_ads", "openai": "chatgpt_ads",
}


def _g(row, *path, default=None):
    """Nested getter for camelCase REST rows (dicts/lists), returning `default`."""
    cur = row
    for p in path:
        if isinstance(cur, dict):
            if p not in cur:
                return default
            cur = cur[p]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(p)]
            except (TypeError, ValueError, IndexError):
                return default
        else:
            return default
    return default if cur is None else cur


def _norm(v):
    return "" if v is None else str(v).strip()


def _to_float(v, default=0.0):
    if v is None:
        return default
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("$", "").replace("_", "")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _as_date(v):
    if v is None:
        return None
    s = str(v).strip()[:10]
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s


def _date_part(v):
    return _as_date(v) or ""


def _period_bounds(period):
    if period is None:
        return None
    if isinstance(period, str) or not isinstance(period, (list, tuple)) or len(period) != 2:
        raise ToolError("period must be a (start, end) pair of YYYY-MM-DD dates")
    a, b = _as_date(period[0]), _as_date(period[1])
    if a is None or b is None:
        raise ToolError("period dates must be YYYY-MM-DD")
    if a > b:
        raise ToolError("period start must be <= end")
    return (a, b)


def _in_period(d, bounds):
    return bool(d) and bounds[0] <= d <= bounds[1]


def _host(raw):
    s = _norm(raw)
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    try:
        h = urlsplit(s).hostname or ""
    except ValueError:
        return ""
    h = h.lower().strip(".")
    return h[4:] if h.startswith("www.") else h


def _attr_utm(lead):
    src = _norm(lead.get("utm_source")).lower()
    med = _norm(lead.get("utm_medium")).lower()
    if not src and not med:
        return None
    if src in _MAIL_SOURCES or med in _MAIL_SOURCES:
        return "direct_mail"
    if src in {"google", "google_ads", "googleads", "adwords"} and med in _PAID_MEDIA:
        return "google_ads"
    if src in {"chatgpt.com", "chatgpt", "openai", "openai.com"} and med in _PAID_MEDIA:
        return "chatgpt_ads"
    if src in {"chatgpt.com", "chatgpt", "openai", "openai.com", "perplexity",
               "perplexity.ai", "claude.ai", "claude", "anthropic"} and med == "organic":
        return "ai_organic"
    if src in {"facebook", "instagram", "meta", "fb"} and med in _PAID_MEDIA:
        return "meta_ads"
    return None


def _attr_self(lead):
    txt = _norm(lead.get("self_reported"))
    if not txt:
        return None
    if _MAIL_RE.search(txt):
        return "direct_mail"
    if _GOOGLE_RE.search(txt):
        return "google_unknown"
    if _AI_RE.search(txt):
        return "ai_unknown"
    if _REFERRAL_RE.search(txt):
        return "referral"
    return None


def _attr_ref(lead):
    raw = _norm(lead.get("referrer")) or _norm(lead.get("referer"))
    host = _host(raw)
    if not host:
        return None
    if host in _AI_HOSTS or any(host.endswith("." + h) for h in _AI_HOSTS):
        return "ai_organic"
    if host == "google.com" or host.endswith(".google.com") or host.startswith("google."):
        return "google_unknown"
    # Any other referring site is treated as a referral link, not paid media.
    return "referral"


def attribute(lead, mailed_codes=frozenset(), rules=_DEFAULT_RULES):
    """Return (channel, method) for one lead dict; unknown -> ("unattributed", "none").

    Rules are tried in order.  `code` checks tracking_code against the mailed set,
    `utm` inspects utm_source/utm_medium, `self_reported` parses free text and
    `referrer` looks at the referring host.  The first rule yielding wins.
    """
    if not isinstance(lead, dict):
        return ("unattributed", "none")
    if isinstance(mailed_codes, str):
        mailed_codes = (mailed_codes,)
    if isinstance(rules, str):
        rules = (rules,)
    codes = {_norm(c).lower() for c in (mailed_codes or ()) if _norm(c)}
    for rule in (rules or ()):
        r = _norm(rule).lower()
        if r in ("code", "tracking_code", "tracking"):
            tc = _norm(lead.get("tracking_code")).lower()
            if tc and tc in codes:
                return ("direct_mail", "code")
        elif r == "utm":
            ch = _attr_utm(lead)
            if ch:
                return (ch, "utm")
        elif r in ("self_reported", "self-reported", "self"):
            ch = _attr_self(lead)
            if ch:
                return (ch, "self_reported")
        elif r in ("referrer", "referer", "ref"):
            ch = _attr_ref(lead)
            if ch:
                return (ch, "referrer")
    return ("unattributed", "none")


def _row_amount(row):
    raw = _g(row, "amount")
    if raw is not None:
        return _to_float(raw)
    micros = _g(row, "amount_micros")
    if micros is None:
        micros = _g(row, "cost_micros")
    if micros is not None:
        return _to_float(micros) / 1_000_000.0
    return _to_float(_g(row, "cost"))


def _channel_key(name):
    c = _norm(name).lower()
    return _CHANNEL_ALIASES.get(c, c) if c else ""


def _metrics(spend, n_leads, qualified, clients, client_value):
    spend = round(float(spend), 2)
    cpl = round(spend / n_leads, 2) if n_leads else None
    cpql = round(spend / qualified, 2) if qualified else None
    cac = round(spend / clients, 2) if clients else None
    l2c = round(clients / n_leads, 4) if n_leads else None
    roas = round(clients * client_value / spend, 4) if (client_value is not None and spend > 0) else None
    return {
        "spend": spend, "leads": n_leads, "qualified": qualified, "clients": clients,
        "cpl": cpl, "cpql": cpql, "cac": cac, "lead_to_client": l2c, "roas": roas,
    }


def _note(s, min_n):
    spend, leads = s["spend"], s["leads"]
    parts = []
    if spend > 0 and leads == 0:
        parts.append("spend without leads")
    elif leads == 0:
        parts.append("no data")
    else:
        if not s.get("has_cost"):
            parts.append("no cost data")
        if min_n > 0 and leads < min_n:
            parts.append(f"too few to judge (n<{min_n})")
    return "; ".join(parts)


def _rank_key(c):
    cac, cpl = c["cac"], c["cpl"]
    return (cac is None, cac if cac is not None else 0.0,
            cpl is None, cpl if cpl is not None else 0.0, c["channel"])


def channel_report(costs, leads, mailed_codes=(), period=None, client_value=None, min_n=5):
    """Join cost rows and lead rows, then price every channel on the same ruler.

    `costs` rows: {"channel","date","amount"} (or amount_micros/cost_micros).
    `leads` rows: {"id","created_at","stage": lead|qualified|client|lost, ...}.
    `qualified` counts both qualified and client stages; `clients` counts client.
    `unattributed` leads are reported separately and excluded from totals so the
    channel table sums to attributed demand only.
    """
    costs = [] if costs is None else costs
    leads = [] if leads is None else leads
    if not isinstance(costs, (list, tuple)):
        raise ToolError("costs must be a list of rows")
    if not isinstance(leads, (list, tuple)):
        raise ToolError("leads must be a list of rows")
    try:
        min_n = int(min_n)
    except (TypeError, ValueError):
        raise ToolError("min_n must be an integer")
    if min_n < 0:
        raise ToolError("min_n must be >= 0")
    if client_value is not None:
        client_value = _to_float(client_value, default=None)
        if client_value is None or client_value < 0:
            raise ToolError("client_value must be a non-negative number")
    bounds = _period_bounds(period)

    stats = {}

    def _slot(ch):
        s = stats.get(ch)
        if s is None:
            s = {"spend": 0.0, "leads": 0, "qualified": 0, "clients": 0, "has_cost": False}
            stats[ch] = s
        return s

    for row in costs:
        if not isinstance(row, dict):
            continue
        ch = _channel_key(_g(row, "channel"))
        if not ch:
            continue
        if bounds is not None and not _in_period(_date_part(_g(row, "date")), bounds):
            continue
        s = _slot(ch)
        s["spend"] += _row_amount(row)
        s["has_cost"] = True

    methods = {}
    unattributed = 0
    for row in leads:
        if not isinstance(row, dict):
            continue
        if bounds is not None and not _in_period(_date_part(_g(row, "created_at")), bounds):
            continue
        ch, method = attribute(row, mailed_codes)
        methods[method] = methods.get(method, 0) + 1
        if ch == "unattributed":
            unattributed += 1
            continue
        s = _slot(ch)
        s["leads"] += 1
        stage = _norm(_g(row, "stage")).lower() or "lead"
        if stage in ("qualified", "client"):
            s["qualified"] += 1
        if stage == "client":
            s["clients"] += 1

    out_channels = []
    for ch in sorted(stats):
        s = stats[ch]
        entry = {"channel": ch}
        entry.update(_metrics(s["spend"], s["leads"], s["qualified"], s["clients"], client_value))
        entry["note"] = _note(s, min_n)
        out_channels.append(entry)

    total = {"channel": "total"}
    total.update(_metrics(
        sum(s["spend"] for s in stats.values()),
        sum(s["leads"] for s in stats.values()),
        sum(s["qualified"] for s in stats.values()),
        sum(s["clients"] for s in stats.values()),
        client_value,
    ))
    total["note"] = _note({
        "spend": total["spend"], "leads": total["leads"],
        "has_cost": any(s["has_cost"] for s in stats.values()),
    }, min_n)

    return {
        "channels": out_channels,
        "unattributed": unattributed,
        "attribution_methods": {k: methods[k] for k in sorted(methods)},
        "total": total,
        "ranking": [c["channel"] for c in sorted(out_channels, key=_rank_key)],
    }


def breakeven(cost_per_piece, lead_to_client, client_value, target_roas=1.0):
    """Direct-mail break-even math: lead rate, pieces per lead, and max CPL."""
    cp, l2c, cv, roas = (_to_float(x, default=None) for x in
                         (cost_per_piece, lead_to_client, client_value, target_roas))
    for name, val in (("cost_per_piece", cp), ("lead_to_client", l2c),
                      ("client_value", cv), ("target_roas", roas)):
        if val is None or val <= 0:
            raise ToolError(f"{name} must be positive")
    lead_rate = cp * roas / (l2c * cv)
    return {
        "breakeven_lead_rate": round(lead_rate, 6),
        "pieces_per_lead_at_breakeven": round(1.0 / lead_rate, 4),
        "max_cpl": round(cv * l2c / roas, 2),
    }


def piece_cost(print_cost, postage, other=0.0, quantity=1):
    """Per-piece and total cost of one direct-mail drop."""
    p = _to_float(print_cost, default=None)
    q = _to_float(postage, default=None)
    o = _to_float(other, default=0.0)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ToolError("quantity must be an integer")
    if p is None or q is None or p < 0 or q < 0 or o < 0:
        raise ToolError("costs must be non-negative numbers")
    if quantity <= 0:
        raise ToolError("quantity must be positive")
    total = p + q + o
    return {"per_piece": round(total / quantity, 4), "total": round(total, 2)}


def selftest() -> int:
    n = 0

    def check(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    codes = frozenset({"DM42"})
    # attribute: code rule
    check(attribute({"tracking_code": "DM42"}, codes) == ("direct_mail", "code"), "mailed code")
    check(attribute({"tracking_code": "dm42"}, codes) == ("direct_mail", "code"), "mailed code case")
    check(attribute({"tracking_code": "NOPE"}, codes) == ("unattributed", "none"), "unknown code")
    # attribute: UTM
    check(attribute({"utm_source": "google", "utm_medium": "cpc"}) == ("google_ads", "utm"), "google paid")
    check(attribute({"utm_source": "chatgpt.com", "utm_medium": "paid"}) == ("chatgpt_ads", "utm"), "chatgpt paid")
    check(attribute({"utm_source": "perplexity", "utm_medium": "organic"}) == ("ai_organic", "utm"), "ai organic")
    check(attribute({"utm_source": "postcard"}) == ("direct_mail", "utm"), "mail utm")
    check(attribute({"utm_source": "instagram", "utm_medium": "paid"}) == ("meta_ads", "utm"), "meta paid")
    check(attribute({"utm_source": "newsletter"}) == ("unattributed", "none"), "unknown utm")
    # attribute: self reported
    check(attribute({"self_reported": "Got a postcard in the mail"}) == ("direct_mail", "self_reported"), "self mail")
    check(attribute({"self_reported": "searched on Google"}) == ("google_unknown", "self_reported"), "self google")
    check(attribute({"self_reported": "ChatGPT recommended you"}) == ("ai_unknown", "self_reported"), "self ai")
    check(attribute({"self_reported": "a friend referral"}) == ("referral", "self_reported"), "self referral")
    check(attribute({"self_reported": "email newsletter"}) == ("unattributed", "none"), "email not mail")
    # attribute: referrer + rule order + bad input
    check(attribute({"referrer": "https://www.google.com/search?q=x"}) == ("google_unknown", "referrer"), "ref google")
    check(attribute({"referrer": "chatgpt.com"}) == ("ai_organic", "referrer"), "ref chatgpt")
    check(attribute({"referrer": "https://blog.example.com/post"}) == ("referral", "referrer"), "ref blog")
    lead = {"utm_source": "google", "utm_medium": "cpc", "self_reported": "postcard"}
    check(attribute(lead) == ("google_ads", "utm"), "utm first")
    check(attribute(lead, rules=("self_reported", "utm")) == ("direct_mail", "self_reported"), "self first")
    check(attribute(None) == ("unattributed", "none"), "non-dict lead")

    # breakeven
    be = breakeven(1.0, 0.1, 100.0)
    check(abs(be["breakeven_lead_rate"] - 0.1) < 1e-9, "be lead rate")
    check(abs(be["pieces_per_lead_at_breakeven"] - 10.0) < 1e-6, "be pieces")
    check(abs(be["max_cpl"] - 10.0) < 1e-9, "be max cpl")
    be2 = breakeven(1.0, 0.1, 100.0, target_roas=2.0)
    check(abs(be2["breakeven_lead_rate"] - 0.2) < 1e-9, "be roas2")
    check(abs(be2["max_cpl"] - 5.0) < 1e-9, "be max cpl roas2")
    for bad in ((0, 0.1, 100), (1, 0, 100), (1, 0.1, 0)):
        try:
            breakeven(*bad)
            check(False, f"breakeven accepted {bad}")
        except ToolError:
            check(True, f"breakeven rejected {bad}")

    # piece_cost
    pc = piece_cost(100.0, 50.0, quantity=100)
    check(pc["total"] == 150.0, "piece total")
    check(abs(pc["per_piece"] - 1.5) < 1e-9, "piece per")
    check(piece_cost(1.0, 2.0)["per_piece"] == 3.0, "piece defaults")
    try:
        piece_cost(1.0, 1.0, quantity=0)
        check(False, "quantity zero accepted")
    except ToolError:
        check(True, "quantity zero rejected")

    # channel_report fixtures
    costs = [
        {"channel": "google_ads", "date": "2024-01-05", "amount": "100.00"},
        {"channel": "direct_mail", "date": "2024-01-10", "amount": 500},
        {"channel": "google_ads", "date": "2024-02-01", "amount": 50},
    ]
    leads = []
    for i in range(6):
        stage = "client" if i < 2 else ("qualified" if i == 2 else "lead")
        leads.append({"id": f"g{i}", "created_at": f"2024-01-0{i + 1}", "stage": stage,
                      "utm_source": "google", "utm_medium": "cpc"})
    leads += [
        {"id": "d1", "created_at": "2024-01-11", "stage": "qualified", "tracking_code": "DM42"},
        {"id": "d2", "created_at": "2024-01-12", "stage": "lead", "tracking_code": "DM42"},
        {"id": "m1", "created_at": "2024-01-13", "stage": "lead", "utm_source": "facebook", "utm_medium": "paid"},
        {"id": "u1", "created_at": "2024-01-14", "stage": "lead"},
        {"id": "x1", "created_at": "2024-03-01", "stage": "client", "utm_source": "google", "utm_medium": "cpc"},
    ]

    rep = channel_report(costs, leads, mailed_codes={"DM42"},
                         period=("2024-01-01", "2024-01-31"), client_value=1000.0, min_n=5)
    ch = {c["channel"]: c for c in rep["channels"]}
    check(set(ch) == {"google_ads", "direct_mail", "meta_ads"}, "channel set")
    check(rep["unattributed"] == 1, "unattributed")
    check(rep["attribution_methods"] == {"code": 2, "none": 1, "utm": 7}, "methods")
    g = ch["google_ads"]
    check(g["spend"] == 100.0, "g spend period")
    check(g["leads"] == 6 and g["qualified"] == 3 and g["clients"] == 2, "g counts")
    check(g["cpl"] == 16.67, "g cpl")
    check(g["cpql"] == 33.33, "g cpql")
    check(g["cac"] == 50.0, "g cac")
    check(g["lead_to_client"] == 0.3333, "g l2c")
    check(g["roas"] == 20.0, "g roas")
    check(g["note"] == "", "g note")
    d = ch["direct_mail"]
    check(d["spend"] == 500.0, "d spend")
    check(d["cpl"] == 250.0 and d["cac"] is None, "d metrics none")
    check(d["note"] == "too few to judge (n<5)", "d note")
    m = ch["meta_ads"]
    check(m["spend"] == 0.0 and m["cpl"] == 0.0, "m zero spend")
    check(m["note"] == "no cost data; too few to judge (n<5)", "m note")
    check(rep["ranking"] == ["google_ads", "meta_ads", "direct_mail"], "ranking")
    t = rep["total"]
    check(t["spend"] == 600.0 and t["leads"] == 9, "total basic")
    check(t["qualified"] == 4 and t["clients"] == 2, "total counts")
    check(t["cpl"] == 66.67 and t["cpql"] == 150.0, "total cpl cpql")
    check(t["cac"] == 300.0 and t["lead_to_client"] == 0.2222, "total cac l2c")
    check(t["roas"] == 3.3333, "total roas")

    rep2 = channel_report(costs, leads, mailed_codes={"DM42"}, client_value=None)
    ch2 = {c["channel"]: c for c in rep2["channels"]}
    check(ch2["google_ads"]["spend"] == 150.0, "no period spend")
    check(ch2["google_ads"]["leads"] == 7 and ch2["google_ads"]["clients"] == 3, "no period counts")
    check(ch2["google_ads"]["cac"] == 50.0 and ch2["google_ads"]["roas"] is None, "no client value roas")
    check(rep2["total"]["spend"] == 650.0 and rep2["total"]["leads"] == 10, "no period total")
    check(rep2["attribution_methods"]["utm"] == 8, "no period utm")

    empty = channel_report([], [])
    check(empty["channels"] == [] and empty["ranking"] == [], "empty lists")
    check(empty["unattributed"] == 0 and empty["attribution_methods"] == {}, "empty methods")
    check(empty["total"]["spend"] == 0.0 and empty["total"]["cpl"] is None, "empty total")

    weird = channel_report(
        [{"channel": "x", "date": "2024-01-01", "amount_micros": "2500000"},
         {"channel": "", "date": None, "amount": "n/a"},
         "not-a-dict"],
        [{"id": "1"}, {"id": "2", "stage": "client", "utm_source": "google", "utm_medium": "cpc"}, None],
    )
    w = {c["channel"]: c for c in weird["channels"]}
    check(w["x"]["spend"] == 2.5 and w["x"]["note"] == "spend without leads", "micros + spend without leads")
    check(w["google_ads"]["clients"] == 1 and w["google_ads"]["cac"] == 0.0, "malformed lead")
    check(weird["unattributed"] == 1 and weird["total"]["leads"] == 1, "malformed totals")

    for bad_period in (("2024-02-01", "2024-01-01"), ("bad", "2024-01-01"), ("2024-01-01",)):
        try:
            channel_report([], [], period=bad_period)
            check(False, f"period accepted {bad_period}")
        except ToolError:
            check(True, f"period rejected {bad_period}")

    return n


if __name__ == "__main__":
    import sys; n = selftest(); print(f"ALL PASS ({n} checks)", file=sys.stderr)