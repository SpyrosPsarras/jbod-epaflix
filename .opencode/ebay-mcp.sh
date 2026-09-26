#!/bin/sh
# Starts ebay-mcp with its eBay keys read from the KeePass vault at launch,
# so no secret lives in the repo or a dotfile. Needs MCP_HUB_URL/MCP_HUB_TOKEN
# (set in the t3code pod).
set -e
V=/home/spyros/.agents/skills/keepassxc-secrets/scripts/vault.py
envs=$(python3 "$V" vault_get '{"path":"/KeePassXC-Browser Passwords/developer.ebay.com","include_password":false}' \
  | python3 -c 'import json,sys,shlex; p=json.load(sys.stdin)["custom_properties"]; print("export EBAY_CLIENT_ID=" + shlex.quote(p["EBAY_CLIENT_ID"])); print("export EBAY_CLIENT_SECRET=" + shlex.quote(p["EBAY_CLIENT_SECRET"]))') \
  || { echo "ebay-mcp.sh: could not read eBay keys from the vault" >&2; exit 1; }
eval "$envs"
: "${EBAY_CLIENT_ID:?missing}" "${EBAY_CLIENT_SECRET:?missing}"
exec npx -y ebay-mcp
