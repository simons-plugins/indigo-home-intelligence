"""Tests for how a fresh MCP client learns what THIS server is for.

HI is not a general Indigo API, and the most expensive mistake a
client can make is reaching for it to read or control the live house.
The MCP ``instructions`` string (InitializeResult) is read once, up
front, before any tool is chosen, so it is the only place that
boundary — and the pointer at indigo-mcp-lite — can be stated.

The handler mechanism itself is the keep-aligned copy of lite's; the
text is HI's own.
"""

import importlib.util
import json
import logging
import pathlib
import sys

from mcp_handler import MCPHandler


def _initialize(handler):
    response = handler.handle_request(
        "POST",
        {"Content-Type": "application/json", "Accept": "application/json"},
        json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        }),
    )
    assert response["status"] == 200, response
    return json.loads(response["content"])["result"]


def _handler(**kwargs):
    return MCPHandler(
        logger=logging.getLogger("test-mcp"),
        server_name="home-intelligence",
        server_version="0-test",
        **kwargs,
    )


def test_instructions_absent_when_not_supplied():
    # Optional per spec: omitted rather than sent empty, so a client
    # can tell "not provided" from "nothing to say".
    assert "instructions" not in _initialize(_handler())


def test_instructions_returned_when_supplied():
    result = _initialize(_handler(instructions="Read me first."))
    assert result["instructions"] == "Read me first."


def test_instructions_do_not_disturb_the_rest_of_initialize():
    result = _initialize(_handler(instructions="x"))
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"] == "home-intelligence"
    assert "capabilities" in result


def _plugin_module():
    path = (pathlib.Path(__file__).parent.parent
            / "Home Intelligence.indigoPlugin" / "Contents"
            / "Server Plugin" / "plugin.py")
    spec = importlib.util.spec_from_file_location("_hi_plugin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_hi_plugin"] = module
    spec.loader.exec_module(module)
    return module


def test_instructions_send_live_house_questions_to_lite():
    # The boundary is the whole point: HI has no live device state,
    # no control, no search. A client that doesn't learn this here
    # will try to use HI as a general Indigo API.
    text = _plugin_module().SERVER_INSTRUCTIONS
    assert "indigo-mcp-lite" in text
    assert "NOT a general Indigo API" in text


def test_instructions_name_every_tool_family():
    text = _plugin_module().SERVER_INSTRUCTIONS
    for tool in ("house_context_snapshot", "get_observations", "get_rules",
                 "propose_rule", "add_rule", "update_rule"):
        assert tool in text, tool


def test_instructions_state_the_propose_before_add_rule_order():
    # Writing a rule the user never confirmed is the one destructive
    # mistake available here.
    text = _plugin_module().SERVER_INSTRUCTIONS
    assert "propose_rule" in text and "confirm" in text
