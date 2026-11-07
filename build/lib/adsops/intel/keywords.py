"""Seed a tenant keyword set: the live ad keywords plus town-name variants.

    python3 -m adsops.intel.keywords seed --config CFG --structure FILE.json [--structure ...] \
        --town Tampa --town "Wesley Chapel" ... [--write]

``--structure`` reads any JSON holding ad groups with ``keywords: [[text, match_type], ...]`` (Acme's
``split-2026-10-26/structure.json``) or a ``keywords`` list of strings / {"text": ...} objects
(desired_state files). Without ``--write`` it prints the list; with it, it replaces ``keywords`` in the config.
"""
import argparse
import json
import re
import sys

PATTERNS = ("property management {town}", "{town} property management",
            "property manager {town}", "rental management {town}",
            # second tier: how owners phrase the same search in our own search-terms reports
            "property management companies {town}", "property management {town} fl",
            "rental property management {town}")


def norm(text):
    t = re.sub(r"[^\w\s']", " ", str(text).lower()).replace("'", "")
    return re.sub(r"\s+", " ", t).strip()


def _walk(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "keywords" and isinstance(v, list):
                for item in v:
                    if isinstance(item, str):
                        out.append(item)
                    elif isinstance(item, list) and item and isinstance(item[0], str):
                        out.append(item[0])
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        out.append(item["text"])
            elif not k.startswith("negative"):
                _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def from_structure(obj):
    out = []
    _walk(obj, out)
    return out


def town_variants(towns, patterns=PATTERNS):
    return [p.format(town=norm(t)) for t in towns for p in patterns]


def seed(structures, towns, extra=()):
    seen, out = set(), []
    for kw in [*[k for s in structures for k in from_structure(s)], *town_variants(towns), *extra]:
        k = norm(kw)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adsops.intel.keywords")
    ap.add_argument("cmd", nargs="?", choices=["seed"])
    ap.add_argument("--config")
    ap.add_argument("--structure", action="append", default=[])
    ap.add_argument("--town", action="append", default=[])
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        print(f"intel.keywords: {selftest()} checks")
        print("ALL PASS")
        return 0
    if a.cmd != "seed":
        ap.error("command required: seed")
    structs = []
    for p in a.structure:
        with open(p, encoding="utf-8") as fh:
            structs.append(json.load(fh))
    kws = seed(structs, a.town)
    if a.write:
        if not a.config:
            ap.error("--write needs --config")
        with open(a.config, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["keywords"] = kws
        with open(a.config, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
        print(f"wrote {len(kws)} keywords to {a.config}")
    else:
        print("\n".join(kws))
        print(f"# {len(kws)} keywords", file=sys.stderr)
    return 0


def selftest():
    n = 0
    s = {"ad_groups": [{"keywords": [["Property Management Tampa", "EXACT"], ["flat fee", "PHRASE"]]}],
         "negatives_add": [{"text": "jobs"}], "negative_keywords": ["rentals"]}
    assert from_structure(s) == ["Property Management Tampa", "flat fee"]; n += 1
    assert norm("Land O' Lakes") == "land o lakes" and norm("  A,  b ") == "a b"; n += 1
    v = town_variants(["Land O' Lakes"])
    assert v[0] == "property management land o lakes" and len(v) == len(PATTERNS); n += 1
    out = seed([s], ["Tampa"])
    assert out.count("property management tampa") == 1 and "jobs" not in out and "rentals" not in out; n += 1
    assert len(out) == 2 + len(PATTERNS) - 1; n += 1
    assert seed([{"keywords": [{"text": "x y"}, "z"]}], []) == ["x y", "z"]; n += 1
    return n


if __name__ == "__main__":
    sys.exit(main())
