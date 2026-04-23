# Home Intelligence — Indigo plugin

A weekly house-wide digest + email-reply rule engine for Indigo, plus
an MCP (Model Context Protocol) surface so Claude Desktop / Claude
Code can interrogate your house on demand under a subscription —
no API spend.

## What it does

1. **Weekly digest.** Reads the SQL Logger database and Indigo event log, assembles the last 7 days of activity, asks a Claude model to produce a plain-English summary with statistical breakdowns (e.g. "hall lights 84% triggered vs 36% manual"), and emails it to you via SMTP.

2. **Feedback loop.** If the digest proposes an automation ("want me to turn the bedroom light off 30 min after 23:00 every night?") and you reply **YES** to the email, the plugin writes a rule to its own store and starts enforcing it.

3. **Plugin-internal rule engine.** Rules live in an Indigo variable as JSON. The plugin evaluates them against live device state every 60 seconds (configurable) and calls `indigo.dimmer.*` / `indigo.relay.*` directly. No native Indigo triggers are created — the Indigo API does not expose `trigger.create()`. This is the "Auto Lights" pattern.

4. **Interactive MCP surface.** Claude Desktop / Claude Code connect to the plugin over MCP to interrogate the house mid-week — same data the weekly digest reasons over, no API spend if you have a Claude subscription. Read-only in v1 (rules, observations, SQL Logger history, curated house context snapshot). See [MCP surface](#mcp-surface) below.

## Install

First-time install must be **double-click** through Indigo's Plugin Manager, not `cp`. Subsequent updates can be copied into `Plugins/` and the plugin restarted.

## Configuration

Plugin prefs (via **Plugins → Home Intelligence → Configure…**):

- **SQL Logger database** — SQLite auto-detect or PostgreSQL connection details
- **Anthropic API key + model** — used only for the weekly digest; Sonnet 4.6 recommended. Leave blank if you plan to use the MCP surface from a Claude subscription and skip the scheduled email.
- **Digest schedule** — day + local time, recipient email
- **SMTP** — host, port, user, password, from address; SSL on by default
- **IMAP** — host, port, user, password, folder for YES / NO / SNOOZE reply ingestion
- **Rule store variable** — Indigo variable name for the rule JSON blob

## Menu items

- **Run Digest Now** — force a digest run
- **Show Agent Rules** — print all rules to the event log
- **Disable All Agent Rules** — panic switch; does not delete, just disables
- **Show Status** — rule count + model + next digest time
- **Toggle Debug Logging** — verbose log output

## MCP surface

The plugin hosts an MCP (Model Context Protocol) server at

```
POST /message/com.simons-plugins.home-intelligence/mcp
```

Claude Desktop and Claude Code connect here via the `mcp-remote` npm
bridge (until native HTTP MCP transport ships). Transport is
stateless JSON-RPC 2.0; authentication rides on Indigo's reflector
(remote) or implicit LAN trust (local).

### Tools (v1, read-only)

| Tool | What it does |
|---|---|
| `get_rules(include_disabled=false)` | List agent-owned automation rules with fires_count / last_fired_at / auto_disabled metadata. |
| `get_observations(status_filter="all", days_back=60)` | Prior digest observations + user responses. Lets Claude avoid re-proposing things you declined. |
| `query_sql_logger(device_id, column, time_range="24h")` | SQL Logger history for one device + column. Ranges: `1h`, `6h`, `24h`, `7d`, `30d`. |
| `house_context_snapshot(days=7)` | Curated whole-house bundle — devices, triggers, schedules, action groups, event log + summary, SQL rollups, fleet health, energy context, rules, observations. Expensive; use sparingly. |

### Resource

- `home-intelligence:digest_instructions` — the reasoning guide the weekly digest runs Claude under. Fetch this when asking for digest-style output mid-week.

### Claude Desktop config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` and
add the plugin alongside any other MCP servers you use (mlamoure's
`indigo-mcp-server` is the natural companion — it covers device
control / state queries / the general Indigo object model):

```json
{
  "mcpServers": {
    "home-intelligence": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://jarvis.local:8176/message/com.simons-plugins.home-intelligence/mcp"
      ]
    }
  }
}
```

Swap the URL for your Indigo Reflector address
(`https://<yours>.indigodomo.net/message/com.simons-plugins.home-intelligence/mcp`)
if you want to reach the plugin away from home — reflector auth
replaces LAN trust in that case.

Restart Claude Desktop after editing. On startup it will call the
`initialize` handshake and list the tools + resource above.

Example: "what rules has Home Intelligence flagged so far?" →
Claude calls `get_rules` and narrates.

## Architecture

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture, PRD-0002
for the MCP surface design, and `docs/adr/` for architectural
decisions (transport, tool surface scope, safety allowlist).

## Tests

A pytest suite covers the pure helpers (JSON parsing, schema
validation, reply-id extraction, intent classification, subject
tagging, response parsers, rule safety, MCP handler protocol and
tool dispatch). Tests live in `tests/` at repo root and stub the
`indigo` module via `tests/conftest.py` so they can run outside an
Indigo server.

```bash
pip install pytest
python -m pytest tests/ -v
```

CI runs the suite on every PR via `.github/workflows/test.yml`.

## License

MIT — see [`LICENSE`](./LICENSE).
