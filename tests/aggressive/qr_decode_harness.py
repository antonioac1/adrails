"""Independent verification of adsops.mail.qr: every generated QR must be decoded by OpenCV back to the exact input.
Not part of the stdlib selftest (needs cv2 + numpy); run: PYTHONPATH=. python3 tests/aggressive/qr_decode_harness.py [N]
Covers: every ECC level x versions 1-10 (forced), auto version, every mask (forced), real tracking URLs, UTF-8, boundary lengths,
and a cross-check against segno (an established encoder) on the module matrix when both pick the same version/mask."""
import random, string, sys
import numpy as np, cv2
from adsops.mail import qr as Q
from adsops.mail import codes as C

DETECTORS = [cv2.QRCodeDetector()] + ([cv2.QRCodeDetectorAruco()] if hasattr(cv2, "QRCodeDetectorAruco") else [])

def decode_matrix(m01):
    """Two OpenCV decoders x 4 scales: a symbol counts as unreadable only if EVERY attempt fails. (Measured 2026-11-05: a single
    detector at one scale misreads ~2.5% of codes from segno, a known-good encoder, so single-shot failures are detector noise.)"""
    m = np.array(m01, dtype=np.uint8)
    base = np.where(m == 1, 0, 255).astype(np.uint8)
    for scale in (4, 6, 8, 10):
        img = cv2.resize(base, (base.shape[1] * scale, base.shape[0] * scale), interpolation=cv2.INTER_NEAREST)
        for d in DETECTORS:
            try:
                txt = d.detectAndDecode(img)[0]
            except cv2.error:
                txt = ""
            if txt:
                return txt
    return ""

def decode(q):
    return decode_matrix(Q.to_matrix(q, border=4))

CAP = {}
def cap(ecc, v):
    # largest byte length that fits version v at ecc, found by probing the encoder's own limit (must raise above it)
    k = (ecc, v)
    if k not in CAP:
        lo = 1
        while True:
            try:
                Q.encode("a" * (lo + 1), ecc=ecc, version=v); lo += 1
            except Exception:
                break
        CAP[k] = lo
    return CAP[k]

def main(n_random=300):
    rng = random.Random(7); fails = []; total = 0
    def check(data, **kw):
        nonlocal total
        total += 1
        try:
            q = Q.encode(data, **kw)
        except Exception as e:
            fails.append((data[:60], kw, "encode error: %s" % e)); return
        got = decode(q)
        if got != data:
            fails.append((data[:60], kw | {"v": q.version, "mask": q.mask}, "decoded %r" % got[:60]))
    # 1. every ECC x forced version, at 1 byte, half capacity and exact capacity
    for ecc in "LMQH":
        for v in range(1, 11):
            c = cap(ecc, v)
            for ln in sorted({1, max(1, c // 2), c}):
                check("".join(rng.choice(string.ascii_letters + string.digits) for _ in range(ln)), ecc=ecc, version=v)
    # 2. every mask forced, at v1, v5, v7 (version-info), v10
    for mask in range(8):
        for v in (3, 5, 7, 10):                       # 31 bytes needs >= v3 at ECC M (v2-M holds 26)
            check("https://adrails.dev/m?c=ABC2345", ecc="M", version=v, mask=mask)
    # 3. real tracking URLs from the codes module, auto version, random ECC
    for code in C.generate(n_random, seed=11):
        url = C.tracking_url("https://acmepm.example/rent-estimate/", code, utm={"utm_source": "direct_mail", "utm_medium": "postcard",
                                                                               "utm_campaign": "2026-12"})
        check(url, ecc=rng.choice("LMQH"))
    # 4. UTF-8 and symbols
    for s in ["Café ☕ — Tampa", "日本語テキスト", "a&b=c?d#e", " leading and trailing ", "x" * 50 + "é"]:
        check(s, ecc="M")
    # 5. over capacity must refuse cleanly
    try:
        Q.encode("z" * 400, ecc="H"); fails.append(("400 bytes", {}, "should refuse"))
    except Exception:
        pass
    # 6. matrix cross-check with segno where version+mask+ecc coincide
    try:
        import segno
        same = diff = 0
        for code in C.generate(60, seed=3):
            url = "https://adrails.dev/x?c=" + code
            q = Q.encode(url, ecc="M", version=4, mask=2)
            s = segno.make_qr(url, error="m", version=4, mask=2, mode="byte", boost_error=False)
            sm = [[1 if b else 0 for b in row] for row in s.matrix]
            qm = [[1 if b else 0 for b in row] for row in q.modules]
            same += sm == qm; diff += sm != qm
        print("segno matrix identical: %d/%d (informational: valid encoders may differ in padding/mask choices)" % (same, same + diff))
        def padded(sq):
            m = [[1 if b else 0 for b in r] for r in sq.matrix]
            w = len(m) + 8
            return [[0] * w for _ in range(4)] + [[0] * 4 + r + [0] * 4 for r in m] + [[0] * w for _ in range(4)]
        ctl = sum(decode_matrix(padded(segno.make_qr(c, error="m", mode="byte"))) == c
                  for c in ["https://adrails.dev/x?c=" + k for k in C.generate(40, seed=5)])
        print("segno control decoded by same harness: %d/40 (proves the harness can read a known-good encoder)" % ctl)
        if ctl < 40:
            fails.append(("control", {}, "harness failed to decode known-good segno codes: %d/40" % ctl))
    except ImportError:
        print("segno not installed: matrix cross-check skipped")
    print("QR decode harness: %d encodes, %d failures" % (total, len(fails)))
    for f in fails[:15]:
        print("  FAIL", f)
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300))
