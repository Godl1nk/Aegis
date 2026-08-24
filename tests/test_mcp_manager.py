import asyncio
from unittest.mock import patch

from src.mcp_manager import _format_mcp_connection_error, McpManager


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Aegis" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()


def test_mcp_tool_call_times_out_instead_of_hanging():
    mgr = McpManager()

    class HangingSession:
        async def call_tool(self, name, arguments):
            await asyncio.Event().wait()

    mgr._sessions["slow"] = HangingSession()
    with patch("src.mcp_manager._MCP_TOOL_CALL_TIMEOUT_S", 0.01):
        result = asyncio.run(mgr.call_tool("mcp__slow__wait_forever", {}))

    assert result["exit_code"] == 1
    assert "timed out" in result["error"]


def test_builtin_mcp_reconnect_times_out_instead_of_hanging():
    mgr = McpManager()
    mgr._sessions["builtin_slow"] = object()

    async def fail_call(*args, **kwargs):
        raise RuntimeError("transport died")

    async def hang_reconnect(*args, **kwargs):
        await asyncio.Event().wait()

    with patch.object(mgr, "_do_call", side_effect=fail_call), \
         patch.object(mgr, "is_builtin", return_value=True), \
         patch.object(mgr, "_reconnect_builtin", side_effect=hang_reconnect), \
         patch("src.mcp_manager._MCP_RECONNECT_TIMEOUT_S", 0.01):
        result = asyncio.run(mgr.call_tool("mcp__builtin_slow__demo", {}))

    assert result["exit_code"] == 1
    assert "reconnect timed out" in result["error"]
