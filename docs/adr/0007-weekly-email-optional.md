---
parent: Decisions
nav_order: 7
title: "ADR-0007: Make the weekly digest email optional, default off for new installs after v2"
status: "accepted"
date: 2026-04-23
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0007: Make the weekly digest email optional, default off for new installs after v2

## Context and Problem Statement

The plugin shipped with a single output path: a Sunday-evening digest
email generated via direct Anthropic API call, costing ~£0.39 per
run. Once the interactive MCP surface lands (ADR-0002), users with a
Claude subscription gain an alternative: ask Claude Desktop mid-week
for the same reasoning, free of API spend.

This creates a choice for every user:

* **Subscription user who prefers chat** — uses Claude Desktop for
  mid-week interrogation; the scheduled email is redundant.
* **Subscription user who likes the passive Sunday email** — uses
  both.
* **API-only user (no subscription)** — uses the email as their only
  channel.

Keeping the email unconditionally enabled wastes ~£20/year for
subscription users who would rather chat. Removing it breaks the
plugin for API-only users.

The email must become a toggleable feature, with sensible defaults
that don't break existing installs.

## Decision Drivers

* Backward compatibility: existing users who have the plugin running
  with email expect it to keep working across updates.
* No surprise spend: a new user installing the plugin and configuring
  only Claude Desktop MCP should NOT be silently spending £20/year
  on a digest email they aren't reading.
* Simple mental model: "this pluginPref controls that behaviour"
  rather than complex conditionals.
* Decoupled from channel choice: the pref is about "do I want a
  scheduled email" not about "do I have a subscription" — many
  subscription users will still want the email.

## Considered Options

1. **New pluginPref `enableWeeklyEmail` (default: true for backward
   compat; documented as settable to false); email schedule skipped
   when false.**
2. **Same pref but default false for new installs only** — detect
   fresh install (no existing rule / observation data) and default
   differently.
3. **Remove the email schedule entirely; require users to either
   trigger digests via Indigo action or via MCP.**
4. **Keep email mandatory for v1; add the toggle only when MCP is
   a well-proven alternative.**

## Decision Outcome

Chosen option: **"New pluginPref `enableWeeklyEmail` (default: true
for backward compat; new-install documentation recommends setting
false if the user has a Claude subscription)"**, because it preserves
the existing experience by default, gives users control, and avoids
detecting-new-vs-upgrade logic that is fragile and surprising.

The default will flip to `false` in a future version (target: v2) once
the MCP path is proven and the documentation clearly steers new
users toward the subscription-driven flow.

### Consequences

* Good, because existing deployments continue sending the Sunday
  email with no user action required after upgrade.
* Good, because subscription users who want to stop paying for the
  email can set one toggle in Plugin Configure.
* Good, because API-only users are unaffected.
* Good, because the codepath for running the digest unconditionally
  becomes guarded by one prefs check. Minimal diff.
* Good, because plugin Configure UI can present "Weekly digest
  email" with a clear help text: "Required unless you use Claude
  Desktop / Code with the MCP surface to chat with your house."
* Bad, because there is temporary redundancy: subscription users
  who don't flip the toggle pay for an email they may stop reading
  in favour of chat. Acceptable — this is a user choice, documented.
* Bad, because detecting "subscription available" and offering
  smart defaults is impossible without asking; we accept the
  explicit-toggle UX.
* Bad, because v2 flipping the default to false for new installs is
  a subtle behaviour change; must be called out clearly in the v2
  release notes.

### Confirmation

Considered implemented when:

1. `enableWeeklyEmail` pluginPref exists in `PluginConfig.xml` with
   default `true` and a clear description mentioning the MCP
   alternative.
2. `plugin._tick_digest_clock` checks the pref before firing the
   scheduled digest.
3. The plugin's README documents the flag and its relationship to
   the MCP surface.
4. The plugin handles the transitional case where a user has
   `enableWeeklyEmail=false` AND no SMTP config — the startup log
   notes "weekly email disabled" as INFO, not WARNING.
5. A deferred followup task is recorded (in TODOS.md): flip default
   to `false` for fresh installs in v2, after MCP surface is proven
   stable for 3+ months.

## Pros and Cons of the Options

### New pref, default true (chosen)

* Good, because backward compatibility.
* Good, because user has explicit control.
* Good, because simple to document.
* Bad, because subscription users who want "no email" must discover
  and flip the toggle.

### New pref, default false for new installs

* Good, because new installs are never surprised by API spend.
* Bad, because "new install detection" is fragile — relies on
  absent rules / observations, which a user could clear manually
  and trigger a surprise default flip.
* Bad, because leads to inconsistent behaviour across installs
  depending on cleanup history.

### Remove email entirely

* Good, because forces all users into the new path.
* Bad, because API-only users have no working alternative.
* Bad, because breaks every existing deployment the day the update
  ships.

### Keep email mandatory until MCP proven

* Good, because conservative.
* Bad, because delays the option unnecessarily — the toggle costs
  almost nothing to ship.
* Bad, because doesn't give users who prefer chat-only a way to
  opt out without disabling the whole plugin.

## More Information

* PRD-0002 §5.2 (user story U5 and U7), §11 Phase 4.
* Related: ADR-0002 (MCP transport makes the optional-email viable).

## For AI agents
- DO: Check `enableWeeklyEmail` before running `DigestRunner.run()`
  from the plugin's scheduled tick. No check needed in the
  MCP-triggered digest (if that tool ever exists — that's an
  on-demand path).
- DO: Log "weekly email disabled by pref" at INFO level on startup
  so operators can see the state without reading prefs.
- DO: Treat the pref as load-bearing for user spend — do not
  bypass it silently in any code path.
- DON'T: Add logic that re-enables the email based on other state
  (e.g. "enable if no MCP clients connected in N days") — that
  violates the "one explicit pref controls the behaviour" principle.
- DON'T: Assume the default is the same across all install
  generations. Check the pref explicitly.
