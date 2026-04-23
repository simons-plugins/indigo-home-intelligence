"""Tests for MCPHandler — JSON-RPC 2.0 protocol negotiation, method
dispatch, tool/resource invocation, and error envelopes.

Exercises the handler without touching indigo or IWS — all deps are
injected, all requests are synthesised as plain dicts.
"""

import json
import logging

import pytest

from mcp_handler import MCPHandler, SUPPORTED_PROTOCOL_VERSIONS


@pytest.fixture
def handler():
    """Fresh MCPHandler with no tools/resources registered. Logger is
    the stdlib one so we see warnings during test failures but don't
    assert on specific messages."""
    return MCPHandler(
        logger=logging.getLogger("test-mcp"),
        server_name="home-intelligence",
        server_version="2026.1.0-test",
    )


def _rpc(method: str, msg_id=1, params=None, extra=None):
    """Build a JSON-RPC 2.0 envelope and POST it to the handler."""
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        body["params"] = params
    if extra:
        body.update(extra)
    return json.dumps(body)


def _post(handler, body, headers=None):
    """Fire a POST through the handler and parse the JSON response body."""
    full_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        full_headers.update(headers)
    resp = handler.handle_request("POST", full_headers, body)
    return resp, json.loads(resp["content"]) if resp["content"] else None


# ---------------------------------------------------------------------
# Transport-level
# ---------------------------------------------------------------------


class TestTransport:
    def test_rejects_non_post(self, handler):
        resp = handler.handle_request("GET", {}, "")
        assert resp["status"] == 405
        assert resp["headers"]["Allow"] == "POST"

    def test_rejects_incompatible_accept(self, handler):
        resp = handler.handle_request(
            "POST", {"Accept": "application/xml"}, _rpc("initialize")
        )
        assert resp["status"] == 406

    def test_accepts_wildcard_accept(self, handler):
        # `mcp-remote` (npm bridge) sometimes strips the Accept header
        # or sends `*/*` — both must be treated as acceptable.
        resp = handler.handle_request(
            "POST", {"Accept": "*/*"},
            _rpc("initialize", params={"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]}),
        )
        assert resp["status"] == 200

    def test_missing_accept_is_allowed(self, handler):
        resp = handler.handle_request(
            "POST", {},
            _rpc("initialize", params={"protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0]}),
        )
        assert resp["status"] == 200

    def test_bad_json_returns_parse_error(self, handler):
        resp = handler.handle_request(
            "POST", {"Accept": "application/json"}, "not json"
        )
        assert resp["status"] == 200  # JSON-RPC errors ride on 200
        body = json.loads(resp["content"])
        assert body["error"]["code"] == -32700

    def test_batch_rejected(self, handler):
        body = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        resp = handler.handle_request(
            "POST", {"Accept": "application/json"}, body
        )
        assert resp["status"] == 200
        body = json.loads(resp["content"])
        assert body["error"]["code"] == -32600

    def test_notification_returns_empty_body(self, handler):
        # Notification has no ``id`` → no response body per JSON-RPC 2.0.
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp = handler.handle_request(
            "POST", {"Accept": "application/json"}, body
        )
        assert resp["status"] == 200
        assert resp["content"] == "{}"


# ---------------------------------------------------------------------
# initialize handshake
# ---------------------------------------------------------------------


class TestInitialize:
    def test_initialize_echoes_protocol_version(self, handler):
        pv = SUPPORTED_PROTOCOL_VERSIONS[0]
        _, body = _post(handler, _rpc("initialize", params={
            "protocolVersion": pv,
            "capabilities": {},
            "clientInfo": {"name": "claude-desktop", "version": "0.1.0"},
        }))
        assert body["result"]["protocolVersion"] == pv
        assert body["result"]["serverInfo"]["name"] == "home-intelligence"
        assert body["result"]["serverInfo"]["version"] == "2026.1.0-test"
        assert "tools" in body["result"]["capabilities"]
        assert "resources" in body["result"]["capabilities"]

    def test_initialize_issues_session_header(self, handler):
        resp, _ = _post(handler, _rpc("initialize", params={
            "protocolVersion": SUPPORTED_PROTOCOL_VERSIONS[0],
        }))
        assert "Mcp-Session-Id" in resp["headers"]
        assert len(resp["headers"]["Mcp-Session-Id"]) >= 16

    def test_unsupported_protocol_version_errors(self, handler):
        _, body = _post(handler, _rpc("initialize", params={
            "protocolVersion": "1999-01-01",
        }))
        assert body["error"]["code"] == -32602
        assert "supported" in body["error"]["data"]
        assert SUPPORTED_PROTOCOL_VERSIONS[0] in body["error"]["data"]["supported"]

    def test_accepts_legacy_supported_version(self, handler):
        # 2025-06-18 should still negotiate — that's why we keep it in
        # SUPPORTED_PROTOCOL_VERSIONS alongside the newer one.
        _, body = _post(handler, _rpc("initialize", params={
            "protocolVersion": "2025-06-18",
        }))
        assert "result" in body
        assert body["result"]["protocolVersion"] == "2025-06-18"


# ---------------------------------------------------------------------
# ping / unknown method
# ---------------------------------------------------------------------


class TestBasicMethods:
    def test_ping_returns_empty_result(self, handler):
        _, body = _post(handler, _rpc("ping"))
        assert body == {"jsonrpc": "2.0", "id": 1, "result": {}}

    def test_unknown_method_returns_method_not_found(self, handler):
        _, body = _post(handler, _rpc("does/not/exist"))
        assert body["error"]["code"] == -32601

    def test_prompts_list_returns_empty(self, handler):
        _, body = _post(handler, _rpc("prompts/list"))
        assert body["result"] == {"prompts": []}


# ---------------------------------------------------------------------
# tools/list and tools/call
# ---------------------------------------------------------------------


class TestTools:
    def test_tools_list_empty_when_none_registered(self, handler):
        _, body = _post(handler, _rpc("tools/list"))
        assert body["result"] == {"tools": []}

    def test_tools_list_returns_registered_tool(self, handler):
        handler.register_tool(
            name="echo",
            description="Echo args as a string.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            handler=lambda text="": text,
        )
        _, body = _post(handler, _rpc("tools/list"))
        tools = body["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "echo"
        assert tools[0]["description"] == "Echo args as a string."
        assert "inputSchema" in tools[0]

    def test_tools_call_dispatches_handler(self, handler):
        handler.register_tool(
            name="echo", description="x", input_schema={},
            handler=lambda text="default": f"got: {text}",
        )
        _, body = _post(handler, _rpc("tools/call", params={
            "name": "echo", "arguments": {"text": "hello"},
        }))
        assert body["result"]["content"][0]["text"] == "got: hello"

    def test_tools_call_serialises_dict_result(self, handler):
        handler.register_tool(
            name="stats", description="x", input_schema={},
            handler=lambda: {"count": 3, "items": ["a", "b"]},
        )
        _, body = _post(handler, _rpc("tools/call", params={"name": "stats"}))
        text = body["result"]["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed == {"count": 3, "items": ["a", "b"]}

    def test_tools_call_unknown_tool_errors(self, handler):
        _, body = _post(handler, _rpc("tools/call", params={"name": "nope"}))
        assert body["error"]["code"] == -32602

    def test_tools_call_validation_error_returns_is_error(self, handler):
        def broken(x):
            raise ValueError("x must be positive")
        handler.register_tool(
            name="broken", description="x", input_schema={}, handler=broken,
        )
        _, body = _post(handler, _rpc("tools/call", params={
            "name": "broken", "arguments": {"x": -1},
        }))
        # Validation errors become tool-result errors, not protocol
        # errors — so the model can self-correct.
        assert body["result"]["isError"] is True
        err_text = json.loads(body["result"]["content"][0]["text"])
        assert err_text["error"] == "x must be positive"

    def test_tools_call_internal_error_returns_jsonrpc_error(self, handler):
        def broken():
            raise RuntimeError("unexpected")
        handler.register_tool(
            name="broken", description="x", input_schema={}, handler=broken,
        )
        _, body = _post(handler, _rpc("tools/call", params={"name": "broken"}))
        # Non-validation exceptions become protocol errors so the
        # client can back off rather than retry with altered args.
        assert body["error"]["code"] == -32603


# ---------------------------------------------------------------------
# resources/list and resources/read
# ---------------------------------------------------------------------


class TestResources:
    def test_resources_list_empty(self, handler):
        _, body = _post(handler, _rpc("resources/list"))
        assert body["result"] == {"resources": []}

    def test_resources_list_returns_registered(self, handler):
        handler.register_resource(
            uri="home-intelligence:test_resource",
            name="Test",
            description="A test resource",
            handler=lambda: "body",
            mime_type="text/plain",
        )
        _, body = _post(handler, _rpc("resources/list"))
        resources = body["result"]["resources"]
        assert len(resources) == 1
        assert resources[0]["uri"] == "home-intelligence:test_resource"
        assert resources[0]["mimeType"] == "text/plain"

    def test_resources_read_returns_body(self, handler):
        handler.register_resource(
            uri="home-intelligence:test",
            name="Test", description="x",
            handler=lambda: "the body",
        )
        _, body = _post(handler, _rpc("resources/read", params={
            "uri": "home-intelligence:test",
        }))
        content = body["result"]["contents"][0]
        assert content["text"] == "the body"
        assert content["uri"] == "home-intelligence:test"

    def test_resources_read_unknown_uri_errors(self, handler):
        _, body = _post(handler, _rpc("resources/read", params={
            "uri": "home-intelligence:nope",
        }))
        assert body["error"]["code"] == -32602

    def test_resources_read_missing_uri_errors(self, handler):
        _, body = _post(handler, _rpc("resources/read", params={}))
        assert body["error"]["code"] == -32602
