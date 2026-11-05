"""python3 -m adsops.intel --selftest   (runs every intel module's selftest; all network calls are mocked)

Commands live in the modules:
  python3 -m adsops.intel.keywords  seed ...
  python3 -m adsops.intel.collector --config CFG estimate|plan|run|collect|resolve-locations|status
  python3 -m adsops.intel.auction_insights import|show --config CFG [--file F]   (free: Ads UI export / pasted table)
  python3 -m adsops.intel.transparency run|status --config CFG [--live]           (free: Transparency Center, capped)
  python3 -m adsops.intel.ownstats  snapshot|show --config CFG                    (free: own Google Ads API, read-only)
  python3 -m adsops.intel.report    --config CFG [--write]
"""
import importlib
import sys

MODULES = ("config", "keywords", "creds", "dataforseo", "store", "collector", "analysis", "calibrate", "auction_insights",
           "transparency", "ownstats", "report")


def selftest():
    total = 0
    for name in MODULES:
        n = importlib.import_module("adsops.intel." + name).selftest()
        print(f"  {name:12s} {n} checks", file=sys.stderr)
        total += n
    print(f"ALL PASS ({total} checks)")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
