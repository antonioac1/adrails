# Installing AdRails (for AI agents such as Cline)

AdRails is a **local stdio MCP server** (Python, stdlib-only core). There is no hosted URL and no API key to
obtain for the first run: the lead book, growth plan, AEO toolkit, playbooks and direct-mail tools work as soon as
the server starts. Google Ads / Local Services Ads access is an optional later step the user does in a browser.

The PyPI distribution is `adrails`; the console script is `adsops-mcp`; the Python module is `adsops`.

## 1. Requirements

- Python **3.11 or newer** (`python3 --version`).
- `pipx` (preferred) or a virtual environment. `uvx` also works.

## 2. Install

Pick one:

```bash
pipx install adrails                    # puts `adsops-mcp` on PATH
# or, in a virtual environment:
python3 -m venv ~/.adrails-venv && ~/.adrails-venv/bin/pip install adrails
```

Verify (offline, everything mocked, about a minute):

```bash
adsops-mcp --selftest                   # must end with a line containing "ALL PASS"
```

## 3. Create the workspace and config (no keys needed)

```bash
adsops-mcp setup --dir ~/AdRails --business "<the user's business name>"
```

This prints one JSON object. Note the absolute `config` path it returns (for example
`/Users/<user>/AdRails/config.json`). It never overwrites an existing config.

## 4. Register the server in Cline

Find the absolute path of the executable with `command -v adsops-mcp` (or use `~/.adrails-venv/bin/adsops-mcp`
if you installed into a venv; expand `~` to the real home directory). Add this to `cline_mcp_settings.json`
(Cline panel → MCP Servers → Configure → Configure MCP Servers; CLI: `~/.cline/data/settings/cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "adrails": {
      "command": "/absolute/path/to/adsops-mcp",
      "args": ["--config", "/absolute/path/to/AdRails/config.json"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Both paths must be absolute. Without `pipx`/a venv you can use
`"command": "uvx", "args": ["--from", "adrails", "adsops-mcp", "--config", "/absolute/path/to/AdRails/config.json"]`.

## 5. Check it works

The server should show as connected with about 70 tools (names start with `adsops_`, `aeo_`, `mail_`, `channel_`).
A good first call is `adsops_playbook` or `adsops_local_profile` with `action: "questions"`; the MCP prompt
`start` walks a small business through setup.

## 6. Optional: connect Google Ads / Local Services Ads

Only if the user wants ad-account access. The user creates a Google Cloud OAuth client of type **Desktop app**
(Google Ads API and Search Console API enabled) and downloads its JSON. Then:

```bash
adsops-mcp connect --config /absolute/path/to/AdRails/config.json --client-file /path/to/client.json
```

One browser sign-in; accounts are added read-only. Changes to a campaign always go
desired-state file → `adsops_plan` → `adsops_apply` (validate-only) → `adsops_apply commit=true`.
Full guide: https://adrails.dev/docs/ (service-account setup: `adsops-mcp init --help`).

## Rules the agent must keep

- Never spend money, enable a campaign or message a customer without the owner's explicit yes.
- `adsops_paid` extras are optional and priced up front; the tool never pays on its own.
- Troubleshooting: diagnostics go to stderr; `adsops-mcp doctor --config <config>` checks the setup.
