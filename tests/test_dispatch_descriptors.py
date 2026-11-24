"""Exercise the client-visible descriptor identity and pre-mutation rejection."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import mcp_server, store
from test_orchd import FakeRuntime


class DispatchDescriptorsTest(unittest.TestCase):
    def test_finalized_descriptor_content_changes_identity_without_mutating_legacy(self):
        original = copy.deepcopy(mcp_server.TOOLS)
        before = mcp_server.dispatch_contract()
        self.assertEqual(before, mcp_server.dispatch_contract())
        for change in ("origin", "type", "description", "annotations"):
            tools = copy.deepcopy(original)
            dispatch = next(t for t in tools if t["name"] == "dispatch")
            if change == "origin":
                dispatch["inputSchema"]["properties"]["synthetic_origin"] = {"type": "integer"}
            elif change == "type":
                dispatch["inputSchema"]["properties"]["repo"]["type"] = "integer"
            else:
                dispatch[change] = "synthetic changed value"
            with patch.object(mcp_server, "TOOLS", tools):
                self.assertNotEqual(before["dispatch_tool"], mcp_server.dispatch_contract()["dispatch_tool"])
        aliases = mcp_server.dispatch_descriptors()
        for alias in aliases:
            self.assertLessEqual(len("mcp__orchd__" + alias["name"]), 64)
            alias["inputSchema"]["properties"].clear()
        self.assertEqual(original, mcp_server.TOOLS)

    def test_alias_routes_to_same_dispatch_handler_and_preserves_caller(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=True):
            con = store.connect(Path(root) / "test.db")
            self.addCleanup(con.close)
            rt = FakeRuntime()
            result = mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                "name": mcp_server.dispatch_contract()["dispatch_tool"], "_meta": {"threadId": "synthetic-owner"},
                "arguments": {"repo": "demo", "title": "T", "instructions": "i", "done_when": "d",
                              "model_reason": "r", "task_type": "code"}}}, con, rt)["result"]
            self.assertNotIn("isError", result)
            self.assertEqual(json.loads(result["content"][0]["text"])["orch_id"], "synthetic-owner")

    def test_stale_alias_rejected_before_registration_or_direct_call(self):
        for env in ({}, {"ORCHD_ORCH_ID": "synthetic-owner"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with patch.object(store, "register_orch") as register, patch.object(mcp_server.core, "dispatch") as dispatch:
                    for name in ("dispatch_s000000000000", "dispatch_batch_s000000000000"):
                        result = mcp_server.handle({"id": 1, "method": "tools/call", "params": {
                            "name": name, "arguments": {}, "_meta": {"threadId": "synthetic-owner"}}}, None, None)
                        self.assertTrue(result["result"]["isError"])
                        with self.assertRaisesRegex(ValueError, "Stale dispatch descriptor"):
                            mcp_server.call(name, {}, "synthetic-owner", None, None)
                    register.assert_not_called()
                    dispatch.assert_not_called()

    def test_batch_alias_uses_final_nested_descriptor_and_handler(self):
        before = mcp_server.dispatch_contract()
        tools = copy.deepcopy(mcp_server.TOOLS)
        batch = next(t for t in tools if t["name"] == "dispatch_batch")
        batch["inputSchema"]["properties"]["tasks"]["items"]["properties"]["synthetic_origin"] = {"type": "integer"}
        with patch.object(mcp_server, "TOOLS", tools):
            self.assertNotEqual(before["dispatch_batch_tool"], mcp_server.dispatch_contract()["dispatch_batch_tool"])
        with patch.object(mcp_server.core, "dispatch_batch", return_value={"synthetic": True}) as dispatch:
            result = mcp_server.call(before["dispatch_batch_tool"], {"tasks": []}, "synthetic-owner", None, None)
            self.assertEqual(result, {"synthetic": True})
            dispatch.assert_called_once_with(None, None, orch_thread="synthetic-owner", tasks=[])
