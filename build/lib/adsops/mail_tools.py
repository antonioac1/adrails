"""Direct-mail tools for AdRails: offline channels on the same ruler as paid ads (the maintainers glue, 2026-11-05).

Generalised from a live postcard program (BYO list, printer and CRM; local-first). Logic lives in adsops/mail/* (DeepSeek-built
from specs/ent/mail_*.md, Lane-A reviewed, independent black-box suites + tests/aggressive/). This file only wires it.

Safety:
  * Files are read from anywhere the server user can read, but WRITTEN only inside the mail workdir (cfg mail.workdir, default
    ~/.adsops/mail). Output names are sanitised; no path traversal.
  * Labels default to SAMPLE (watermarked). sample=false is a deliberate act and is recorded in the audit chain
    (mail_labels is in enterprise_tools.MUTATING).
  * Scan counting stores hashed visitor ids only, never raw IPs. Nothing here sends mail, buys postage or contacts anyone.
"""
import csv
import io
import json
import os
import re

from adsops.errors import ToolError
from adsops.mail import cac, codes, hygiene, labels, qr, response, scans

MAX_ROWS = 200_000


def _workdir(cfg):
    from adsops.enterprise_tools import state_path
    d = os.path.expanduser((cfg.get("mail") or {})["workdir"]) if (cfg.get("mail") or {}).get("workdir") else state_path(cfg, "_none", "mail")
    os.makedirs(d, exist_ok=True)
    return d


def _out(cfg, name, ext):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "out")).strip(".-")[:80] or "out"
    if not safe.endswith(ext):
        safe += ext
    return os.path.join(_workdir(cfg), safe)


def _read_csv(path):
    p = os.path.expanduser(str(path))
    if not os.path.isfile(p):
        raise ToolError(f"file not found: {path}")
    with open(p, newline="", encoding="utf-8-sig") as fh:
        rows = []
        for i, r in enumerate(csv.DictReader(fh)):
            if i >= MAX_ROWS:
                raise ToolError(f"more than {MAX_ROWS} rows; split the file")
            rows.append({(k or "").strip(): (v or "").strip() for k, v in r.items()})
    return rows


def _rows(args, key="records", path_key="csv_path"):
    if args.get(key):
        return list(args[key])
    if args.get(path_key):
        return _read_csv(args[path_key])
    raise ToolError(f"pass {key} or {path_key}")


def _write_csv(path, rows):
    cols = []
    for r in rows:
        for k in r:
            if not k.startswith("_") and k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    return path


def register(srv, ctx, cfg):
    def schema(props, req=()):
        return {"type": "object", "properties": props, "required": list(req), "additionalProperties": False}

    src = {"csv_path": {"type": "string", "description": "local CSV with name, address_line_1, address_line_2, city, state, zip[, country]"},
           "records": {"type": "array", "items": {"type": "object"}}}

    @srv.tool("mail_list_clean", "Clean a mailing list before you pay to print and post it: normalises addresses (USPS abbreviations, ZIP+4), "
              "drops foreign and incomplete addresses, institutional owners (banks, REITs, housing authorities; plain LLCs and trusts are kept), "
              "your own properties, anyone on your client / lead / do-not-mail lists, and duplicates. Labels are wrapped, never truncated. "
              "Writes the kept list as CSV in the mail workdir and returns counts by reason.",
              schema({**src, "suppress": {"type": "object", "description": "{clients: csv_path, leads: csv_path, do_not_mail: csv_path}"},
                      "own_addresses_csv": {"type": "string"}, "allow_foreign": {"type": "boolean"}, "out_name": {"type": "string"}}))
    def list_clean(args):
        recs = _rows(args)
        sup = {k: _read_csv(v) for k, v in (args.get("suppress") or {}).items()}
        own = [hygiene.normalize_address(r)["key"] for r in _read_csv(args["own_addresses_csv"])] if args.get("own_addresses_csv") else ()
        res = hygiene.clean(recs, suppress=sup or None, own_addresses=own, allow_foreign=bool(args.get("allow_foreign")))
        out = _write_csv(_out(cfg, args.get("out_name") or "clean-list", ".csv"), res["kept"])
        return {"counts": res["counts"], "dropped_sample": res["dropped"][:25], "suppression": hygiene.suppression_stats(sup),
                "kept_csv": out}

    @srv.tool("mail_codes_assign", "Give every mail piece a unique tracking code (no 0/1/I/L/O/U, with a check character that catches every "
              "single typo and swapped pair when a caller reads it back) and a tracking URL with UTM tags. Writes a CSV ready for labels "
              "or a print vendor.",
              schema({**src, "base_url": {"type": "string"}, "campaign": {"type": "string"}, "existing_codes_csv": {"type": "string"},
                      "param": {"type": "string"}, "out_name": {"type": "string"}}, ("base_url",)))
    def codes_assign(args):
        recs = _rows(args)
        existing = {r.get("tracking_code") for r in _read_csv(args["existing_codes_csv"])} if args.get("existing_codes_csv") else set()
        utm = {"utm_source": "direct_mail", "utm_medium": "mail"}
        pieces = codes.assign(recs, args["base_url"], existing=existing, utm=utm, campaign=args.get("campaign"))
        out = _write_csv(_out(cfg, args.get("out_name") or "pieces", ".csv"), pieces)
        return {"pieces": len(pieces), "pieces_csv": out, "example": {k: pieces[0].get(k) for k in ("tracking_code", "tracking_url")} if pieces else None}

    @srv.tool("mail_labels", "Print-exact label sheets (Avery 5160/8160, 5163, 5164, 5167) as HTML that prints to PDF from any browser, with an "
              "optional QR per label, plus a vendor CSV. SAMPLE watermark is ON by default; pass sample=false only for the real print run "
              "(that call is recorded in the audit chain).",
              schema({"csv_path": {"type": "string"}, "template": {"type": "string"}, "sample": {"type": "boolean"},
                      "qr": {"type": "boolean"}, "out_name": {"type": "string"}}, ("csv_path",)))
    def labels_tool(args):
        pieces = _read_csv(args["csv_path"])
        for p in pieces:
            p["_label"] = hygiene.label_lines(p)
        tpl = args.get("template", "avery_5160")
        if tpl not in labels.TEMPLATES:
            raise ToolError("template must be one of " + ", ".join(sorted(labels.TEMPLATES)))
        qr_fn = (lambda url: qr.to_svg(qr.encode(url, ecc="M"), scale=2, border=2)) if args.get("qr") else None
        sample = args.get("sample", True)
        name = args.get("out_name") or ("labels-sample" if sample else "labels-PRINT")
        h = labels.sheet_html(pieces, template=tpl, sample=sample, qr_svg=qr_fn)
        hp = _out(cfg, name, ".html")
        open(hp, "w", encoding="utf-8").write(h)
        cp = _out(cfg, name + "-vendor", ".csv")
        open(cp, "w", encoding="utf-8").write(labels.vendor_csv(pieces))
        return {"html": hp, "vendor_csv": cp, "sample": sample, **labels.counts(pieces, tpl)}

    @srv.tool("mail_qr", "Make a QR code for any URL (pure-Python encoder, verified against an independent decoder). Returns SVG text and "
              "writes a PNG in the mail workdir.",
              schema({"url": {"type": "string"}, "ecc": {"type": "string", "description": "L|M|Q|H, default M"},
                      "out_name": {"type": "string"}}, ("url",)))
    def qr_tool(args):
        q = qr.encode(args["url"], ecc=args.get("ecc", "M"))
        p = _out(cfg, args.get("out_name") or "qr", ".png")
        open(p, "wb").write(qr.to_png(q, scale=8))
        return {"version": q.version, "ecc": q.ecc, "mask": q.mask, "svg": qr.to_svg(q), "png": p}

    @srv.tool("mail_scans", "Count real scans of your mail tracking codes from your own web-server access logs: unique visitors per code, "
              "first-scan delay, and scan rate by batch/segment. Drops bots and link previewers, your own IPs, unknown codes, and any hit "
              "before that piece was mailed (your own testing). Stores hashed visitor ids only.",
              schema({"log_paths": {"type": "array", "items": {"type": "string"}}, "pieces_csv": {"type": "string"},
                      "format": {"type": "string"}, "param": {"type": "string"}, "own_ips": {"type": "array", "items": {"type": "string"}},
                      "own_cidrs": {"type": "array", "items": {"type": "string"}}}, ("log_paths", "pieces_csv")))
    def scans_tool(args):
        pieces = _read_csv(args["pieces_csv"])
        mailed = {p["tracking_code"]: p.get("mailed_at") or "1970-01-01T00:00:00+00:00" for p in pieces if p.get("tracking_code")}
        lines = []
        for lp in args["log_paths"][:50]:
            p = os.path.expanduser(lp)
            if not os.path.isfile(p):
                raise ToolError(f"log not found: {lp}")
            opener = __import__("gzip").open if p.endswith(".gz") else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
                lines.extend(fh)
        salt = (cfg.get("mail") or {}).get("visitor_salt", "")
        r = scans.count(lines, mailed, fmt=args.get("format", "combined"), param=args.get("param", "c"),
                        own_ips=tuple(args.get("own_ips") or ()), own_cidrs=tuple(args.get("own_cidrs") or ()),
                        normalizer=codes.normalize, salt=salt)
        state_p = _out(cfg, "scans-state", ".json")
        prev = json.load(open(state_p)) if os.path.isfile(state_p) else None
        merged = scans.merge(prev, r) if prev else r
        json.dump(merged, open(state_p, "w"))
        return {"summary": scans.summarize(merged, pieces), "dropped": r["dropped"], "codes_scanned": len(merged["scans"]),
                "state": state_p}

    @srv.tool("mail_response", "Honest direct-mail response rates by any column (batch, segment, variant...): scans, leads, clients, with 95% "
              "intervals and a verdict that says 'too early' or 'no signal yet: could still be up to X%' instead of a misleading zero.",
              schema({"pieces_csv": {"type": "string"}, "outcomes": {"type": "object", "description": "{code: {scanned, lead, client, lead_at}}"},
                      "leads_csv": {"type": "string", "description": "alternative: CSV with tracking_code, stage, created_at"},
                      "by": {"type": "string"}, "min_days": {"type": "integer"}}, ("pieces_csv",)))
    def response_tool(args):
        pieces = _read_csv(args["pieces_csv"])
        out = dict(args.get("outcomes") or {})
        if args.get("leads_csv"):
            for l in _read_csv(args["leads_csv"]):
                c = codes.normalize(l.get("tracking_code") or "")
                if c:
                    o = out.setdefault(c, {})
                    o["lead"] = True
                    o["client"] = o.get("client") or l.get("stage") == "client"
                    o["lead_at"] = l.get("created_at")
        import datetime as dt
        today = dt.date.fromtimestamp(ctx.now()).isoformat() if ctx else None
        return {"rows": response.rate_table(pieces, out, by=args.get("by", "batch"), min_days=args.get("min_days", 42), today=today),
                "decay": response.decay(out, pieces)}

    @srv.tool("mail_holdout_plan", "How many pieces (and how big a no-mail holdout) you need to prove direct mail works: sample size for a "
              "base response rate and the lift you want to detect, at 95% confidence and 80% power.",
              schema({"base_rate": {"type": "number"}, "lift": {"type": "number"}, "ratio": {"type": "number"}}, ("base_rate", "lift")))
    def holdout(args):
        return response.holdout_size(args["base_rate"], args["lift"], ratio=args.get("ratio", 1.0))

    @srv.tool("mail_compare", "Is variant A really better than B? Two-proportion test with a difference interval (e.g. two postcard designs, or "
              "mailed vs holdout).", schema({"k1": {"type": "integer"}, "n1": {"type": "integer"}, "k2": {"type": "integer"},
                                              "n2": {"type": "integer"}}, ("k1", "n1", "k2", "n2")))
    def compare(args):
        return response.compare(args["k1"], args["n1"], args["k2"], args["n2"])

    @srv.tool("channel_cac", "Cost per lead and per client across every channel on ONE ruler: Google Ads, ChatGPT Ads, direct mail, lead vendors, "
              "referrals. Attribution order: tracking code, then UTM, then what the lead said, then referrer. Optionally pulls live Google "
              "Ads spend for a campaign. Small numbers are labelled 'too few to judge'.",
              schema({"costs_csv": {"type": "string", "description": "channel,date,amount"}, "leads_csv": {"type": "string"},
                      "pieces_csv": {"type": "string"}, "client_value": {"type": "number"},
                      "google_campaign": {"type": "string"}, "since": {"type": "string"}, "until": {"type": "string"}},
                     ("leads_csv",)))
    def channel_cac(args):
        costs = [dict(r, amount=float(r.get("amount") or 0)) for r in _read_csv(args["costs_csv"])] if args.get("costs_csv") else []
        leads = _read_csv(args["leads_csv"])
        mailed = {p.get("tracking_code") for p in _read_csv(args["pieces_csv"])} if args.get("pieces_csv") else set()
        period = (args["since"], args["until"]) if args.get("since") and args.get("until") else None
        if args.get("google_campaign"):
            if not period:
                raise ToolError("google_campaign needs since and until (YYYY-MM-DD)")
            _, _, _, eff, _, client = ctx.open(args["google_campaign"])
            rows = client.search("SELECT metrics.cost_micros FROM campaign WHERE campaign.id = %d AND segments.date BETWEEN '%s' AND '%s'"
                                 % (int(eff["campaign_id"]), period[0], period[1]))
            spend = sum(float((r.get("metrics") or {}).get("costMicros") or 0) for r in rows) / 1e6
            costs = [c for c in costs if c.get("channel") != "google_ads"] + [{"channel": "google_ads", "date": period[0], "amount": round(spend, 2)}]
        return cac.channel_report(costs, leads, mailed_codes=frozenset(c for c in mailed if c), period=period,
                                  client_value=args.get("client_value"))

    @srv.tool("mail_breakeven", "Unit economics of a mail piece: cost per piece (print + postage + other) and the response rate you need to break "
              "even for a given lead-to-client rate and client value.",
              schema({"print_cost": {"type": "number"}, "postage": {"type": "number"}, "other": {"type": "number"},
                      "lead_to_client": {"type": "number"}, "client_value": {"type": "number"}, "quantity": {"type": "integer"}},
                     ("print_cost", "postage", "lead_to_client", "client_value")))
    def breakeven(args):
        pc = cac.piece_cost(args["print_cost"], args["postage"], args.get("other", 0.0), args.get("quantity", 1))
        return {"cost": pc, "breakeven": cac.breakeven(pc["per_piece"], args["lead_to_client"], args["client_value"])}


def selftest():
    import tempfile
    from adsops import protocol
    n = 0
    tmp = tempfile.mkdtemp()
    cfg = {"mail": {"workdir": os.path.join(tmp, "mail")}}
    srv = protocol.Server("t", "0")

    class Ctx:
        now = staticmethod(lambda: 1793880000.0)

    register(srv, Ctx(), cfg)

    def call(name, a, err=False):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": a}})["result"]
        assert r["isError"] == err, (name, r["content"][0]["text"][:400])
        return r.get("structuredContent")

    lp = os.path.join(tmp, "list.csv")
    with open(lp, "w") as fh:
        fh.write("name,address_line_1,address_line_2,city,state,zip,country\n"
                 "Pat Owner,123 Main Street,APT 4B,Tampa,FL,33602-1234,\n"
                 "Pat Owner,123 MAIN ST.,apt 4b,Tampa,FL,33602,\n"
                 "TRICON SFR LLC,9 Oak Ave,,Tampa,FL,33603,\n"
                 "Lee Abroad,1 King St,,Toronto,ON,M5V 2T6,Canada\n"
                 "Sam Landlord LLC,77 Elm Rd,,Lutz,FL,33549,\n")
    r = call("mail_list_clean", {"csv_path": lp, "out_name": "../../etc/evil"})
    assert r["counts"]["kept"] == 2 and r["kept_csv"].startswith(cfg["mail"]["workdir"]); n += 1          # traversal neutralised
    r = call("mail_codes_assign", {"csv_path": r["kept_csv"], "base_url": "https://example.com/offer/", "campaign": "dec"})
    assert r["pieces"] == 2 and codes.is_valid(r["example"]["tracking_code"]) and "utm_campaign=dec" in r["example"]["tracking_url"]; n += 1
    pieces_csv = r["pieces_csv"]
    l = call("mail_labels", {"csv_path": pieces_csv, "qr": True})
    h = open(l["html"]).read()
    assert l["sample"] is True and "SAMPLE" in h and "<svg" in h and "33602-1234" in h; n += 1
    q = call("mail_qr", {"url": "https://example.com/offer/?c=ABC2345"})
    assert open(q["png"], "rb").read(8) == b"\x89PNG\r\n\x1a\n" and q["svg"].startswith("<svg"); n += 1
    code = [row for row in csv.DictReader(open(pieces_csv))][0]["tracking_code"]
    log = os.path.join(tmp, "access.log")
    open(log, "w").write('8.8.8.8 - - [05/Nov/2026:10:00:00 -0500] "GET /offer/?c=%s HTTP/1.1" 200 1 "-" "Mozilla/5.0 (iPhone)"\n'
                         '8.8.4.4 - - [05/Nov/2026:10:00:00 -0500] "GET /offer/?c=%s HTTP/1.1" 200 1 "-" "Slackbot 1.0"\n' % (code, code))
    s = call("mail_scans", {"log_paths": [log], "pieces_csv": pieces_csv})
    assert s["codes_scanned"] == 1 and "8.8.8.8" not in json.dumps(s); n += 1
    s2 = call("mail_scans", {"log_paths": [log], "pieces_csv": pieces_csv})
    assert s2["codes_scanned"] == 1; n += 1                                                               # idempotent re-run
    rr = call("mail_response", {"pieces_csv": pieces_csv, "outcomes": {code: {"scanned": True}}})
    assert rr["rows"] and "verdict" in rr["rows"][0]; n += 1
    assert call("mail_holdout_plan", {"base_rate": 0.01, "lift": 0.5})["treatment"] > 1000; n += 1
    assert call("mail_compare", {"k1": 30, "n1": 1000, "k2": 15, "n2": 1000})["significant"]; n += 1
    leads = os.path.join(tmp, "leads.csv")
    open(leads, "w").write("id,created_at,stage,tracking_code,utm_source,utm_medium,self_reported\n"
                           "1,2026-11-05,lead,%s,,,\n2,2026-11-05,client,,google,cpc,\n3,2026-11-05,lead,,,,\n" % code)
    costs = os.path.join(tmp, "costs.csv")
    open(costs, "w").write("channel,date,amount\ngoogle_ads,2026-11-01,700\ndirect_mail,2026-11-02,395\n")
    c = call("channel_cac", {"costs_csv": costs, "leads_csv": leads, "pieces_csv": pieces_csv, "client_value": 3000})
    ch = {x["channel"]: x for x in c["channels"]}
    assert ch["direct_mail"]["leads"] == 1 and ch["google_ads"]["clients"] == 1 and c["unattributed"] == 1; n += 1
    b = call("mail_breakeven", {"print_cost": 0.096, "postage": 0.65, "other": 0.04, "lead_to_client": 0.25, "client_value": 3000})
    assert abs(b["cost"]["per_piece"] - 0.786) < 1e-6; n += 1
    call("mail_labels", {"csv_path": pieces_csv, "template": "nope"}, err=True); n += 1
    call("mail_list_clean", {}, err=True); n += 1
    return n
