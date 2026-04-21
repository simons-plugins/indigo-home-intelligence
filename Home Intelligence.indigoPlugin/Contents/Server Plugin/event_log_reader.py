"""Event log reader — gives the digest a view of what actually happened
this week, beyond the house's static configuration.

Indigo's event log is the authoritative narrative layer: every trigger
/ schedule / action-group fire is narrated there alongside the device
state changes that resulted. Joining those over a 7-day window lets
Claude reason about real behaviour ("Study Lamp was turned off 5x this
week by Auto Lights") rather than having to guess from action-group
names what an automation actually does.

Two data sources, merged:

- Today's live log via ``indigo.server.getEventLogList`` — returns a
  list of dict-like entries with ``TimeStamp`` / ``TypeStr`` /
  ``Message`` fields.
- Prior days via parsing ``{install}/Logs/{YYYY-MM-DD} Events.txt``.
  Reference implementation lives in the ``LogsOverReflector`` plugin
  (reflector-logs repo); we reuse its regex and file path pattern.

Filtering: keep entries that narrate automation activity (triggers,
schedules, action groups, Auto Lights zones, device state-change
narrations from individual plugins). Drop system chatter (MCP server
synchronisation, vector-store embedding progress, plugin start/stop
housekeeping, log-query echo).

Known blind spot: triggers with "Write to Event Log" disabled do not
appear in the event log. Claude's inference for those device changes
will say "manual or silenced trigger" rather than attributing them
to a specific automation. The digest INSTRUCTIONS explicitly call
this out so the model hedges.
"""

import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import indigo


# Matches the Indigo event log file format:
# "YYYY-MM-DD HH:MM:SS.fff\t<TypeStr>\t<Message>"
_LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\t+(\S.*?)\t+(.*)$"
)

# ``TypeStr`` values we always keep — automations firing.
_USEFUL_TYPE_STR = frozenset(
    {
        "Trigger",
        "Schedule",
        "Action Group",
        "Auto Lights",  # the Auto Lights plugin's zone narration
    }
)

# ``TypeStr`` prefixes that are pure system noise.
_NOISE_TYPE_PREFIXES = (
    "MCP Server",
)

# Message-level noise patterns — fire regardless of source type.
_NOISE_MESSAGE_PATTERNS = [
    re.compile(r"Vector store", re.IGNORECASE),
    re.compile(r"tools:call \|", re.IGNORECASE),
    re.compile(
        r"Refreshing embeddings|Generating embeddings|Embedding Generation",
        re.IGNORECASE,
    ),
    re.compile(r"Keyword Generation progress", re.IGNORECASE),
    re.compile(r"[Dd]evices[_ ]Embeddings"),
    re.compile(
        r"^(Starting|Stopping|Started|Stopped|Reloading|Loading) plugin \""
    ),
    re.compile(r"^Processing requirements for plugin"),
    re.compile(r"^Requirements for .* (?:previously )?processed"),
    re.compile(r"log_query.*query completed"),
    re.compile(r"search_entities|list_(?:action_groups|handlers)|action_control"),
    re.compile(r"StopPoll max exceeded"),
    re.compile(r"Logging to (?:Indigo|Plugin) Event Log at"),
]

# Hard cap on events returned. A 7-day window on an active house can
# produce thousands of narrated events; trimming keeps the digest
# prompt bounded. We keep the NEWEST events — patterns from the most
# recent day or two are what the digest is most likely to flag.
_DEFAULT_MAX_EVENTS = 3000

# Live log entries to fetch. Indigo retains ~5000 lines by default in
# the live buffer; we ask for 20k (the API returns whatever's available
# up to that cap) to make sure we cover the whole live window before
# the buffer rolls over into file storage.
_LIVE_FETCH_CAP = 20000


class EventLogReader:
    def __init__(self, logger, install_folder: Optional[str] = None):
        self.logger = logger
        # Resolved lazily on first read_window so unit tests can inject.
        self._install_folder = install_folder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_window(
        self, days_back: int = 7, max_events: int = _DEFAULT_MAX_EVENTS
    ) -> List[dict]:
        """Return chronologically-sorted narrated events from
        ``(now - days_back)`` to now. Dedupes across the live + file
        boundary (today's entries may appear in both if the day has
        already been archived) and caps at ``max_events`` keeping the
        newest."""
        events = self._read_live(days_back) + self._read_historical(days_back)
        filtered = self._filter_and_dedup(events)
        filtered.sort(key=lambda e: e["timestamp"])
        if len(filtered) > max_events:
            filtered = filtered[-max_events:]
        return filtered

    def summarise(self, events: List[dict]) -> dict:
        """Produce compact aggregates over an already-filtered event list
        so Claude gets both the chronology and the big-picture shape
        without having to derive them itself."""
        counts_by_source = Counter(e.get("source", "") for e in events)
        counts_by_hour: Counter = Counter()
        for e in events:
            ts = e.get("timestamp", "")
            # Timestamps look like "2026-04-21 15:30:00.000" or
            # "2026-04-21T15:30:00+00:00"; either way bytes 11-12 are
            # the hour digits.
            if len(ts) >= 13:
                counts_by_hour[ts[11:13]] += 1
        return {
            "total_events": len(events),
            "top_sources": dict(counts_by_source.most_common(20)),
            "events_by_hour": dict(sorted(counts_by_hour.items())),
        }

    # ------------------------------------------------------------------
    # Live log
    # ------------------------------------------------------------------

    def _read_live(self, days_back: int) -> List[dict]:
        try:
            raw = indigo.server.getEventLogList(
                returnAsList=True, showTimeStamp=True, lineCount=_LIVE_FETCH_CAP
            )
        except Exception as exc:
            self.logger.exception(f"Failed to read live event log: {exc}")
            return []

        cutoff = datetime.now() - timedelta(days=days_back)
        events: List[dict] = []
        for entry in raw:
            d = dict(entry)
            canonical = self._canonical_ts(d.get("TimeStamp"))
            if canonical is None:
                continue
            display, dt_local = canonical
            if dt_local < cutoff:
                continue
            events.append(
                {
                    "timestamp": display,
                    "source": d.get("TypeStr", ""),
                    "message": d.get("Message", ""),
                }
            )
        return events

    @staticmethod
    def _canonical_ts(ts) -> Optional[Tuple[str, datetime]]:
        """Normalise any incoming timestamp into a (display_string,
        naive_local_datetime) pair. Display string format matches the
        file-log native form (``YYYY-MM-DD HH:MM:SS.fff``) regardless
        of source, so the same event appearing in both the live log
        (tz-aware datetime) and an archived file collapses to one
        dedup key. Returns None for any unparseable input."""
        if ts is None:
            return None
        if hasattr(ts, "astimezone"):
            try:
                local = ts.astimezone() if ts.tzinfo else ts
                naive = local.replace(tzinfo=None)
            except Exception:
                return None
        else:
            s = str(ts).strip()
            if not s:
                return None
            try:
                naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                try:
                    dt = datetime.fromisoformat(s)
                except ValueError:
                    return None
                local = dt.astimezone() if dt.tzinfo else dt
                naive = local.replace(tzinfo=None)
        ms = naive.microsecond // 1000
        display = f"{naive.strftime('%Y-%m-%d %H:%M:%S')}.{ms:03d}"
        return display, naive

    # ------------------------------------------------------------------
    # Historical files
    # ------------------------------------------------------------------

    def _read_historical(self, days_back: int) -> List[dict]:
        install = self._get_install_folder()
        if install is None:
            return []
        logs_dir = os.path.join(install, "Logs")
        if not os.path.isdir(logs_dir):
            self.logger.warning(f"Event log directory not found: {logs_dir}")
            return []

        now_local = datetime.now()
        cutoff = now_local - timedelta(days=days_back)
        today = now_local.date()
        events: List[dict] = []
        # range(1, days_back + 1) = yesterday, day-before, … — today is
        # already covered by the live log. Apply a rolling cutoff per
        # event so the oldest file isn't included whole when days_back
        # crosses mid-day (e.g. running at 14:00 with days_back=7 should
        # stop 7 days ago at 14:00, not at midnight).
        for i in range(1, days_back + 1):
            date = today - timedelta(days=i)
            filename = os.path.join(
                logs_dir, f"{date.strftime('%Y-%m-%d')} Events.txt"
            )
            if not os.path.isfile(filename):
                continue
            for e in self._parse_file(filename):
                canonical = self._canonical_ts(e.get("timestamp"))
                if canonical is None:
                    continue
                display, dt_local = canonical
                if dt_local < cutoff:
                    continue
                e["timestamp"] = display
                events.append(e)
        return events

    def _get_install_folder(self) -> Optional[str]:
        if self._install_folder is not None:
            return self._install_folder
        try:
            self._install_folder = indigo.server.getInstallFolderPath()
        except Exception as exc:
            self.logger.exception(f"Cannot resolve Indigo install path: {exc}")
            return None
        return self._install_folder

    @classmethod
    def _parse_file(cls, path: str) -> List[dict]:
        """Parse a single dated events file. Continuation lines (no
        leading timestamp) append to the previous entry — Indigo
        frequently logs multi-line errors and we want them kept
        together."""
        try:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except UnicodeDecodeError:
                with open(path, "r", encoding="latin-1") as f:
                    lines = f.readlines()
        except OSError:
            return []

        events: List[dict] = []
        current: Optional[dict] = None
        for raw_line in lines:
            line = raw_line.rstrip("\r\n")
            m = _LOG_LINE_RE.match(line)
            if m:
                if current is not None:
                    events.append(current)
                current = {
                    "timestamp": m.group(1),
                    "source": m.group(2),
                    "message": m.group(3),
                }
            elif current is not None:
                current["message"] += "\n" + line
        if current is not None:
            events.append(current)
        return events

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @classmethod
    def _filter_and_dedup(cls, events: List[dict]) -> List[dict]:
        seen = set()
        out: List[dict] = []
        for e in events:
            src = e.get("source", "") or ""
            msg = e.get("message", "") or ""
            if cls._is_noise(src, msg):
                continue
            if not cls._is_useful(src, msg):
                continue
            # Dedup key: (ts, source, first-100-chars). Same event in
            # both live and file form will collapse to one entry.
            key = (e.get("timestamp"), src, msg[:100])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    @classmethod
    def _is_noise(cls, src: str, msg: str) -> bool:
        for prefix in _NOISE_TYPE_PREFIXES:
            if src.startswith(prefix):
                return True
        for pat in _NOISE_MESSAGE_PATTERNS:
            if pat.search(msg):
                return True
        return False

    @classmethod
    def _is_useful(cls, src: str, msg: str) -> bool:
        # Explicit automation narrations always win.
        if src in _USEFUL_TYPE_STR:
            return True
        # Device state-change narrations from plugin sources (Z-Wave,
        # ShellyNGMQTT, Hue, online sensors etc.). Heuristic: the
        # message mentions a device in quotes (double OR single — the
        # online-sensor plugins use single quotes: "'Online Dining
        # TRV' on") and the verb describes a state change.
        if '"' not in msg and "'" not in msg:
            return False
        narrated = (
            'sent "' in msg
            or "sent '" in msg
            or 'received "' in msg
            or "received '" in msg
            or "set to" in msg
            or msg.endswith(" on")
            or msg.endswith(" off")
            or "turned on" in msg
            or "turned off" in msg
        )
        return narrated
