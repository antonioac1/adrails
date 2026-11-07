"""OpenAI Ads asset + measurement helpers: image upload, brand logo, pixel + lead_created event, test event.

None of these can spend money. They still write to the ad account (except the test event, which is
always validate_only), so commit=true needs account mode=live and commit_policy.allow_commit.
A brand update can pause ALL delivery until OpenAI re-reviews the account, so it also needs
acknowledge_delivery_pause=true.
"""
import os
import stat

from . import oai_policy, urls
from .errors import RailViolation, ToolError
from .platforms import openai_ads

MAX_IMAGE_BYTES = 10_000_000
MIN_PX = {"ad_image": 640, "account_favicon": 256}   # docs say 256 on one page, 128 on another: use 256
CTYPE = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


def _schema(props, required):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _write_allowed(ctx, account):
    if account.get("mode") != "live":
        raise RailViolation(f"account {account['key']!r} is mode={account.get('mode')}; commit needs mode=live")
    if not ctx.cfg.get("commit_policy", {}).get("allow_commit", False):
        raise RailViolation("commit disabled by commit_policy.allow_commit")


def _account_domains(cfg, account):
    doms = set()
    for camp in account.get("campaigns", []):
        try:
            doms.update(oai_policy.load(camp["policy_file"])["allowed_domains"])
        except ToolError:
            continue
    if not doms:
        raise RailViolation("no readable policy file for this account names allowed_domains")
    return sorted(doms)


def read_image(path, purpose):
    p = os.path.realpath(os.path.expanduser(path))
    try:
        st = os.stat(p)
    except OSError:
        raise ToolError(f"image not found: {p}") from None
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_IMAGE_BYTES or st.st_size < 100:
        raise ToolError(f"{p} is not a regular image file under {MAX_IMAGE_BYTES} bytes")
    with open(p, "rb") as fh:
        content = fh.read()
    info = openai_ads.image_info(content)
    if not info:
        raise ToolError(f"{p} is not a PNG, JPEG or WebP image")
    fmt, w, h = info
    need = MIN_PX[purpose]
    if w < need or h < need:
        raise ToolError(f"{p} is {w}x{h}; {purpose} needs at least {need}x{need}")
    if purpose == "ad_image" and w != h:
        note = "not square: OpenAI may crop it (pass image_crop) or reject it"
    else:
        note = None
    return p, content, fmt, w, h, note


def register(srv, ctx, cfg, oai):
    @srv.tool("adsops_openai_upload_image", "OpenAI Ads: check and upload a local PNG/JPEG/WebP (ad_image >= 640x640, "
              "account_favicon >= 256x256) and return its file_id. Checks only unless commit=true.",
              _schema({"account": {"type": "string"}, "path": {"type": "string"},
                       "purpose": {"type": "string", "enum": ["ad_image", "account_favicon"]},
                       "commit": {"type": "boolean"}}, ["account", "path", "purpose"]))
    def upload(args):
        a = oai.account(args["account"])
        p, content, fmt, w, h, note = read_image(args["path"], args["purpose"])
        out = {"account": a["key"], "path": p, "format": fmt, "width": w, "height": h, "bytes": len(content),
               "note": note, "committed": False}
        if not args.get("commit"):
            return out
        _write_allowed(ctx, a)
        r = oai.client(a, "write").upload_image(os.path.basename(p), content, CTYPE[fmt],
                                                "account_favicon" if args["purpose"] == "account_favicon" else None)
        ctx.audit({"tool": "adsops_openai_upload_image", "account": a["key"], "path": p, "file_id": r.get("file_id")})
        return {**out, "committed": True, "file_id": r.get("file_id")}

    @srv.tool("adsops_openai_brand_update", "OpenAI Ads: set the advertiser icon (favicon_file_id), website url and/or "
              "brand_name. WARNING: brand changes can pause ALL delivery until OpenAI re-reviews the account. "
              "Preview unless commit=true and acknowledge_delivery_pause=true.",
              _schema({"account": {"type": "string"}, "favicon_file_id": {"type": "string"}, "url": {"type": "string"},
                       "brand_name": {"type": "string"}, "commit": {"type": "boolean"},
                       "acknowledge_delivery_pause": {"type": "boolean"}}, ["account"]))
    def brand(args):
        a = oai.account(args["account"])
        body = {}
        if args.get("favicon_file_id"):
            body["favicon_file_id"] = openai_ads._seg(args["favicon_file_id"], "file id")
        if args.get("url"):
            body["url"] = urls.check_url(args["url"], _account_domains(cfg, a))
        if args.get("brand_name"):
            bn = args["brand_name"].strip()
            if not 1 <= len(bn) <= 100:
                raise ToolError("brand_name must be 1..100 characters")
            body["brand_name"] = bn
        if not body:
            raise ToolError("nothing to change: give favicon_file_id, url or brand_name")
        out = {"account": a["key"], "body": body, "committed": False,
               "warning": "a brand update can pause delivery for the whole account until review passes"}
        if not args.get("commit"):
            return out
        if args.get("acknowledge_delivery_pause") is not True:
            raise RailViolation("brand update refused: pass acknowledge_delivery_pause=true (delivery may pause)")
        _write_allowed(ctx, a)
        r = oai.client(a, "write").update_brand(body)
        ctx.audit({"tool": "adsops_openai_brand_update", "account": a["key"], "fields": sorted(body)})
        return {**out, "committed": True, "status": r.get("status"), "review": r.get("review")}

    @srv.tool("adsops_openai_conversions_setup", "OpenAI Ads: make sure a web pixel and a lead_created conversion "
              "event setting exist (reuses ones with the same names). Optionally creates a Conversions API key and "
              "writes it to the config's capi key file (chmod 600, never returned). Preview unless commit=true.",
              _schema({"account": {"type": "string"}, "pixel_name": {"type": "string"},
                       "event_name": {"type": "string"}, "create_capi_key": {"type": "boolean"},
                       "commit": {"type": "boolean"}}, ["account"]))
    def conversions(args):
        a = oai.account(args["account"])
        pname = (args.get("pixel_name") or "AdRails website pixel").strip()
        ename = (args.get("event_name") or "Lead created").strip()
        if not 3 <= len(pname) <= 1000 or not 3 <= len(ename) <= 1000:
            raise ToolError("pixel_name and event_name must be 3..1000 characters")
        c = oai.client(a)
        pix = next((p for p in c.pixels() if p.get("name") == pname), None)
        evs = c.event_settings()
        ev = next((e for e in evs if pix and e.get("event_type") == "lead_created" and not e.get("archived")
                   and pix.get("id") in (e.get("source_ids") or [])), None)
        todo = ([] if pix else [f"create pixel {pname!r}"]) + ([] if ev else [f"create lead_created setting {ename!r}"])
        keyfile = os.path.expanduser(a.get("capi_key_file") or "")
        if args.get("create_capi_key"):
            if not a.get("capi_key_file"):
                raise ToolError("create_capi_key needs capi_key_file in the account config")
            if os.path.exists(keyfile):
                raise RailViolation(f"{keyfile} already exists; refusing to overwrite a Conversions API key")
            todo.append(f"create Conversions API key -> {keyfile}")
        out = {"account": a["key"], "pixel": pix, "event_setting": ev, "todo": todo, "committed": False}
        if not args.get("commit") or not todo:
            return out
        _write_allowed(ctx, a)
        c = oai.client(a, "write")
        if not pix:
            pix = c.create_pixel(pname)
            if not pix.get("id"):
                raise ToolError("pixel creation returned no id")
        if not ev:
            ev = c.create_event_setting(ename, "lead_created", pix["id"])
        if args.get("create_capi_key"):
            secret = (c.create_capi_key("AdRails " + a["key"]).get("api_key") or "").strip()
            if len(secret) < 16 or any(ch.isspace() for ch in secret):
                raise ToolError("Conversions API key creation returned no usable key; nothing was written")
            tmp = keyfile + ".tmp-adrails"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(secret + "\n")
                os.link(tmp, keyfile)          # fails if keyfile appeared meanwhile: never overwrite
            finally:
                os.unlink(tmp)
            out["capi_key_file"] = keyfile
        ctx.audit({"tool": "adsops_openai_conversions_setup", "account": a["key"], "pixel_id": pix.get("pixel_id"),
                   "event_setting_id": ev.get("id"), "capi_key_created": bool(args.get("create_capi_key"))})
        return {**out, "committed": True, "pixel": {k: pix.get(k) for k in ("id", "name", "pixel_id")},
                "event_setting": {k: ev.get(k) for k in ("id", "name", "event_type")},
                "next": "add the Pixel to the owner form's thank-you step: oaiq('measure', 'lead_created', "
                        "{type: 'customer_action'}, {event_id: <lead id>})"}

    @srv.tool("adsops_openai_test_event", "OpenAI Ads: send ONE lead_created test event to the Conversions API with "
              "validate_only=true (never saved, never counted). Needs the account's Conversions API key.",
              _schema({"account": {"type": "string"}, "pixel_id": {"type": "string"}, "source_url": {"type": "string"}},
                      ["account", "pixel_id", "source_url"]))
    def test_event(args):
        a = oai.account(args["account"])
        if a.get("mode", "disabled") == "disabled":
            raise RailViolation(f"account {a['key']!r} is disabled")
        src = urls.check_url(args["source_url"], _account_domains(cfg, a))
        if not a.get("capi_key_file"):
            raise ToolError("no capi_key_file in the account config (run adsops_openai_conversions_setup "
                            "create_capi_key=true, or store the key there yourself)")
        key = openai_ads.key_from_file(a["capi_key_file"])()
        event = {"id": f"adrails-test-{int(ctx.now())}", "type": "lead_created", "timestamp_ms": int(ctx.now() * 1000),
                 "action_source": "web", "source_url": src, "data": {"type": "customer_action"}}
        r = openai_ads.capi_validate_event(key, args["pixel_id"], event, http=oai.capi_http)
        ctx.audit({"tool": "adsops_openai_test_event", "account": a["key"], "validate_only": True})
        return {"account": a["key"], "validate_only": True, "event": event, "response": r}
