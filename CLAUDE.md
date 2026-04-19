# CLAUDE.md — Home Intelligence Indigo Plugin

> **Part of the [Indigo workspace](../CLAUDE.md)** — see root for cross-project map, standards, and tooling.

## Project Identity

- **Name**: Home Intelligence
- **Type**: Indigo plugin
- **Shortcut**: `home intel` / `home intelligence`
- **GitHub**: https://github.com/simons-plugins/indigo-home-intelligence *(to be created)*
- **Plugin ID**: `com.simons-plugins.home-intelligence`
- **Language**: Python 3.10+

## Role in the workspace

Weekly whole-house digest + email-feedback rule engine. Reads existing Indigo history data (SQL Logger + event log), produces a Claude-written summary, and enforces agent-proposed rules that the user accepts via email reply.

```
SQL Logger + event log
   │
   ▼
Home Intelligence plugin ──▶ domio-push-relay (Worker) ──▶ email out
                              ▲
                              │
                           email in (CF Email Workers)
                              │
                              ▼
                       plugin /feedback ──▶ rule store
                                             │
                                             ▼
                                      runConcurrentThread
                                      enforces rules
```

## Related projects

- [`../indigo-domio-plugin/`](../indigo-domio-plugin/) — source of the `history_db.py` module (currently copied; refactor to shared lib is future work). The Domio plugin stays focused on iOS push delivery.
- [`../domio-push-relay/`](../domio-push-relay/) — shared Cloudflare Worker. Home Intelligence adds two new routes (`/email-out`, `/email-in`) and reuses the HMAC secret under a distinct env var (`HOME_INTELLIGENCE_HMAC_SECRET`).

## Why a separate plugin?

The Domio plugin is for iOS push notifications. Home Intelligence does whole-house reasoning, scheduled digests, and runs its own rule evaluator loop — a different lifecycle and a different failure domain. Keeping them separate means Domio stays small and obviously correct, and Home Intelligence can grow without bloating the push path.

## Standards

Inherits workspace standards from [root CLAUDE.md](../CLAUDE.md#common-standards-apply-to-every-project-unless-its-claudemd-overrides). Key points for this project:

- **Version bump per PR**: `Info.plist` `PluginVersion`. Format `YYYY.R.P`; started at `2026.0.1`.
- **Testing**: none yet. Real testing happens on `jarvis.local` once the first digest runs against real data. Add `pytest` suite when the rule evaluator gets complex enough to need one.
- **Merge**: GitHub PR only, never `--admin`, never squash, wait for CI green, wait for user go-ahead.
- **Python**: 3.10+.

## Architecture Decision Records

- **Local ADRs:** `docs/adr/` — not yet populated. First ADR candidate: the choice of plugin-internal rule engine (Pattern 1) over native Indigo trigger creation.
- **Workspace ADRs:** `~/vsCodeProjects/Indigo/docs/adr/` — cross-repo decisions (push contract, HMAC scheme, shared auth).
- **Format:** MADR 4.0.0.

### Rules
- Before introducing a new library or architectural pattern, read `docs/adr/INDEX.md` and grep for relevant ADRs.
- If a new cross-cutting decision is made (e.g. new Worker route contract), propose a workspace ADR.

---

## Plugin layout

```
Home Intelligence.indigoPlugin/
└── Contents/
    ├── Info.plist                      # metadata, PluginVersion
    └── Server Plugin/
        ├── plugin.py                   # lifecycle, runConcurrentThread, menu, HTTP
        ├── PluginConfig.xml            # prefs UI
        ├── MenuItems.xml               # menu items
        ├── Actions.xml                 # hidden IWS HTTP endpoints (/feedback, /status)
        ├── history_db.py               # SQL Logger reader (copied from Domio plugin)
        ├── rule_store.py               # JSON-in-Indigo-variable rule persistence
        ├── rule_evaluator.py           # fixed-schema rule matcher; called from runConcurrentThread
        ├── digest.py                   # weekly Claude call + prompt assembly (stubbed)
        └── delivery.py                 # HMAC-signed POST to relay; inbound signature verify
```

## Key design decisions (not yet in ADRs)

### 1. Plugin-internal rule engine, not native Indigo triggers

The Indigo Object Model does not expose `indigo.trigger.create()`. Only `enable`, `execute`, `delete`, `moveToFolder` are available. So the plugin maintains its own rule store and evaluator rather than writing Indigo triggers.

**Pattern:** Auto Lights style — rules are data the plugin owns, `runConcurrentThread` evaluates them every ~60s, and the plugin calls `indigo.dimmer.turnOff(...)` / `indigo.relay.turnOff(...)` directly.

**Consequence:** agent rules do not appear in Indigo's Triggers panel. To keep them visible the rule store lives in an Indigo variable (`home_intelligence_rules` by default) whose JSON value the user can inspect and edit in the Indigo UI.

### 2. Fixed JSON schema for rules — no DSL, no eval()

Rules are pure data with a fixed shape (device_id, state, equals, optional after/before/for_minutes, action op). The agent fills in predefined fields. No Python `eval()`, no custom grammar, no LLM-generated code paths. See `rule_store.py` docstring for the schema.

### 3. History data is read, not re-recorded

The full `PRD-provenance.md` in `../indigo-domio-plugin/.planning/AI-agent/` was scoped out. Claude reasons directly over SQL Logger rows + Indigo event log narration — no separate provenance table, no attribution engine, no SelfTagger. The PRDs remain on disk as reference material for a possible year-2 expansion.

### 4. Shares the domio-push-relay Worker, not a new one

Adds `/email-out` and `/email-in` routes to the existing `domio-push-relay`. Same HMAC pattern as push, distinct env var (`HOME_INTELLIGENCE_HMAC_SECRET`). One Worker, two feature areas.

## HTTP endpoints (IWS)

Both are `uiPath="hidden"` in `Actions.xml`, reachable at:

```
POST https://<indigo-reflector>/message/com.simons-plugins.home-intelligence/feedback
GET  https://<indigo-reflector>/message/com.simons-plugins.home-intelligence/status
```

`/feedback` verifies an `X-HI-Signature` HMAC header before acting. `/status` is unauthenticated health-check only.

## Testing on jarvis.local

First install requires double-click via Indigo UI. Subsequent updates:

```bash
cp -r "Home Intelligence.indigoPlugin" \
  "/Volumes/Macintosh HD-1/Library/Application Support/Perceptive Automation/Indigo 2025.1/Plugins/"
```

Then restart the plugin via MCP (`mcp__indigo__restart_plugin`) or the Indigo UI.

## Open TODOs

- `digest.py` — prompt assembly, Claude SDK call with prompt caching, output parsing
- `delivery.py` — integration test against the Worker once `/email-out` is live
- `rule_evaluator.py` — extend schema with more ops (scene, action_group trigger)
- Worker — new `/email-out` and `/email-in` routes in `domio-push-relay`
- CF Email Workers — inbound routing configuration
- Cost observability — per-digest token counts and running monthly spend
