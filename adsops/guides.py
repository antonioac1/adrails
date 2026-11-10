"""Playbooks: the tested strategy behind AdRails, shipped inside the package so every owner's agent starts from what worked (2026-11-06).

A tool can audit a page; it cannot tell an agent WHICH pages to build first, what AI assistants actually cite, or the lead follow-up
cadence that books jobs. That judgement took weeks of running real businesses; these markdown files carry it, generic and public-safe,
and every step names the AdRails tool that does it. adsops_playbook lists them, returns one whole, or one section (to save context).
Files: adsops/playbooks/*.md (shipped as package data). Add a playbook = drop a .md there with a '# Title' first line.
"""
import os
import re

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks")
_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{0,40}$")


def available(base=DIR):
    out = []
    for f in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if f.endswith(".md"):
            text = open(os.path.join(base, f), encoding="utf-8").read()
            title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), f[:-3])
            out.append({"name": f[:-3], "title": title, "sections": sections(text), "lines": text.count("\n") + 1})
    return out


def sections(text):
    return [l[3:].strip() for l in text.splitlines() if l.startswith("## ")]


def read(name, section=None, base=DIR):
    """-> {"name","title","text"}; section matches a '## ' heading case-insensitively by substring."""
    from .errors import ToolError
    if not _NAME.match(str(name or "")):
        raise ToolError("playbook name must be one of: %s" % ", ".join(p["name"] for p in available(base)))
    path = os.path.join(base, name + ".md")
    if not os.path.isfile(path):
        raise ToolError("no playbook %r; available: %s" % (name, ", ".join(p["name"] for p in available(base))))
    text = open(path, encoding="utf-8").read()
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), name)
    if section:
        parts = re.split(r"(?m)^(?=## )", text)
        hit = [p for p in parts if p.startswith("## ") and str(section).lower() in p.splitlines()[0].lower()]
        if not hit:
            raise ToolError("no section matching %r; sections: %s" % (section, "; ".join(sections(text))))
        text = "\n".join(hit)
    return {"name": name, "title": title, "text": text}


def register(srv, ctx, cfg):
    @srv.tool("adsops_playbook", "The tested strategy behind AdRails, written for you (the agent) to execute: 'aeo' (getting the "
              "business recommended by ChatGPT, Perplexity, Gemini and Google AI answers), 'local-leads' (speed-to-lead, Local Services Ads, "
              "reviews, follow-up), 'google-business-profile' (to 100% and kept there), 'google-ads' (Search ads: tracking honesty, "
              "negatives, landing pages, caps, when to stop), 'chatgpt-ads' (ChatGPT Ads: drafts, creative, attribution, the guard) "
              "and 'seo-audit' (technical SEO site audit: run, read the table in order, Search Console, prove the fixes held). "
              "Each step names the AdRails tool that does it and a done-check. No name = list; section = one part only.",
              {"type": "object", "properties": {"name": {"type": "string"}, "section": {"type": "string"}},
               "required": [], "additionalProperties": False})
    def playbook(args):
        if not args.get("name"):
            return {"playbooks": available(),
                    "how": "call again with name (and optionally section) and follow the steps; tell the owner only the steps that need them"}
        return read(args["name"], args.get("section"))


def selftest():
    import tempfile
    n = 0
    td = tempfile.mkdtemp(prefix="adrails-pb-")
    open(os.path.join(td, "demo.md"), "w").write("# Demo playbook\nintro\n## First week\ndo a\n## Measure AI visibility\ndo b\n")
    a = available(td)
    assert a == [{"name": "demo", "title": "Demo playbook", "sections": ["First week", "Measure AI visibility"], "lines": 7}], a; n += 1
    r = read("demo", "measure", td)
    assert r["text"].startswith("## Measure AI visibility") and "do a" not in r["text"]; n += 1
    assert "intro" in read("demo", base=td)["text"]; n += 1
    for bad in ("../etc/passwd", "Demo", "", "x" * 60):
        try:
            read(bad, base=td)
            raise AssertionError("must refuse %r" % bad)
        except Exception as e:
            assert "playbook" in str(e); n += 1
    try:
        read("demo", "nope", td)
        raise AssertionError("unknown section must be refused")
    except Exception as e:
        assert "sections:" in str(e); n += 1
    shipped = {p["name"] for p in available()}
    assert {"aeo", "local-leads", "google-business-profile", "google-ads", "chatgpt-ads", "seo-audit"} <= shipped, "shipped playbooks missing: %s" % shipped; n += 1
    return n
