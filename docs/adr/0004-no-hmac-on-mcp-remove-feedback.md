---
parent: Decisions
nav_order: 4
title: "ADR-0004: No HMAC on the MCP endpoint; remove the vestigial /feedback path"
status: "accepted"
date: 2026-04-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0004: No HMAC on the MCP endpoint; remove the vestigial /feedback path

## Context and Problem Statement

The plugin contains an HMAC-SHA256 verification layer on its IWS
`/feedback` endpoint, introduced as the authentication boundary for
an originally-planned Cloudflare Email Worker path (Worker parses
inbound email → POST to plugin `/feedback` with HMAC). That plan was
superseded by workspace ADR-0002 ("Home Intelligence email feedback
loop via user SMTP+IMAP"), in which the plugin polls IMAP directly
and bypasses the Worker entirely.

Consequences today:

* The inbox poller calls `_dispatch_feedback(payload)` **in-process**
  with no HMAC verification — no external caller exists for
  `/feedback`.
* The `/feedback` IWS route + `handle_feedback` handler + HMAC
  verification + `internalHmacSecret` pluginPref exist but are never
  exercised.
* Adding an MCP endpoint raises the question: does MCP need its own
  HMAC? If it does, we keep (and extend) the pattern. If it doesn't,
  we can delete the whole HMAC footprint.

We must decide the authentication model for the new MCP endpoint —
and whether the legacy HMAC infrastructure is still earning its
weight.

## Decision Drivers

* MCP callers are Claude Desktop / Claude Code on the user's own
  machine, reaching the plugin via either LAN or the Indigo Reflector.
* Indigo Reflector already provides user authentication for remote
  access (that is its entire purpose — public URL → authenticated
  tunnel to the user's Indigo server).
* LAN access implies network-level trust (user's own home network, or
  user's own VPN).
* Configuration burden: every additional auth layer is another secret
  to generate, store, rotate, and distribute.
* Dead code is a liability — it implies features that don't exist
  and masks the actual security surface.
* ADR-0002 (workspace) already committed us to a pure SMTP+IMAP
  feedback loop; the Worker path will never be built.

## Considered Options

1. **Rely on reflector auth for remote, implicit LAN trust for
   local; remove the vestigial HMAC path** — MCP needs no additional
   authentication; delete `/feedback`, `handle_feedback`, HMAC
   verification, and `internalHmacSecret` pluginPref.
2. **Keep HMAC on `/feedback` as a future-proofing hook; extend HMAC
   to the MCP endpoint** — even if nothing uses `/feedback` today,
   leave the code and pattern; require Claude Desktop to send an HMAC
   on every MCP call.
3. **Keep the HMAC verification code but remove the `/feedback`
   route; add HMAC to MCP** — half-measure, same downsides as option
   2.
4. **Build a new auth scheme for MCP (bearer token, OAuth)** —
   separate from the HMAC pattern, designed specifically for
   interactive tool calls.

## Decision Outcome

Chosen option: **"Rely on reflector auth for remote, implicit LAN
trust for local; remove the vestigial HMAC path"**, because the
Indigo Reflector already IS the user-identity boundary for any remote
access, LAN is already a trust domain, and extending HMAC to MCP
would duplicate that existing boundary with a second secret offering
zero marginal protection — at the cost of configuration complexity
for every user who installs the plugin.

### Consequences

* Good, because users add ONE less config entry. The plugin's
  Configure dialog gets simpler.
* Good, because `delivery.py` loses ~60 lines of HMAC generation,
  verification, and header handling. `plugin.py` loses `handle_feedback`.
  `Actions.xml` loses its `feedback` entry. Security surface shrinks.
* Good, because the authentication story becomes explainable: "if
  you can reach the plugin's IWS, you are already authenticated by
  the LAN or by Indigo Reflector." One boundary, not two.
* Good, because future Worker-to-plugin paths (if ever needed) can
  introduce their own specific auth at that time. We're not
  pre-emptively keeping a pattern for a use case that was explicitly
  replaced.
* Bad, because if a user deliberately exposes IWS port 8176 to the
  internet WITHOUT enabling Indigo Reflector auth, the MCP endpoint
  is unauthenticated. This is a self-inflicted misconfiguration;
  documented in the plugin README as "do not do this."
* Bad, because we lose the ability to distinguish "call from plugin
  itself" vs "call from external client" at the `/feedback` layer —
  but this distinction was never load-bearing, since inbox poller
  uses `_dispatch_feedback` directly in-process.
* Bad, because if a future external caller (e.g. a mobile push
  handler) wants to POST to the plugin, we'll need to introduce auth
  for that specific path at that time. Acceptable — premature auth
  is a form of over-engineering.

### Confirmation

Considered implemented when:

1. `handle_feedback` method is removed from `plugin.py`.
2. `internalHmacSecret` pluginPref is removed from PluginConfig.xml
   and `_ensure_internal_hmac_secret` is deleted.
3. HMAC verification helper is removed from `delivery.py` (the
   HMAC signing helper for outbound `/email-out` stays IF we ever
   wire that up; for now, both are gone).
4. `Actions.xml` no longer contains the `feedback` entry.
5. The MCP endpoint `/mcp` is live on IWS with no additional auth
   headers required beyond what IWS itself enforces.
6. Plugin test suite passes with the HMAC code path removed.
7. Plugin restarts cleanly without warnings about missing secrets.

## Pros and Cons of the Options

### Remove HMAC entirely, rely on reflector + LAN (chosen)

* Good, because one fewer secret, one fewer config field.
* Good, because -60 LOC of dead code.
* Good, because the auth boundary is consistent with every OTHER
  Indigo plugin — reflector is THE user boundary.
* Bad, because misconfigured IWS port exposure becomes a user-
  visible footgun. Mitigated by documentation.

### Keep HMAC everywhere (status quo + extend to MCP)

* Good, because belt-and-braces security.
* Bad, because users must configure + distribute the secret to
  every MCP client (Claude Desktop header config, Claude Code header
  config, any future client).
* Bad, because keeps dead code.
* Bad, because layers a second secret on top of an already-
  authenticated transport — the reflector — giving no real
  additional security.

### Keep HMAC code, add to MCP, remove only the `/feedback` route

* Same downsides as above, minus one route delete.

### New bearer-token / OAuth scheme for MCP

* Good, because more aligned with modern API auth norms.
* Bad, because same net result as HMAC — a second secret layered
  on the reflector — with more implementation complexity.
* Bad, because Indigo Reflector has no built-in OAuth issuer; we'd
  invent one.
* Bad, because users would need to configure yet another secret.

## More Information

* Workspace ADR-0002 — the email feedback loop decision that made
  the Worker path obsolete.
* Indigo Reflector documentation:
  https://wiki.indigodomo.com/doku.php?id=indigo_2025.1_documentation:reflector
* PRD-0002 §7.3 — authentication section.
* Related: ADR-0002 (MCP transport), ADR-0005 (tool surface), ADR-0006
  (safety allowlist — the write-path protection that does NOT go
  away).

## For AI agents
- DO: Expose new IWS endpoints (including `/mcp`) without HMAC
  verification. Reflector / LAN is the boundary.
- DO: Document in the README that IWS must be reachable only via
  LAN or reflector, never publicly exposed without reflector auth.
- DO: Protect write actions via the safety allowlist (ADR-0006),
  not via transport-level auth.
- DON'T: Re-introduce HMAC for new endpoints without explicitly
  superseding this ADR.
- DON'T: Re-add `/feedback`, `handle_feedback`, or
  `internalHmacSecret` unless a new external-caller use case
  materialises and warrants a dedicated ADR.
