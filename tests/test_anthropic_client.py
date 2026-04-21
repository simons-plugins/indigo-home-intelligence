"""Tests for anthropic_client.AnthropicClient response-parsing helpers."""

from anthropic_client import AnthropicClient


class TestExtractText:
    def test_single_text_block(self):
        response = {"content": [{"type": "text", "text": "Hello world"}]}
        assert AnthropicClient.extract_text(response) == "Hello world"

    def test_multiple_text_blocks_concatenated(self):
        response = {
            "content": [
                {"type": "text", "text": "Part one. "},
                {"type": "text", "text": "Part two."},
            ]
        }
        assert AnthropicClient.extract_text(response) == "Part one. Part two."

    def test_mixed_text_and_other_blocks(self):
        response = {
            "content": [
                {"type": "text", "text": "Keep me"},
                {"type": "tool_use", "name": "some_tool", "input": {}},
                {"type": "text", "text": " and me."},
            ]
        }
        assert AnthropicClient.extract_text(response) == "Keep me and me."

    def test_empty_content_returns_empty_string(self):
        assert AnthropicClient.extract_text({"content": []}) == ""

    def test_missing_content_key_returns_empty_string(self):
        assert AnthropicClient.extract_text({}) == ""

    def test_text_block_without_text_field(self):
        # Malformed but shouldn't crash
        response = {"content": [{"type": "text"}]}
        assert AnthropicClient.extract_text(response) == ""


class TestExtractUsage:
    def test_full_usage_passthrough(self):
        response = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 200,
                "cache_read_input_tokens": 300,
            }
        }
        usage = AnthropicClient.extract_usage(response)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["cache_creation_input_tokens"] == 200
        assert usage["cache_read_input_tokens"] == 300

    def test_missing_cache_fields_default_to_zero(self):
        response = {"usage": {"input_tokens": 50, "output_tokens": 25}}
        usage = AnthropicClient.extract_usage(response)
        assert usage["cache_creation_input_tokens"] == 0
        assert usage["cache_read_input_tokens"] == 0
        assert usage["input_tokens"] == 50

    def test_missing_usage_entirely_returns_all_zeros(self):
        usage = AnthropicClient.extract_usage({})
        assert usage == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_usage_none_returns_all_zeros(self):
        # Defensive: API wire representation may occasionally have null.
        usage = AnthropicClient.extract_usage({"usage": None})
        assert usage["input_tokens"] == 0


class TestEstimateCost:
    def test_known_model_returns_nonzero_cost(self):
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost = AnthropicClient.estimate_cost_gbp(usage, "claude-sonnet-4-6")
        # Sonnet: $3/$15 per 1M; 1M in + 1M out = $18 USD → ~£14.22
        assert 12.0 < cost < 16.0

    def test_unknown_model_falls_back_to_sonnet_rate(self):
        # Fallback keeps cost reporting working even for new models.
        # Not strictly required, but documented behaviour.
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        cost = AnthropicClient.estimate_cost_gbp(usage, "claude-unknown-model")
        assert cost > 0

    def test_cache_read_cheaper_than_uncached_input(self):
        cache_read_heavy = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1_000_000,
        }
        uncached_heavy = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        assert AnthropicClient.estimate_cost_gbp(
            cache_read_heavy, "claude-sonnet-4-6"
        ) < AnthropicClient.estimate_cost_gbp(
            uncached_heavy, "claude-sonnet-4-6"
        )
