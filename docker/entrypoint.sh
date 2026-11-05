#!/bin/sh
# Serve AdRails over stdio. No config -> scaffold an offline demo once (placeholder campaign, commits OFF, no credentials).
set -e
if [ -z "$ADSOPS_CONFIG" ] || [ ! -f "$ADSOPS_CONFIG" ]; then
  DEMO="$HOME/demo"
  if [ ! -f "$DEMO/config.json" ]; then
    adrails init --dir "$DEMO" --customer-id 1234567890 --campaign-id 1 --sa "$HOME/no-key.json" --offline >/dev/null 2>&1
  fi
  ADSOPS_CONFIG="$DEMO/config.json"
fi
exec adrails --config "$ADSOPS_CONFIG" "$@"
