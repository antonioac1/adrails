"""adsops.devices — desired-state device bid modifiers (campaign-level DEVICE criteria).

Managed field (optional; absent = unmanaged, never touched):

  state["device_bid_modifiers"] = {"MOBILE": 0.0, "DESKTOP": 1.0, "TABLET": 1.0}

Semantics are Google's: 0.0 = opt the device out (-100%), 1.0 = no adjustment, 0.1-0.99 = a reduction.

Rail: a modifier may only REDUCE or REMOVE an adjustment, never raise bids: allowed values are 0.0 or
0.1..1.0. A raise above 1.0 is spend exposure, so it is refused here and in engine.check_op_fields
(defence in depth). Devices are existing criteria on every Search campaign, so the only op is a
field-masked update of bidModifier on the live criterion; nothing is ever created or removed.

Live read note (earned 2026-11-09): a device with NO adjustment omits bid_modifier in the API
response. Some readers render the omitted field as 0, which looks exactly like an opt-out while the
device is serving. Treat a missing value as 1.0 (no adjustment), never as 0. Pure: no I/O.
"""
from .errors import RailViolation, ToolError

KEY = "device_bid_modifiers"
DEVICES = ("MOBILE", "DESKTOP", "TABLET")


def manages_devices(state):
    return isinstance(state, dict) and KEY in state


def check_modifier(v, what="bid modifier"):
    """-> float; RailViolation unless v is 0.0 or within 0.1..1.0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ToolError(f"{what} must be a number")
    v = float(v)
    if v == 0.0 or 0.1 - 1e-9 <= v <= 1.0 + 1e-9:
        return round(v, 4)
    raise RailViolation(f"{what} {v} not allowed: only 0 (opt out) or 0.1-1.0 (reduce); raising bids is refused")


def validate_state(state):
    want = state.get(KEY)
    if not isinstance(want, dict) or not want:
        raise ToolError(f"state.{KEY} must be a non-empty object of DEVICE -> modifier")
    out = {}
    for dev, v in want.items():
        d = str(dev).upper()
        if d not in DEVICES:
            raise ToolError(f"state.{KEY}: unknown device {dev!r} (MOBILE|DESKTOP|TABLET)")
        out[d] = check_modifier(v, f"state.{KEY}.{d}")
    if all(v == 0.0 for v in out.values()) and len(out) == len(DEVICES):
        raise RailViolation("refusing to opt out of every device (that is a pause; use status=PAUSED)")
    return out


def plan_devices(state, live):
    """-> (campaignCriteria update ops, notes). Pure."""
    if not manages_devices(state):
        return [], []
    want = validate_state(state)
    ld = live.get("devices") if isinstance(live, dict) else None
    if not isinstance(ld, dict):
        raise ToolError("live state has no device read (live['devices']); refusing to diff device bids blind")
    ops, notes = [], []
    for dev in sorted(want):
        cur = ld.get(dev)
        if not isinstance(cur, dict) or not cur.get("resource_name"):
            raise RailViolation(f"device {dev} has no live criterion on this campaign")
        have = 1.0 if cur.get("bid_modifier") is None else float(cur["bid_modifier"])
        if abs(have - want[dev]) > 1e-6:
            ops.append({"update": {"resourceName": cur["resource_name"], "bidModifier": want[dev]},
                        "updateMask": "bidModifier"})
            notes.append(f"device {dev}: bid modifier {have:g} -> {want[dev]:g}")
    return ops, notes


def is_device_update(e):
    """True for exactly a field-masked bidModifier update with an allowed value (used by check_op_fields)."""
    if not isinstance(e, dict) or set(e) != {"update", "updateMask"} or e["updateMask"] != "bidModifier":
        return False
    u = e["update"]
    if not isinstance(u, dict) or set(u) != {"resourceName", "bidModifier"}:
        return False
    if "/campaignCriteria/" not in str(u["resourceName"]):
        return False
    try:
        check_modifier(u["bidModifier"])
    except (RailViolation, ToolError):
        return False
    return True


def selftest():
    n = 0
    live = {"devices": {"MOBILE": {"resource_name": "customers/1/campaignCriteria/9~30001", "bid_modifier": None},
                        "DESKTOP": {"resource_name": "customers/1/campaignCriteria/9~30000", "bid_modifier": 1.0},
                        "TABLET": {"resource_name": "customers/1/campaignCriteria/9~30002", "bid_modifier": 1.0}}}
    ops, notes = plan_devices({KEY: {"MOBILE": 0}}, live)
    assert len(ops) == 1 and ops[0]["update"]["bidModifier"] == 0.0 and is_device_update(ops[0]); n += 1
    assert notes == ["device MOBILE: bid modifier 1 -> 0"]; n += 1          # missing modifier read as 1.0, not 0
    live["devices"]["MOBILE"]["bid_modifier"] = 0.0
    assert plan_devices({KEY: {"MOBILE": 0}}, live) == ([], []); n += 1      # idempotent
    for bad in (1.5, -0.2, 0.05, "x", True):
        try:
            plan_devices({KEY: {"MOBILE": bad}}, live); raise AssertionError(bad)
        except (RailViolation, ToolError):
            n += 1
    try:
        plan_devices({KEY: {"MOBILE": 0, "DESKTOP": 0, "TABLET": 0}}, live); raise AssertionError("all off")
    except RailViolation:
        n += 1
    try:
        plan_devices({KEY: {"MOBILE": 0}}, {}); raise AssertionError("blind")
    except ToolError:
        n += 1
    assert plan_devices({}, {}) == ([], []); n += 1                            # unmanaged = untouched
    assert not is_device_update({"update": {"resourceName": "customers/1/campaignCriteria/9~1", "bidModifier": 2.0},
                                 "updateMask": "bidModifier"}); n += 1
    assert not is_device_update({"update": {"resourceName": "customers/1/campaigns/9", "bidModifier": 0.0},
                                 "updateMask": "bidModifier"}); n += 1
    return n


if __name__ == "__main__":
    print(f"devices selftest: {selftest()} checks ALL PASS")
