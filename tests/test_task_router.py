from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "integrations" / "task_router.py"
SPEC = importlib.util.spec_from_file_location("blun_test_task_router", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class TaskRouterTests(unittest.TestCase):
    def test_source_and_target_locale_automatically_route_to_translation(self) -> None:
        route = MODULE.route_host_context({
            "source_text": "Please confirm your choice.",
            "target_language": "sv-SE",
        })
        self.assertEqual(route.task_kind, "translation")
        self.assertEqual(route.language, "sv-SE")

    def test_structured_translation_operation_requires_source(self) -> None:
        with self.assertRaises(MODULE.RoutingBlocked):
            MODULE.route_host_context({"operation": "translate", "target_language": "de-DE"})

    def test_agent_cannot_downgrade_translation_source_to_response(self) -> None:
        with self.assertRaises(MODULE.RoutingBlocked):
            MODULE.route_host_context({
                "task_kind": "response",
                "source_text": "Translate this source.",
                "response_language": "de-DE",
            })

    def test_ordinary_chat_routes_to_response(self) -> None:
        route = MODULE.route_host_context({
            "operation": "chat",
            "response_language": "de-AT",
        })
        self.assertEqual(route.task_kind, "response")
        self.assertEqual(route.source_text, "")

    def test_auto_all_unknown_operation_and_wrong_types_block(self) -> None:
        cases = (
            {"response_language": "auto"},
            {"operation": "maybe", "response_language": "de-DE"},
            {"source_text": ["not", "text"], "target_language": "sv-SE"},
        )
        for context in cases:
            with self.subTest(context=context), self.assertRaises(MODULE.RoutingBlocked):
                MODULE.route_host_context(context)

    def test_conflicting_host_operation_blocks(self) -> None:
        with self.assertRaises(MODULE.RoutingBlocked):
            MODULE.route_host_context({
                "task_kind": "translation",
                "operation": "chat",
                "source_text": "Hello",
                "target_language": "sv-SE",
            })


if __name__ == "__main__":
    unittest.main()
