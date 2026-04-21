# Home Intelligence — Indigo plugin

A weekly house-wide digest + email-reply rule engine for Indigo.

## What it does

1. **Weekly digest.** Reads the SQL Logger database and Indigo event log, assembles the last 7 days of activity, asks a Claude model to produce a plain-English summary with statistical breakdowns (e.g. "hall lights 84% triggered vs 36% manual"), and emails it to you via a Cloudflare Worker relay.

2. **Feedback loop.** If the digest proposes an automation ("want me to turn the bedroom light off 30 min after 23:00 every night?") and you reply **YES** to the email, the plugin writes a rule to its own store and starts enforcing it.

3. **Plugin-internal rule engine.** Rules live in an Indigo variable as JSON. The plugin evaluates them against live device state every 60 seconds (configurable) and calls `indigo.dimmer.*` / `indigo.relay.*` directly. No native Indigo triggers are created — the Indigo API does not expose `trigger.create()`. This is the "Auto Lights" pattern.

## Status

Scaffold only — structure in place, Claude call and email delivery are stubbed. See TODO comments in `digest.py` and `delivery.py`.

## Install

First-time install must be **double-click** through Indigo's Plugin Manager, not `cp`. Subsequent updates can be copied into `Plugins/` and the plugin restarted.

## Configuration

Plugin prefs (via **Plugins → Home Intelligence → Configure…**):

- **SQL Logger database** — SQLite auto-detect or PostgreSQL connection details
- **Anthropic API key + model** — stored securely; Sonnet 4.6 recommended
- **Digest schedule** — day + local time, recipient email
- **Delivery Worker** — URL + HMAC shared secret (matches the Worker env var)
- **Rule store variable** — Indigo variable name for the rule JSON blob

## Menu items

- **Run Digest Now** — force a digest run
- **Show Agent Rules** — print all rules to the event log
- **Disable All Agent Rules** — panic switch; does not delete, just disables
- **Show Status** — rule count + model + next digest time
- **Toggle Debug Logging** — verbose log output

## Architecture

See [`CLAUDE.md`](./CLAUDE.md) for the full architecture and ADR references.

## Tests

A Tier-A pytest suite covers the pure helpers (JSON parsing, schema
validation, reply-id extraction, intent classification, subject tagging,
response parsers). Tests live in `tests/` at repo root and stub the
`indigo` module via `tests/conftest.py` so they can run outside an
Indigo server.

```bash
pip install pytest
python -m pytest tests/ -v
```

CI runs the suite on every PR via `.github/workflows/test.yml`.

## License

MIT — see [`LICENSE`](./LICENSE).
