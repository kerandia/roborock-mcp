import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import roborock_mcp_server as server


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_inventory_without_connecting(self):
        with patch.object(server, "_get_device", new_callable=AsyncMock) as connect:
            tools = await server.mcp.list_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {"get_status", "list_rooms", "get_consumables", "get_clean_history",
             "start_clean", "clean_rooms", "pause", "stop", "return_to_dock",
             "locate", "set_fan_power", "drive"},
        )
        connect.assert_not_awaited()

    async def test_missing_credentials_fail_before_discovery(self):
        with patch.object(server, "_device", None), patch.object(
            server, "USER_DATA_PATH", Path("/nonexistent/roborock-test-credentials.json")
        ), patch.object(server, "create_device_manager", new_callable=AsyncMock) as create:
            with self.assertRaisesRegex(RuntimeError, "No cached credentials"):
                await server._get_device()
            create.assert_not_awaited()

    async def test_empty_rooms_send_nothing(self):
        with patch.object(server, "_send", new_callable=AsyncMock) as send:
            result = await server.clean_rooms([])
            self.assertIn("No segments", result)
            send.assert_not_awaited()

    async def test_invalid_fan_code_send_nothing(self):
        device = SimpleNamespace(v1_properties=SimpleNamespace(
            status=SimpleNamespace(fan_speed_mapping={101: "quiet", 104: "max"})
        ))
        with patch.object(server, "_get_device", AsyncMock(return_value=device)), patch.object(
            server, "_send", new_callable=AsyncMock
        ) as send:
            self.assertIn("Not sent", await server.set_fan_power(999))
            send.assert_not_awaited()

    async def test_close_clears_cached_connection(self):
        manager = SimpleNamespace(close=AsyncMock())
        with patch.object(server, "_manager", manager), patch.object(
            server, "_manager_loop", asyncio.get_running_loop()
        ), patch.object(server, "_device", object()):
            await server.close_connection()
            manager.close.assert_awaited_once()
            self.assertIsNone(server._device)
            self.assertIsNone(server._manager)
            self.assertIsNone(server._manager_loop)

    async def test_cross_loop_reuse_is_rejected(self):
        with patch.object(server, "_device", object()), patch.object(
            server, "_manager_loop", object()
        ):
            with self.assertRaisesRegex(RuntimeError, "different"):
                await server._get_device()


if __name__ == "__main__":
    unittest.main()
