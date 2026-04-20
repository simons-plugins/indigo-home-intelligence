"""
Thin Anthropic Messages API client over urllib (stdlib only).

Why raw HTTP instead of the `anthropic` SDK: Indigo plugins bundle their
Python dependencies in `Contents/Packages/`, and the official SDK drags
in httpx, pydantic, pydantic-core, anyio, sniffio, certifi and a dozen
other transitive packages (~tens of MB, many with C extensions that
must match the server's architecture). Indigo's requirements.txt auto-
install has a known silent-skip failure mode, so the reliable deployment
paths are "bundle the whole tree" or "stdlib only". Stdlib wins.

The model endpoint is versioned via the `anthropic-version` header
(currently 2023-06-01). Prompt caching is supported by placing
`cache_control: {"type": "ephemeral"}` on the last stable system block;
callers should keep volatile content (timestamps, per-run context) out
of the cached prefix.
"""

import json
import urllib.error
import urllib.request
from typing import List, Optional


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, body: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.body = body


class AnthropicClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        logger,
        max_tokens: int = 4096,
        timeout_sec: int = 120,
    ):
        self.api_key = api_key
        self.model = model
        self.logger = logger
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec

    def configured(self) -> bool:
        return bool(self.api_key)

    def create_message(
        self,
        system_blocks: List[dict],
        user_message: str,
        max_tokens: Optional[int] = None,
        output_schema: Optional[dict] = None,
    ) -> dict:
        """
        Call /v1/messages. Returns the parsed response dict.

        `system_blocks` is a list of {"type": "text", "text": "...",
        "cache_control": {"type": "ephemeral"}?} blocks. Put the stable
        prefix first with cache_control on the last stable block; put
        volatile content in `user_message`.

        If `output_schema` is provided, the response's first text block
        is guaranteed to contain JSON matching the schema.
        """
        if not self.configured():
            raise AnthropicError("Anthropic API key is not configured")

        body = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_message}],
        }
        if output_schema is not None:
            body["output_config"] = {
                "format": {"type": "json_schema", "schema": output_schema}
            }

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=data,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:
                pass
            raise AnthropicError(
                f"HTTP {exc.code}: {exc.reason}",
                status=exc.code,
                body=err_body,
            ) from exc
        except urllib.error.URLError as exc:
            raise AnthropicError(f"Network error: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def extract_text(response: dict) -> str:
        """Concatenate text from all `text` content blocks in the response."""
        blocks = response.get("content", []) or []
        parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)

    @staticmethod
    def extract_usage(response: dict) -> dict:
        """Return the usage dict with cache hit fields defaulted to 0 if absent."""
        usage = response.get("usage", {}) or {}
        return {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        }

    @staticmethod
    def estimate_cost_gbp(usage: dict, model: str) -> float:
        """Rough cost estimate in GBP using cached USD-per-1M rates. Approximate.
        Exchange rate pegged at 1 USD = 0.79 GBP (2026-04 snapshot)."""
        # USD per 1M tokens: (input, output, cache_write_1.25x, cache_read_0.1x)
        rates = {
            "claude-opus-4-7": (5.00, 25.00),
            "claude-opus-4-6": (5.00, 25.00),
            "claude-sonnet-4-6": (3.00, 15.00),
            "claude-haiku-4-5": (1.00, 5.00),
            "claude-haiku-4-5-20251001": (1.00, 5.00),
        }
        in_usd, out_usd = rates.get(model, (3.00, 15.00))
        input_cost = usage.get("input_tokens", 0) / 1_000_000 * in_usd
        output_cost = usage.get("output_tokens", 0) / 1_000_000 * out_usd
        cache_write = usage.get("cache_creation_input_tokens", 0) / 1_000_000 * in_usd * 1.25
        cache_read = usage.get("cache_read_input_tokens", 0) / 1_000_000 * in_usd * 0.10
        total_usd = input_cost + output_cost + cache_write + cache_read
        return round(total_usd * 0.79, 4)
