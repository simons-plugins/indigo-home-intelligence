# TODOS — Home Intelligence

Backlog surfaced by the 2026-04-22 `/plan-ceo-review` (HOLD SCOPE) pass.
Each item is a deferred improvement — not blocking for the shipped
plugin but worth doing as operational pain surfaces or capacity allows.

Format: `## Title` — one-line hook, then **What / Why / Pros / Cons /
Effort / Priority / Depends-on**. Kept concise so picking one up in
three months is a 30-second decision.

---

## 1. Extend the `mlamoure/indigo-mcp-server` with rule CRUD tools

**What:** Add MCP tools to the Indigo MCP server for `list_rules`,
`get_rule`, `enable_rule`, `disable_rule`, `delete_rule`, `add_rule`
(with fixed-schema validation matching `rule_store.py`).

**Why:** Rule management via Claude Code chat is free
(covered by user's Claude subscription) whereas email replies cost
API tokens. Chat is richer than YES/NO/SNOOZE — user can iterate on
rule shape naturally.

**Pros:** Leverages existing MCP server; rule store is just JSON in
an Indigo variable (MCP already reads variables); natural-language
interface at zero marginal cost.

**Cons:** Cross-repo coordination (this plugin owns schema, MCP
server owns tool surface); schema drift risk if either evolves
without the other.

**Effort:** M · **Priority:** P2 · **Depends on:** Acceptance of
rule schema as stable (it is after ADR-0001).

---

## 2. Split `digest.py` into modules

**What:** Extract prompt-building, snapshot helpers, output
validation, and orchestration into separate modules under a
`digest/` package.

**Why:** Current file is 1,219 lines with ~25 methods on
`DigestRunner`. Growing a second digest-like feature (e.g. monthly
summary, anomaly alert) will worsen this.

**Pros:** Each module independently testable; new features add
files not methods.

**Cons:** Import gymnastics; no new user-visible value; risk of
breaking existing tests during refactor.

**Effort:** M · **Priority:** P3 · **Depends on:** A second
digest-shaped feature justifying it.

---

## 3. Persist `_last_digest_date` across plugin restarts

**What:** Store last-fired date in `pluginPrefs` (or an Indigo
variable) so a plugin restart on Sunday 18:01 doesn't fire the
weekly digest a second time.

**Why:** Current state lives in memory. Plugin restart loses it.
A restart in the 5-minute window after the weekly fire would cause
a duplicate send — real £0.40 of wasted Anthropic cost + user
confusion.

**Pros:** Prevents duplicate digests on Sunday evenings with
restarts; cheap to fix (~10 lines).

**Cons:** Need to be careful about pluginPrefs race with
`closedPrefsConfigUi`.

**Effort:** S · **Priority:** P2 · **Depends on:** Nothing.

---

## 4. Consolidate `history_db.py` with Domio plugin's copy

**What:** Extract to shared library or vendored submodule; both
plugins import from one source.

**Why:** Currently duplicated verbatim. This session found three
PG-specific bugs (case folding, psql trailing NULL, IS NOT NULL
filter) — any fixes need to land in both copies.

**Pros:** Single source of truth; bugs fixed once apply to both.

**Cons:** Indigo plugin packaging is awkward for shared libs
(bundled `Packages/` is per-plugin). Options: git subtree, Python
package on PyPI, manual periodic sync.

**Effort:** M · **Priority:** P3 · **Depends on:** Nothing.

---

## 5. Cap or chunk the UNION ALL energy rollup query at ~500 devices

**What:** When discovery returns >500 device IDs, split into
batches and merge results in Python.

**Why:** Current UNION ALL builds one SQL string growing linearly
with device count. PG's query text limit is well above 1MB so
current 70-device deployment is fine, but unbounded growth is a
smell.

**Pros:** Handles future mega-houses; predictable query time.

**Cons:** N+1-ish query pattern per batch; no real problem until
device count grows ~10x.

**Effort:** S · **Priority:** P3 · **Depends on:** Need appearing.

---

## 6. Prune expired observations from the store

**What:** In `observation_store.add`, drop entries older than
`DEFAULT_EXPIRY_DAYS` (60) before writing new ones.

**Why:** No pruning today — expired observations stay in the
Indigo variable forever. Variable has ~512KB limit, each obs ~400
bytes → ~1,300 max → ~25 years at weekly cadence. Bounded in
practice but unbounded by design.

**Pros:** Guaranteed bounded growth; variable stays small.

**Cons:** Tiny; just 5-line change.

**Effort:** S · **Priority:** P3 · **Depends on:** Nothing.

---

## 7. PG integration test harness

**What:** Docker Compose with PostgreSQL + SQL Logger schema
reproduction; regression tests for:
- Case-folding on unquoted column names
- psql `--unaligned` trimming trailing NULL fields
- `WHERE accumEnergyTotal IS NOT NULL` gating on latest-row query

**Why:** Every PG-specific bug this session escaped unit tests
because the test fixtures use SQLite. Real-world PG behaviour
diverges silently.

**Pros:** Catches regressions; documents the weird PG corners via
executable tests; makes future PG-backend work safer.

**Cons:** CI complexity (Docker in GH Actions); slower test suite;
need to maintain schema fidelity.

**Effort:** M · **Priority:** P2 · **Depends on:** Nothing.

---

## 8. YES→NO rollback

**What:** If user replies NO to an observation they previously
replied YES to, disable the associated rule and send a
confirmation.

**Why:** Current state: YES writes a rule; subsequent NO
overwrites the observation's user_response but leaves the rule
active. User has to use the menu items to actually disable.

**Pros:** Matches user intent naturally; catches accidental YES.

**Cons:** Minor UX path; menu items cover the same need.

**Effort:** S · **Priority:** P3 · **Depends on:** Confirmation
email infrastructure (landed in 2026.0.20).

---

## 9. Auto-Lights overlap review

**What:** When rule count grows or Auto-Lights-style rules are
proposed, evaluate whether a unified rule engine makes sense.

**Why:** Two rule engines (Auto Lights for presence-based zones;
Home Intelligence for state/time). Currently non-overlapping in
shape but cognitive overhead grows.

**Pros:** Consolidates mental model.

**Cons:** Premature today; neither plugin's schema dominates;
refactoring both is costly.

**Effort:** XL · **Priority:** P3 · **Depends on:** Pain
threshold — perhaps when rule count exceeds 10.

---

## 10. Declined-observation learning loop

**What:** When user replies NO to observations, store the pattern
(device types, time windows, rule shapes) and feed as negative
signal to the digest prompt.

**Why:** Current dedup is by observation ID only — the LLM can
re-propose structurally identical rules against different devices.

**Pros:** Digest quality improves with use; feels genuinely
learning.

**Cons:** Prompt grows with rejection history; risk of the LLM
becoming overly cautious.

**Effort:** M · **Priority:** P3 · **Depends on:** Accumulated
NO replies (needs months of use first).

---

## 11. Anomaly push mid-week via domio-push-relay

**What:** When fleet health or energy detect a critical anomaly
(battery at 2%, TRV offline for 48h+, daily consumption spike
>50%), fire a push notification via the existing Worker rather
than waiting for Sunday's digest.

**Why:** Some signals shouldn't wait a week. Battery at 2% will
die before the next digest.

**Pros:** Proactive channel for urgent issues; reuses existing
push infrastructure.

**Cons:** Needs a separate "urgency" classifier; risk of
notification fatigue; users might turn off push if the threshold
is too aggressive.

**Effort:** M · **Priority:** P2 · **Depends on:** Urgency
threshold definition; integration with domio-push-relay.

---

## 12. Cross-week trend detection

**What:** LLM sees multiple weeks of event-log summaries, spots
week-over-week patterns and multi-week anomalies.

**Why:** Current prompt is single-week. A device that's slowly
degrading over 3 weeks isn't visible until week 4 when it fails.

**Pros:** Catches slow-drift failures; feels more intelligent.

**Cons:** Prompt size grows linearly; token cost scales; needs
persistent state beyond current observation store.

**Effort:** L · **Priority:** P3 · **Depends on:** Months of
accumulated data; evaluation of whether weekly alone is enough.
