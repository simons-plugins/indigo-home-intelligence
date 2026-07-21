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

```text
POST /message/com.simons-plugins.home-intelligence/mcp
```

Claude Desktop and Claude Code connect here via the `mcp-remote` npm
bridge (until native HTTP MCP transport ships). Transport is
stateless JSON-RPC 2.0; authentication rides on Indigo's reflector
(remote) or implicit LAN trust (local).

### Read tools

| Tool | What it does |
|---|---|
| `get_rules(include_disabled=false)` | List agent-owned automation rules with fires_count / last_fired_at / auto_disabled metadata. |
| `get_observations(status_filter="all", days_back=60)` | Prior digest observations + user responses. Lets Claude avoid re-proposing things you declined. |
| `query_sql_logger(device_id, column, time_range="24h")` | SQL Logger history for one device + column. Ranges: `1h`, `6h`, `24h`, `7d`, `30d`. |
| `house_context_snapshot(days=7)` | Curated whole-house bundle — devices, triggers, schedules, action groups, event log + summary, SQL rollups, fleet health, energy context, rules, observations. Expensive; use sparingly. |

### Write tools

All three go through the same server-side safety allowlist
(`_is_safe_rule_target`) the email-YES flow uses per ADR-0006.
Thermostats, security systems, locks, cameras, and devices without
a power-switchable surface are refused — regardless of who's
asking.

| Tool | What it does |
|---|---|
| `propose_rule(rule)` | Validate schema + safety allowlist, no write. Returns a human-readable preview on success. Intended as "show the user what this rule would look like before committing." |
| `add_rule(rule, from_observation_id?)` | Persist the rule after propose_rule has validated it. Sends the same confirmation email as the Sunday-digest YES flow. If `from_observation_id` is set, updates the observation's `user_response`. |
| `update_rule(rule_id, action)` | Actions: `disable`, `enable`, `delete`. `enable` also clears auto-disabled metadata so the evaluator's failure counter resets. `delete` is permanent. |

### Resource

- `home-intelligence:digest_instructions` — the reasoning guide the weekly digest runs Claude under. Fetch this when asking for digest-style output mid-week.

### Setup: Claude Desktop with both MCPs

Home Intelligence is designed to work **alongside**
[`mlamoure/indigo-mcp-server`](https://github.com/mlamoure/indigo-mcp-server),
not replace it. mlamoure covers the general Indigo surface
(device control, state queries, action groups, event log,
semantic search). HI covers everything that requires plugin-owned
state: SQL Logger history, agent rules, observations, curated
digest context, digest-reasoning resource. The two surfaces are
deliberately non-overlapping, and Claude composes across both
when a question needs it.

The full setup has four steps. Skip step 1 if you only want HI
(you lose device control from chat but HI's tools still work).

#### 1. Install mlamoure's Indigo MCP Server plugin

1. Download `Indigo MCP Server.indigoPlugin.zip` from
   [github.com/mlamoure/indigo-mcp-server/releases](https://github.com/mlamoure/indigo-mcp-server/releases).
2. Double-click to install in Indigo.
3. In Plugin Configure, enter your **OpenAI API key** — mlamoure's
   plugin uses it for semantic search over devices/variables.
4. Add a new **MCP Server** device in Indigo (Devices → New →
   MCP Server → MCP Server). There can only be one per install.
5. Wait for the first vector-store sync to complete (check the
   Event Log — it announces "synchronized N entities in Xs").

#### 2. Install Home Intelligence (this plugin)

1. Download `Home Intelligence.indigoPlugin.zip` from this repo's
   releases, double-click to install.
2. Configure SQL Logger access + Anthropic API key (optional, for
   the scheduled email) + SMTP (optional, same).
3. No MCP-specific setup — the `/mcp` endpoint becomes live on
   plugin startup.

#### 3. Get a Reflector API key

Both plugins authenticate MCP requests at the Indigo Web Server
layer using a Bearer token. The easiest source is a Reflector
API key (works remotely too):

**Indigo → File → Preferences → Web Server → Reflector tab →
API Keys** → click "+" to add one. Label it `claude-desktop`.
Copy the value.

LAN-only alternative: Indigo's `secrets.json` file. See
`mlamoure/indigo-mcp-server`'s README §Authentication for that
path — the `Authorization: Bearer <token>` scheme is the same, but
the URL becomes `http://<host>:8176/...` with `--allow-http` in
the args.

#### 4. Edit Claude Desktop config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "indigo": {
      "command": "/opt/homebrew/bin/npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://<your-reflector>.indigodomo.net/message/com.vtmikel.mcp_server/mcp/",
        "--header",
        "Authorization:Bearer <your-reflector-api-key>"
      ],
      "env": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
      }
    },
    "home-intelligence": {
      "command": "/opt/homebrew/bin/npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://<your-reflector>.indigodomo.net/message/com.simons-plugins.home-intelligence/mcp",
        "--header",
        "Authorization:Bearer <your-reflector-api-key>"
      ],
      "env": {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

Replace in both entries:
- `<your-reflector>` — your Indigo Reflector hostname (e.g. `clarkcastle.indigodomo.net`).
- `<your-reflector-api-key>` — the key from step 3. Same key works for both plugins.

Gotchas:
- **Trailing slash mismatch is real.** mlamoure's endpoint is
  `/mcp/` (with slash); HI's is `/mcp` (no slash). Each matches its
  plugin's `Actions.xml` declaration; swapping returns 404.
- **`command` must point at the Homebrew-installed npx** and
  `env.PATH` must omit nvm's node directories. See the
  Troubleshooting section below for why.

Then `⌘Q` Claude Desktop and relaunch (close-window doesn't reload
MCP servers). Both servers should negotiate `initialize` on
startup, after which the tools pane (hammer icon bottom-left) will
show both `indigo:*` tools and `home-intelligence:*` tools.

#### First things to try

- *"Using Home Intelligence, what rules has the plugin flagged so far?"* →
  calls `home-intelligence:get_rules`.
- *"What happened with the dining TRV this week?"* →
  Claude will likely use `indigo:search_entities` to find it, then
  `home-intelligence:query_sql_logger` for its 7-day history.
- *"Run me a mid-week digest."* →
  reads `home-intelligence:digest_instructions` resource + calls
  `home-intelligence:house_context_snapshot`. Same reasoning
  substrate as Sunday's email, no API spend.
- *"Add a rule that turns the tumble dryer plug off if it's been
  on for 2 hours."* → Claude calls `propose_rule` first to show
  you the preview, then `add_rule` after you confirm. Safety
  allowlist blocks anything pointed at thermostats / security /
  locks regardless of what Claude proposes.
- *"Disable the noisy hall motion rule until next weekend."* →
  `update_rule` with `action: "disable"`.
- *"Turn off the study lamp."* → mlamoure's territory
  (`indigo:device_turn_off`); HI doesn't duplicate device control.

#### Troubleshooting

Claude Desktop writes per-server logs to
`~/Library/Logs/Claude/mcp-server-<name>.log` — always the first
place to look.

**`ReferenceError: ReadableStream is not defined`**
— Claude Desktop's launcher walks PATH for `node` and will happily
pick an nvm-installed Node 16, which doesn't expose `ReadableStream`
as a global. `mcp-remote`'s `undici` dependency then crashes on
import and the MCP process dies before any request is sent. *Both*
servers fail this way if nvm Node 16 is ahead of Homebrew Node on
PATH.

Fix: pin the `command` to `/opt/homebrew/bin/npx` **and** set `env.PATH`
so the shebang (`#!/usr/bin/env node`) finds Homebrew's Node first,
not nvm's. The config block above already does this; omit the nvm
directories deliberately.

**"Server disconnected" with nothing in the plugin log**
— The request is dying in mcp-remote before it hits the network.
Usually the Node-version issue above. The Indigo plugin event log
(`Indigo → Window → Event Log`, filter on plugin name) will be
silent; Claude Desktop's own log has the real stack trace.

**mlamoure's indigo shows "Stopped"**
— mlamoure's plugin only serves MCP when his `MCP Server` Indigo
device is in the `Running` state. Check the device in Indigo and
run through its Config UI to start it. This plugin (HI) has no
device dependency.

**401 / 403 from the reflector**
— Wrong token type. Reflector API keys and `secrets.json` local
secrets are different credential classes and are validated against
different code paths. Reflector URL wants the reflector API key;
LAN URL wants the local secret.

**Trailing slash 404**
— Gotcha between the two plugins: mlamoure's endpoint is
`/mcp/` with a trailing slash, HI's is `/mcp` without. Swapping
either way returns 404 from IWS.

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
