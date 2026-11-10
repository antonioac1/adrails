"""Site profile and approved-facts loading for the AdRails guarded content pipeline (adsops/content, 0.12).

Two small files decide what the pipeline is allowed to say about a site:

  * the site PROFILE (JSON) - the origin, the author, the rulings (banned phrases, excluded areas, competitors, own
    brand), the allowed source domains, word/duplication thresholds, the writer/reviewer backends and the hard caps.
    `load_profile` validates it, fills every optional key with its documented default and resolves the two file paths
    relative to the profile file itself, so a profile behaves the same whatever the current directory is;
  * the FACTS file (JSON) - the *only* statements that may be asserted without a cited source, each one approved by a
    named human on a date. `load_facts` refuses anything that is not a finished approval: a proposal, a missing
    approver, an empty approval or an oversized fact. `propose` builds a proposal from fetched page text and never
    approves it; `write_proposal` writes that proposal once, refusing to overwrite a file a human may already have
    reviewed.

Everything read here (profile files, facts files, page text) is UNTRUSTED data: nothing is eval'd or exec'd, no regex
is ever built from it, every string and list is capped before use, and every error message is scrubbed. Besides the one
documented write in `write_proposal` (and the temp files the selftest uses), this module touches no network, no
subprocess and no clock.
"""
import hashlib
import json
import os
import re
import tempfile
import urllib.parse
from datetime import date

from ..errors import ToolError, scrub
from . import numbers

MAX_PROFILE_BYTES = 256 * 1024           # a site profile may not exceed 256 KB
MAX_FACTS_BYTES = 1024 * 1024            # an approved facts file may not exceed 1 MB
MAX_PROPOSAL_BYTES = 1024 * 1024         # a proposal may not exceed 1 MB
MAX_PATH = 4096                          # cap for any path we accept
MAX_ITEM_CHARS = 200                     # cap for one list item (rulings, domains, author fields)
MAX_LIST_ITEMS = 200                     # cap for one list of strings in the profile
MAX_FACTS = 500                          # an approval may list at most 500 facts
MAX_FACT_CHARS = 1000                    # one fact text may not exceed 1000 chars
MAX_PROPOSED = 500                       # hard ceiling for propose(max_facts=...)
MAX_PAGES = 500                          # pages considered by propose
MAX_PAGE_TEXT = 200_000                  # chars of page text considered per page by propose
MIN_FACT_SENTENCE = 20                   # shortest sentence propose will turn into a fact
MAX_FACT_SENTENCE = 300                  # longest sentence propose will turn into a fact
MAX_NOTE = 400                           # cap for a message we quote back

_SITE_SCHEMES = ("https", "http")
_ISO_DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_CLAIM_RE = re.compile(r"\d|\$|%")                                    # a number/money/percent claim
_FIRST_PERSON_RE = re.compile(r"(?<![a-z])(?:we|our|us)(?![a-z])", re.I)   # a first-person business claim

_BAD_PATH_CHARS = "\x00\r\n"

_NOTE = ("Review every line. Delete anything untrue or that you do not want stated. Then set approved_by and "
         "approved_at and remove \"proposed\".")

_NO_FACTS_MSG = ('no approved facts file at "{path}": the pipeline refuses to draft. Run seo_content_facts to propose '
                 'one, then a human reviews it and sets approved_by.')


# ── small validation helpers ────────────────────────────────────────────────────────────────────────────────
def _fail(msg):
    """Raise ToolError with a scrubbed, capped, plain-English message. Never returns."""
    raise ToolError(scrub(msg, 400))


def _read_text(path, max_bytes, what, missing_msg):
    """Read a UTF-8 text file, refusing a missing path, a directory, an oversize file or non-UTF-8 bytes."""
    if not isinstance(path, str) or not path.strip():
        _fail("no {} file path was given.".format(what))
    target = path.strip()
    if len(target) > MAX_PATH:
        _fail("the {} file path is too long.".format(what))
    if any(ch in target for ch in _BAD_PATH_CHARS):
        _fail("the {} file path is not usable.".format(what))
    try:
        with open(target, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except FileNotFoundError:
        _fail(missing_msg)
    except IsADirectoryError:
        _fail('the {} path "{}" is a directory, not a file.'.format(what, scrub(target, MAX_ITEM_CHARS)))
    except OSError as exc:
        _fail('could not read the {} file "{}": {}'.format(what, scrub(target, MAX_ITEM_CHARS), scrub(exc, 120)))
    if len(raw) > max_bytes:
        _fail("the {} file is larger than {} KB; keep it small.".format(what, max_bytes // 1024))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("the {} file is not UTF-8 text.".format(what))


def _parse_object(text, what):
    """Parse JSON and require one object; anything else is refused with a plain message."""
    try:
        data = json.loads(text)
    except Exception:
        _fail("the {} file is not valid JSON.".format(what))
    if not isinstance(data, dict):
        _fail("the {} file must contain one JSON object.".format(what))
    return data


def _num_int(value, label, low, high):
    """Require a whole number (bools refused) inside [low, high]."""
    if isinstance(value, bool) or not isinstance(value, int):
        _fail('"{}" must be a whole number between {} and {}.'.format(label, low, high))
    if value < low or value > high:
        _fail('"{}" must be a whole number between {} and {} (got {}).'.format(label, low, high, value))
    return value


def _num_float(value, label, low, high):
    """Require a finite number inside [low, high]; ints are accepted and kept as floats."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail('"{}" must be a number between {} and {}.'.format(label, low, high))
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < low or number > high:
        _fail('"{}" must be a number between {} and {} (got {}).'.format(label, low, high, value))
    return number


def _str_list(value, label, max_items=MAX_LIST_ITEMS, max_chars=MAX_ITEM_CHARS):
    """Require a list of strings, capped in length and item size; returns a new list."""
    if not isinstance(value, list):
        _fail('"{}" must be a list of strings.'.format(label))
    if len(value) > max_items:
        _fail('"{}" has too many items: {} is more than {}.'.format(label, len(value), max_items))
    out = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, str):
            _fail('"{}" item {} is not text.'.format(label, index))
        if len(item) > max_chars:
            _fail('"{}" item {} is too long: the limit is {} characters.'.format(label, index, max_chars))
        out.append(item)
    return out


def _sub_object(raw, label):
    """Return a dict for an optional nested object (None -> {})."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _fail('"{}" must be a JSON object.'.format(label))
    return raw


def _mixed_map(raw, label, defaults, rules):
    """Merge a nested object over `defaults`, validating the keys listed in `rules`.

    rules = {key: ("int"|"float", low, high)}; keys we do not act on are kept as given.
    """
    data = _sub_object(raw, label)
    out = dict(defaults)
    for key, value in data.items():
        rule = rules.get(key)
        if rule is None:
            out[key] = value
            continue
        kind, low, high = rule
        item_label = "{}.{}".format(label, key)
        out[key] = _num_int(value, item_label, low, high) if kind == "int" \
            else _num_float(value, item_label, low, high)
    return out


def _path_string(value, label):
    """A file path as written in the profile: text, non-empty, sane length, no control characters."""
    if not isinstance(value, str) or not value.strip():
        _fail('"{}" must be a file path such as "FACTS.json".'.format(label))
    path = value.strip()
    if len(path) > MAX_PATH:
        _fail('"{}" is too long.'.format(label))
    if any(ch in path for ch in _BAD_PATH_CHARS):
        _fail('"{}" contains a control character.'.format(label))
    return path


# ── profile pieces ──────────────────────────────────────────────────────────────────────────────────────────
def _site(value):
    """Validate and normalise `site` to "<scheme>://<host>[:port]<path>" ending with "/"."""
    if not isinstance(value, str) or not value.strip():
        _fail('"site" must be a URL string such as "https://example.com/".')
    raw = value.strip()
    if len(raw) > MAX_PATH:
        _fail('"site" is too long.')
    if any(ch.isspace() for ch in raw) or "\x00" in raw:
        _fail('"site" must not contain whitespace.')
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        _fail('"site" has an invalid port number.')
    scheme = (parts.scheme or "").lower()
    if scheme not in _SITE_SCHEMES:
        _fail('"site" must start with https:// or http://.')
    if parts.username or parts.password:
        _fail('"site" must not contain a username or password.')
    if parts.query or parts.fragment:
        _fail('"site" must be a plain origin: drop the "?query" and "#fragment".')
    host = (parts.hostname or "").strip().lower()
    if not host:
        _fail('"site" needs a host name such as "example.com".')
    netloc = host if port is None else "{}:{}".format(host, port)
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return "{}://{}{}".format(scheme, netloc, path)


def _host(value):
    """Validate an explicit `host` (used for same-site comparisons; defaults to the site's netloc)."""
    if not isinstance(value, str) or not value.strip():
        _fail('"host" must be a host name such as "example.com".')
    host = value.strip().lower().rstrip("/")
    if not host or len(host) > 253 or "://" in host or "/" in host or "@" in host:
        _fail('"host" must be a bare host name such as "example.com".')
    if any(ch.isspace() for ch in host):
        _fail('"host" must not contain whitespace.')
    return host


def _author(raw):
    """Validate the optional author block; every field falls back to "" so callers never see a missing key."""
    data = _sub_object(raw, "author")
    out = {"name": "", "url": "", "id": ""}
    for key in ("name", "url", "id"):
        if key not in data or data[key] is None:
            continue
        value = data[key]
        if not isinstance(value, str):
            _fail('"author.{}" must be text.'.format(key))
        if len(value) > MAX_ITEM_CHARS:
            _fail('"author.{}" is too long: the limit is {} characters.'.format(key, MAX_ITEM_CHARS))
        out[key] = value.strip()
    return out


def _rulings(raw):
    """Validate the rulings block; each list defaults to [] and unknown keys are kept as given."""
    data = _sub_object(raw, "rulings")
    out = {"banned_phrases": [], "excluded_areas": [], "competitors": [], "own_brand": []}
    for key, value in data.items():
        label = "rulings.{}".format(key)
        out[key] = _str_list(value, label) if key in out else value
    return out


def _backends(raw, label):
    """Keep the writer/reviewer list as given (the backends themselves are validated elsewhere)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        _fail('"{}" must be a list.'.format(label))
    return list(raw)


def _resolve(directory, path):
    """Resolve a profile-relative path against the profile directory and return an absolute path."""
    if os.path.isabs(path):
        return os.path.abspath(os.path.normpath(path))
    return os.path.abspath(os.path.join(directory, path))


# ── public API: profile ────────────────────────────────────────────────────────────────────────────────────
def load_profile(path):
    """Load and validate a site profile, filling every default and resolving file paths.

    Refuses (ToolError) a missing/unreadable/oversize file, non-JSON, a bad `site`/`host`, a missing `facts_file`,
    and lists that are not lists of short strings.
    """
    text = _read_text(path, MAX_PROFILE_BYTES, "profile",
                      'no profile file at "{}": create one with "site" and "facts_file".'.format(scrub(path, 200)))
    data = _parse_object(text, "profile")

    if "site" not in data or data["site"] is None:
        _fail('the profile needs "site" (for example "https://example.com/").')
    site = _site(data["site"])
    host = _host(data["host"]) if data.get("host") is not None else urllib.parse.urlsplit(site).netloc

    if "facts_file" not in data or data["facts_file"] is None:
        _fail('the profile needs "facts_file" (for example "FACTS.json").')
    facts_file = _path_string(data["facts_file"], "facts_file")
    lessons_file = _path_string(data.get("lessons_file", "LESSONS.md"), "lessons_file")

    allowed = data.get("allowed_source_domains")
    allowed = [host] if allowed is None else _str_list(allowed, "allowed_source_domains")

    editor = data.get("editor")
    if editor is not None and not isinstance(editor, dict):
        _fail('"editor" must be a JSON object or null.')

    profile = {
        "site": site,
        "host": host,
        "facts_file": facts_file,
        "lessons_file": lessons_file,
        "author": _author(data.get("author")),
        "rulings": _rulings(data.get("rulings")),
        "allowed_source_domains": allowed,
        "small_number_max": _num_int(data.get("small_number_max", 12), "small_number_max", 0, 1000),
        "min_words": _mixed_map(data.get("min_words"), "min_words", {"new": 700, "extend": 150},
                                {"new": ("int", 0, 100_000), "extend": ("int", 0, 100_000)}),
        "extend": _mixed_map(data.get("extend"), "extend", {"min_retention": 0.85, "max_changed_sentences": 3},
                             {"min_retention": ("float", 0.0, 1.0), "max_changed_sentences": ("int", 0, 1000)}),
        "max_duplicate": _mixed_map(data.get("max_duplicate"), "max_duplicate",
                                    {"jaccard": 0.30, "containment": 0.50},
                                    {"jaccard": ("float", 0.0, 1.0), "containment": ("float", 0.0, 1.0)}),
        "writers": _backends(data.get("writers"), "writers"),
        "reviewers": _backends(data.get("reviewers"), "reviewers"),
        "editor": editor,
        "caps": _mixed_map(data.get("caps"), "caps",
                           {"max_cost_usd": 2.0, "max_tokens": 250_000, "max_calls": 8, "deadline_s": 2400,
                            "max_model_stages_per_day": 6},
                           {"max_cost_usd": ("float", 0.0, 1_000_000.0), "max_tokens": ("int", 1, 100_000_000),
                            "max_calls": ("int", 1, 10_000), "deadline_s": ("float", 0.0, 1_000_000.0),
                            "max_model_stages_per_day": ("int", 1, 1000)}),
        "publish": _sub_object(data.get("publish"), "publish"),
        # PM (pipeline keys): official pages the writer may quote, state/bundle dirs, Search Console property, corpus cap
        "reference_sources": _str_list(data.get("reference_sources") or [], "reference_sources", 12, 500),
        "state_dir": _path_string(data.get("state_dir", "state"), "state_dir"),
        "bundle_dir": _path_string(data.get("bundle_dir", "bundles"), "bundle_dir"),
        "gsc_site": _str_list([data["gsc_site"]], "gsc_site", 1, 300)[0] if data.get("gsc_site") else None,
        "max_corpus_pages": _num_int(data.get("max_corpus_pages", 400), "max_corpus_pages", 1, 400),
        "exclude_paths": _str_list(data.get("exclude_paths") or [], "exclude_paths", 100, 200),
        "revision_rounds": _num_int(data.get("revision_rounds", 1), "revision_rounds", 0, 1),
    }
    for u in profile["reference_sources"]:
        if not u.startswith("https://"):
            _fail('reference_sources must be https URLs on allowed_source_domains.')

    directory = os.path.dirname(os.path.abspath(path.strip()))
    profile["_dir"] = directory
    profile["_facts_path"] = _resolve(directory, facts_file)
    profile["_lessons_path"] = _resolve(directory, lessons_file)
    return profile


# ── public API: facts file ────────────────────────────────────────────────────────────────────────────────
def load_facts(path):
    """Load an APPROVED facts file -> {"text", "facts", "approved_by", "approved_at"}.

    Refused (ToolError): missing file, non-JSON, `"proposed": true`, missing/blank `approved_by` or `approved_at`,
    zero facts, more than 500 facts, a fact without text, or a fact text longer than 1000 characters.
    """
    text = _read_text(path, MAX_FACTS_BYTES, "facts",
                      _NO_FACTS_MSG.format(path=scrub(str(path), MAX_ITEM_CHARS)))
    data = _parse_object(text, "facts")

    if data.get("proposed"):
        _fail('this facts file is still a proposal ("proposed": true): a human must review it, set approved_by and '
              'approved_at, and remove "proposed" before the pipeline drafts.')
    version = data.get("version", 1)
    if isinstance(version, bool) or version != 1:
        _fail('the facts file has an unsupported "version": {}.'.format(scrub(version, 20)))

    approved_by = data.get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        _fail('the facts file needs "approved_by": a named human must approve it before the pipeline drafts.')
    approved_by = approved_by.strip()[:MAX_ITEM_CHARS]

    approved_at = data.get("approved_at")
    if not isinstance(approved_at, str) or not approved_at.strip():
        _fail('the facts file needs "approved_at" (the day the human approved it, YYYY-MM-DD).')
    approved_at = approved_at.strip()[:40]
    if _ISO_DAY_RE.match(approved_at):
        try:
            date(int(approved_at[0:4]), int(approved_at[5:7]), int(approved_at[8:10]))
        except (ValueError, TypeError):
            _fail('"approved_at" is not a real date: "{}".'.format(scrub(approved_at, 40)))

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        _fail('the facts file needs "facts" as a list of objects.')
    if not raw_facts:
        _fail("the facts file has no facts: an approval that lists nothing approves nothing.")
    if len(raw_facts) > MAX_FACTS:
        _fail("the facts file has {} facts; the limit is {}.".format(len(raw_facts), MAX_FACTS))

    facts = []
    lines = []
    for index, item in enumerate(raw_facts, 1):
        if not isinstance(item, dict):
            _fail("fact {} is not a JSON object.".format(index))
        body = item.get("text")
        if not isinstance(body, str) or not body.strip():
            _fail('fact {} has no "text".'.format(index))
        clean = " ".join(body.split())
        if len(clean) > MAX_FACT_CHARS:
            _fail("fact {} is {} characters long; the limit is {}.".format(index, len(clean), MAX_FACT_CHARS))
        source = item.get("source", "")
        if source is None:
            source = ""
        if not isinstance(source, str):
            _fail("fact {} has a source that is not text.".format(index))
        fact_id = item.get("id")
        if isinstance(fact_id, str) and fact_id.strip():
            fact_id = fact_id.strip()[:64]
        else:
            fact_id = "f-" + hashlib.sha1(numbers.norm(clean).encode("utf-8")).hexdigest()[:8]
        facts.append({"id": fact_id, "text": clean, "source": source.strip()[:MAX_PATH]})
        lines.append(clean)

    return {"text": "\n".join(lines), "facts": facts, "approved_by": approved_by, "approved_at": approved_at}


# ── public API: proposals ─────────────────────────────────────────────────────────────────────────────────
def _want_fact(sentence):
    """True when a sentence carries a numeric/money/percent claim or a first-person business claim."""
    if not sentence:
        return False
    return bool(_CLAIM_RE.search(sentence) or _FIRST_PERSON_RE.search(sentence))


def _cap(max_facts):
    """Sanitise max_facts; nonsense falls back to 60 and everything is clamped to [1, MAX_PROPOSED]."""
    try:
        value = int(max_facts)
    except Exception:
        return 60
    return max(1, min(MAX_PROPOSED, value))


def propose(pages, max_facts=60):
    """Build a facts PROPOSAL from fetched pages ({url: text}); never approves anything.

    Picks sentences 20..300 chars that carry a digit, "$", "%" or a first-person business claim (we|our|us), dedupes
    them on their normalised form, and returns {"version": 1, "proposed": true, "approved_by": None,
    "approved_at": None, "note": ..., "facts": [{"id", "text", "source"}]}. Nothing is written and no file is touched.
    """
    limit = _cap(max_facts)
    if not isinstance(pages, dict):
        pages = {}
    facts = []
    seen = set()
    for url, body in list(pages.items())[:MAX_PAGES]:
        if not isinstance(url, str) or not url.strip():
            continue
        source = url.strip()[:MAX_PATH]
        if not isinstance(body, str):
            body = "" if body is None else str(body)
        for sentence in numbers.sentences(body[:MAX_PAGE_TEXT]):
            clean = " ".join(sentence.split())
            if not (MIN_FACT_SENTENCE <= len(clean) <= MAX_FACT_SENTENCE):
                continue
            if not _want_fact(clean):
                continue
            key = numbers.norm(clean)
            if not key or key in seen:
                continue
            seen.add(key)
            facts.append({"id": "f-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8],
                          "text": clean, "source": source})
            if len(facts) >= limit:
                break
        if len(facts) >= limit:
            break
    return {"version": 1, "proposed": True, "approved_by": None, "approved_at": None, "note": _NOTE, "facts": facts}


def write_proposal(path, proposal):
    """Write a proposal as JSON (indent 1, mode 0644) to `path`, refusing to overwrite an existing file.

    The write goes to a temp file next to the target and is moved into place with os.replace, so a crash never leaves
    a half-written proposal behind. Returns the absolute path written.
    """
    if not isinstance(proposal, dict):
        _fail("write_proposal needs the proposal object that propose() returns.")
    target = _path_string(path, "proposal path")
    try:
        payload = json.dumps(proposal, indent=1, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        _fail("the proposal is not plain JSON data and cannot be written.")
    if len(payload) > MAX_PROPOSAL_BYTES:
        _fail("the proposal is larger than {} KB; propose fewer facts.".format(MAX_PROPOSAL_BYTES // 1024))
    absolute = os.path.abspath(target)
    if os.path.exists(absolute):
        _fail('"{}" already exists: refusing to overwrite a facts file a human may have reviewed.'.format(
            scrub(absolute, MAX_ITEM_CHARS)))
    directory = os.path.dirname(absolute) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        _fail('could not create the directory "{}": {}'.format(scrub(directory, MAX_ITEM_CHARS), scrub(exc, 120)))
    handle, temporary = tempfile.mkstemp(prefix=".facts-proposal-", suffix=".tmp", dir=directory)
    moved = False
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
        os.chmod(temporary, 0o644)
        if os.path.exists(absolute):
            _fail('"{}" already exists: refusing to overwrite a facts file a human may have reviewed.'.format(
                scrub(absolute, MAX_ITEM_CHARS)))
        os.replace(temporary, absolute)
        moved = True
    finally:
        if not moved and os.path.exists(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass
    return absolute


# ── selftest ───────────────────────────────────────────────────────────────────────────────────────────────
def selftest() -> int:
    n = 0

    def chk(cond, msg):
        nonlocal n
        assert cond, "facts.selftest: " + msg
        n += 1

    def dump(path, data):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def raw(path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def fails(fn, needle=""):
        try:
            fn()
        except ToolError as exc:
            return needle in str(exc)
        except Exception:
            return False
        return False

    with tempfile.TemporaryDirectory() as root:
        base = os.path.abspath(root)

        # ── load_profile: minimal profile, every default filled ───────────────────────────────────────
        minimal = dump(os.path.join(root, "site.json"), {"site": "https://example.com", "facts_file": "FACTS.json"})
        prof = load_profile(minimal)
        chk(prof["site"] == "https://example.com/", "site is normalised to end with /")
        chk(prof["host"] == "example.com", "host is derived from site")
        chk(prof["facts_file"] == "FACTS.json", "facts_file is kept as written")
        chk(prof["_facts_path"] == os.path.join(base, "FACTS.json"), "_facts_path is absolute")
        chk(prof["lessons_file"] == "LESSONS.md", "lessons_file defaults to LESSONS.md")
        chk(prof["_lessons_path"] == os.path.join(base, "LESSONS.md"), "_lessons_path is absolute")
        chk(prof["_dir"] == base, "_dir is the profile directory")
        chk(prof["rulings"] == {"banned_phrases": [], "excluded_areas": [], "competitors": [], "own_brand": []},
            "rulings default to four empty lists")
        chk(prof["allowed_source_domains"] == ["example.com"], "allowed_source_domains defaults to the host")
        chk(prof["author"] == {"name": "", "url": "", "id": ""}, "author defaults to empty fields")
        chk(prof["small_number_max"] == 12, "small_number_max defaults to 12")
        chk(prof["min_words"] == {"new": 700, "extend": 150}, "min_words defaults")
        chk(prof["extend"] == {"min_retention": 0.85, "max_changed_sentences": 3}, "extend defaults")
        chk(prof["max_duplicate"] == {"jaccard": 0.30, "containment": 0.50}, "max_duplicate defaults")
        chk(prof["caps"] == {"max_cost_usd": 2.0, "max_tokens": 250000, "max_calls": 8, "deadline_s": 2400, "max_model_stages_per_day": 6},
            "caps defaults")
        chk(prof["writers"] == [] and prof["reviewers"] == [] and prof["editor"] is None,
            "writers/reviewers default to empty and editor to null")
        chk(prof["publish"] == {}, "publish defaults to an empty object")
        chk(prof["site"].endswith("/") and "?" not in prof["site"] and "#" not in prof["site"],
            "the normalised site carries no query or fragment")

        # ── load_profile: a full profile keeps what it is given ──────────────────────────────────────
        full = dump(os.path.join(root, "full.json"), {
            "site": "http://example.com", "host": "www.example.com", "facts_file": "docs/FACTS.json",
            "lessons_file": "/tmp/elsewhere/LESSONS.md",
            "author": {"name": "Jane Doe", "url": "/team/jane/", "id": "https://example.com/team/jane/#person"},
            "rulings": {"banned_phrases": ["guaranteed returns"], "excluded_areas": ["Springfield"],
                        "competitors": ["Acme PM"], "own_brand": ["Example Co"]},
            "allowed_source_domains": ["irs.gov", "*.gov", "example.com"],
            "small_number_max": 5, "min_words": {"new": 900},
            "extend": {"min_retention": 0.9, "max_changed_sentences": 2},
            "max_duplicate": {"jaccard": 0.25, "containment": 0.45},
            "writers": [{"kind": "deepseek"}], "reviewers": [{"kind": "glm"}], "editor": {"kind": "glm"},
            "caps": {"max_cost_usd": 1.5, "max_tokens": 1000, "max_calls": 4, "deadline_s": 60},
            "publish": {"static_dir": "public"},
        })
        pf = load_profile(full)
        chk(pf["site"] == "http://example.com/", "an http site keeps its origin and gains a trailing slash")
        chk(pf["host"] == "www.example.com", "an explicit host wins over the derived one")
        chk(pf["_facts_path"] == os.path.join(base, "docs", "FACTS.json"),
            "a relative facts_file resolves against the profile directory")
        chk(pf["_lessons_path"] == "/tmp/elsewhere/LESSONS.md", "an absolute lessons_file is kept")
        chk(pf["small_number_max"] == 5 and pf["min_words"]["new"] == 900, "explicit numbers are kept")
        chk(pf["min_words"]["extend"] == 150, "a partial min_words keeps the documented default for extend")
        chk(pf["rulings"]["competitors"] == ["Acme PM"], "rulings lists are kept")
        chk(pf["allowed_source_domains"] == ["irs.gov", "*.gov", "example.com"], "allowed_source_domains are kept")
        chk(pf["writers"] == [{"kind": "deepseek"}] and pf["editor"] == {"kind": "glm"},
            "writers and editor are kept as given")
        chk(pf["publish"] == {"static_dir": "public"}, "publish is kept as given")
        chk(pf["extend"]["min_retention"] == 0.9 and pf["caps"]["max_calls"] == 4, "nested numbers are kept")

        # ── load_profile: paths resolve relative to the profile file's own directory ─────────────────
        nested = os.path.join(root, "sites")
        os.makedirs(nested, exist_ok=True)
        psub = load_profile(dump(os.path.join(nested, "p.json"),
                                 {"site": "https://a.example.com/", "facts_file": "FACTS.json"}))
        chk(psub["_facts_path"] == os.path.join(os.path.abspath(nested), "FACTS.json"),
            "facts_file resolves against a nested profile directory")
        chk(psub["_dir"] == os.path.abspath(nested), "_dir is the nested profile directory")
        chk(psub["host"] == "a.example.com", "host comes from the nested site")

        # ── load_profile: refusals ──────────────────────────────────────────────────────────────────
        chk(fails(lambda: load_profile(os.path.join(root, "absent.json")), "no profile file"),
            "a missing profile file refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p1.json"), {"facts_file": "FACTS.json"})), "site"),
            "a profile without site refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p2.json"), {"site": "https://example.com/"})),
                  "facts_file"), "a profile without facts_file refuses")
        chk(fails(lambda: load_profile(raw(os.path.join(root, "p3.json"), "{not json")), "JSON"),
            "a profile that is not JSON refuses")
        chk(fails(lambda: load_profile(raw(os.path.join(root, "p4.json"), "[]")), "object"),
            "a profile that is not an object refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p5.json"),
                                            {"site": "ftp://example.com/", "facts_file": "F"})), "https"),
            "a non-http site refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p6.json"), {"site": 12, "facts_file": "F"})), "site"),
            "a non-text site refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p7.json"),
                                            {"site": "   ", "facts_file": "F"})), "site"),
            "a blank site refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p8.json"),
                                            {"site": "https://user:pw@example.com/", "facts_file": "F"})),
                  "password"), "credentials inside site refuse")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "p9.json"),
                                            {"site": "https://example.com/?x=1", "facts_file": "F"})), "query"),
            "a site with a query refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pa.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "rulings": {"banned_phrases": "guaranteed"}})), "banned_phrases"),
            "a rulings list given as text refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pb.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "rulings": {"banned_phrases": ["x"] * 201}})), "200"),
            "a ruling list over 200 items refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pc.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "rulings": {"excluded_areas": ["x" * 201]}})), "200"),
            "a ruling over 200 characters refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pd.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "allowed_source_domains": ["ok.gov", 7]})), "allowed_source_domains"),
            "a non-text source domain refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pe.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "small_number_max": "12"})), "small_number_max"),
            "a text small_number_max refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pf.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "min_words": "700"})), "min_words"),
            "a text min_words refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pg.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "max_duplicate": {"jaccard": 1.5}})), "jaccard"),
            "out-of-range max_duplicate refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "ph.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "caps": {"max_calls": 0}})), "max_calls"),
            "a zero cap refuses")
        chk(fails(lambda: load_profile(dump(os.path.join(root, "pi.json"),
                                            {"site": "https://example.com/", "facts_file": "F",
                                             "author": {"name": 5}})), "author.name"),
            "a non-text author name refuses")
        extra = load_profile(dump(os.path.join(root, "pj.json"),
                                  {"site": "https://example.com/", "facts_file": "F", "notes": "ignored"}))
        chk(extra["site"] == "https://example.com/", "a profile with an unknown extra key still loads")
        chk(fails(lambda: load_profile(raw(os.path.join(root, "pk.json"),
                                           '{"site": "https://example.com/", "facts_file": "' + "x" * 300000 + '"}')),
                  "larger"), "an oversize profile file refuses")

        # ── load_facts: the good path ───────────────────────────────────────────────────────────────
        approved = {"version": 1, "approved_by": "Jane Doe", "approved_at": "2026-11-10",
                    "facts": [{"id": "mgmt-fee", "text": "Management is $149 per month per home.",
                               "source": "https://example.com/pricing/"},
                              {"id": "fee-2", "text": "Fees\nare billed  monthly.", "source": ""}]}
        good_path = dump(os.path.join(root, "FACTS.json"), approved)
        loaded = load_facts(good_path)
        chk(loaded["approved_by"] == "Jane Doe" and loaded["approved_at"] == "2026-11-10",
            "approved_by and approved_at are returned")
        chk(len(loaded["facts"]) == 2, "every approved fact is returned")
        chk(loaded["text"] == "Management is $149 per month per home.\nFees are billed monthly.",
            "facts text is one fact per line with whitespace collapsed")
        chk(loaded["facts"][0]["id"] == "mgmt-fee" and loaded["facts"][1]["id"] == "fee-2", "fact ids are kept")
        chk(set(loaded) == {"text", "facts", "approved_by", "approved_at"}, "the result has exactly the four keys")
        chk(load_facts(dump(os.path.join(root, "FACTS-ok.json"), dict(approved, proposed=False)))["approved_by"]
            == "Jane Doe", '"proposed": false is accepted (only a live proposal is refused)')

        # ── load_facts: refusals ───────────────────────────────────────────────────────────────────
        chk(fails(lambda: load_facts(os.path.join(root, "missing.json")), "no approved facts file"),
            "a missing facts file refuses with the facts message")
        chk(fails(lambda: load_facts(os.path.join(root, "missing.json")), "sets approved_by"),
            "the facts refusal message says what to do next")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F1.json"), dict(approved, proposed=True))), "proposal"),
            "a proposed facts file refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F2.json"),
                                          {k: v for k, v in approved.items() if k != "approved_by"})), "approved_by"),
            "a missing approved_by refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F3.json"), dict(approved, approved_by="  "))),
                  "approved_by"), "a blank approved_by refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F4.json"),
                                          {k: v for k, v in approved.items() if k != "approved_at"})), "approved_at"),
            "a missing approved_at refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F5.json"), dict(approved, approved_at="2026-02-30"))),
                  "real date"), "an impossible approved_at date refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F6.json"), dict(approved, facts=[]))), "no facts"),
            "zero facts refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F7.json"),
                                          dict(approved, facts=[{"text": "We serve homes in Ohio."}] * 501))), "500"),
            "more than 500 facts refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F8.json"),
                                          dict(approved, facts=[{"text": "y" * 1001}]))), "1000"),
            "a fact text over 1000 characters refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "F9.json"), dict(approved, facts={"a": 1}))), "facts"),
            "a non-list facts value refuses")
        chk(fails(lambda: load_facts(dump(os.path.join(root, "FA.json"), dict(approved, facts=[{"id": "x"}]))),
                  "text"), "a fact without text refuses")
        chk(fails(lambda: load_facts(raw(os.path.join(root, "FB.json"), "not json at all")), "JSON"),
            "invalid facts JSON refuses")
        chk(fails(lambda: load_facts(raw(os.path.join(root, "FC.json"),
                                         '{"approved_by": "J", "approved_at": "2026-11-10", "facts": [' +
                                         "{" * 200000)), "JSON"), "a hostile facts file refuses instead of crashing")

        # ── propose ────────────────────────────────────────────────────────────────────────────────
        pages = {
            "https://example.com/pricing/": ("Our fee is $149 per month per home. We serve 300 homes across Ohio. "
                                             "Management is simple. Tiny $5."),
            "https://example.com/about/": ("Our fee is $149 per month per home. We are a local team with roots here."),
        }
        prop = propose(pages)
        texts = [fact["text"] for fact in prop["facts"]]
        chk(prop["version"] == 1 and prop["proposed"] is True, "a proposal is marked proposed")
        chk(prop["approved_by"] is None and prop["approved_at"] is None, "propose never approves anything")
        chk(prop["note"].startswith("Review every line."), "the proposal carries the review note")
        chk("Our fee is $149 per month per home." in texts, "a money sentence becomes a fact")
        chk("We serve 300 homes across Ohio." in texts, "a first-person numeric sentence becomes a fact")
        chk("We are a local team with roots here." in texts, "a first-person claim becomes a fact")
        chk(texts.count("Our fee is $149 per month per home.") == 1, "duplicate sentences are deduped")
        chk("Management is simple." not in texts, "plain prose is not a fact")
        chk("Tiny $5." not in texts, "a sentence shorter than 20 characters is not a fact")
        chk(all(MIN_FACT_SENTENCE <= len(t) <= MAX_FACT_SENTENCE for t in texts), "fact lengths stay in range")
        chk(all(f["id"].startswith("f-") and len(f["id"]) == 10 for f in prop["facts"]), "fact ids are f-<sha1[:8]>")
        chk(all(f["source"] in pages for f in prop["facts"]), "every fact names the page it came from")
        chk(prop == propose(pages), "propose is deterministic")
        chk(len(propose(pages, max_facts=1)["facts"]) == 1, "max_facts caps the proposal")
        chk(propose(pages, max_facts="nonsense")["facts"] == prop["facts"], "a nonsense max_facts falls back to 60")
        chk(propose({})["facts"] == [] and propose(None)["facts"] == [], "an empty or None page map proposes nothing")
        chk(propose({"https://example.com/x/": "9" * 300000})["facts"] == [], "propose survives a hostile huge page")
        many = " ".join("Our team served {} homes in year {}.".format(i, 2000 + i) for i in range(10))
        chk(len(propose({"https://example.com/m/": many}, max_facts=4)["facts"]) == 4,
            "max_facts caps a long page")
        chk(propose({7: "We serve 300 homes in Ohio.", "https://example.com/y/": "We serve 301 homes in Ohio."})
            ["facts"][0]["source"] == "https://example.com/y/", "a non-text url is skipped")
        chk(all("proposed" not in fact for fact in prop["facts"]), "facts themselves carry no approval flag")

        # ── write_proposal ─────────────────────────────────────────────────────────────────────────
        out = os.path.join(root, "review", "FACTS.proposed.json")
        written = write_proposal(out, prop)
        chk(os.path.exists(out), "write_proposal creates the file")
        chk(written == os.path.abspath(out), "write_proposal returns the absolute path")
        chk((os.stat(out).st_mode & 0o777) == 0o644, "the proposal file is mode 0644")
        with open(out, encoding="utf-8") as stream:
            body = stream.read()
        chk(body == json.dumps(prop, indent=1) + "\n", "the proposal is indent-1 JSON with a trailing newline")
        chk(json.loads(body)["approved_by"] is None, "the written proposal is still unapproved")
        chk(fails(lambda: write_proposal(out, prop), "already exists"), "write_proposal refuses to overwrite")
        with open(out, encoding="utf-8") as stream:
            chk(stream.read() == body, "a refused overwrite leaves the file untouched")
        chk(fails(lambda: write_proposal(os.path.join(root, "bad.json"), ["nope"]), "proposal"),
            "write_proposal refuses a non-object")
        chk(not os.path.exists(os.path.join(root, "bad.json")), "a refused write creates no file")
        second = os.path.join(root, "review2", "FACTS.proposed.json")
        chk(write_proposal(second, propose({})) == os.path.abspath(second), "an empty proposal also writes")
        chk(not [f for f in os.listdir(os.path.dirname(out)) if f.endswith(".tmp")],
            "no temp file is left behind")

    return n
