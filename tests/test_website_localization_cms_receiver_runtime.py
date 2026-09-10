from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HELPERS = load(
    "blun_test_website_localization_cms_receiver_runtime_helpers",
    ROOT / "tests" / "test_website_localization_cms_receiver.py",
)
RUNTIME = load(
    "blun_test_website_localization_cms_receiver_runtime",
    ROOT / "integrations" / "website_localization_cms_receiver_runtime.py",
)
CMS = HELPERS.CMS
HTTP = HELPERS.HTTP


class DurableCMSReceiverRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.publication_authority = HELPERS.Authority(
            b"publication-key", "publication-key-1",
        )
        self.acknowledgement_authority = HELPERS.Authority(
            b"ack-key", "ack-key-1",
        )
        self.contract_sha256 = (
            CMS.WebsiteLocalizationCMSBridge._publication_http_capabilities()[
                "sha256"
            ]
        )

    def open(self, database_path, **overrides):
        values = {
            "database": database_path,
            "publication_authority": self.publication_authority,
            "acknowledgement_authority": self.acknowledgement_authority,
            "authenticate": lambda headers: (
                headers.get("authorization") == "Bearer secret"
            ),
            "contract_sha256": self.contract_sha256,
            "clock": lambda: 1000,
        }
        values.update(overrides)
        return RUNTIME.open_durable_cms_receiver(**values)

    def test_composed_runtime_persists_across_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receiver.sqlite3"
            first = self.open(path)
            publication = HELPERS.publication_payload()
            first.register_source(HELPERS.expectation(publication))
            request = HELPERS.request(publication, self.publication_authority)
            publisher = HTTP.HTTPPublisherAdapter(
                "https://cms.example.test/v1/localization/callback",
                lambda: {"Authorization": "Bearer secret"},
                self.acknowledgement_authority,
                transport=HELPERS.WSGIReceiverTransport(first),
                probe_id_factory=lambda: "publisher-health-probe-501",
            )

            accepted = publisher.publish(request)
            first.close()
            first.close()

            second = self.open(path)
            try:
                restarted_publisher = publisher.__class__(
                    "https://cms.example.test/v1/localization/callback",
                    lambda: {"Authorization": "Bearer secret"},
                    self.acknowledgement_authority,
                    transport=HELPERS.WSGIReceiverTransport(second),
                    probe_id_factory=lambda: "publisher-health-probe-502",
                )
                replay = restarted_publisher.publish(request)
                self.assertEqual(replay, accepted)
                self.assertEqual(
                    second.read_active_bundle(
                        publication["site_id"], publication["source_id"],
                    ),
                    publication,
                )
                self.assertNotIn("secret", repr(second))
                self.assertNotIn(str(path), repr(second))

                tombstone = HELPERS.tombstone_payload()
                tombstone.update({
                    "event_id": publication["event_id"],
                    "site_id": publication["site_id"],
                    "website_version": publication["website_version"],
                    "plan_id": publication["plan_id"],
                    "source_id": publication["source_id"],
                    "source_sequence": publication["source_sequence"],
                    "publication_delivery_id": publication["delivery_id"],
                    "publication_payload_sha256": request.payload_sha256,
                    "locales": ["fi-FI"],
                })
                HELPERS.rebind_tombstone(tombstone)
                second.register_tombstone(
                    HELPERS.tombstone_expectation(tombstone)
                )
                deletion = HELPERS.tombstone_request(
                    tombstone, self.publication_authority,
                )
                self.assertEqual(
                    restarted_publisher.publish(deletion)["status"], "deleted",
                )
                self.assertEqual(
                    restarted_publisher.check(
                        contract_sha256=self.contract_sha256,
                    )["status"],
                    "healthy",
                )
                self.assertIsNone(
                    second.read_active_bundle(
                        publication["site_id"], publication["source_id"],
                    )
                )
            finally:
                second.close()

    def test_invalid_configuration_does_not_create_database(self):
        cases = (
            {"contract_sha256": "wrong"},
            {"authenticate": None},
            {"path": "relative"},
            {"require_https": "yes"},
            {"acknowledgement_authority": self.publication_authority},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / f"receiver-{index}.sqlite3"
                    with self.assertRaises(
                        RUNTIME.DurableCMSReceiverRuntimeBlocked
                    ):
                        self.open(path, **overrides)
                    self.assertFalse(path.exists())

    def test_invalid_database_paths_block_before_open(self):
        for path in (b"receiver.sqlite3", "file:receiver.sqlite3", "", "bad\x00path"):
            with self.subTest(path=path):
                with self.assertRaises(
                    RUNTIME.DurableCMSReceiverRuntimeBlocked
                ):
                    self.open(path)

    def test_schema_failure_closes_connection_and_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receiver.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.commit()
            connection.close()

            with self.assertRaises(
                RUNTIME.DurableCMSReceiverRuntimeBlocked
            ):
                self.open(path)

            check = sqlite3.connect(path)
            try:
                self.assertEqual(
                    check.execute("PRAGMA user_version").fetchone()[0], 99,
                )
                tables = check.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                self.assertEqual(tables, [])
            finally:
                check.close()

    def test_closed_runtime_rejects_trusted_host_operations(self):
        runtime = self.open(":memory:")
        runtime.close()
        for operation in (
            lambda: runtime.register_source(HELPERS.expectation(
                HELPERS.publication_payload()
            )),
            lambda: runtime.register_tombstone({}),
            lambda: runtime.read_active_bundle("public-site", "homepage.pricing"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(
                    RUNTIME.DurableCMSReceiverRuntimeBlocked
                ):
                    operation()

    def test_http_failure_after_close_is_content_free(self):
        runtime = self.open(":memory:")
        publication = HELPERS.publication_payload()
        runtime.register_source(HELPERS.expectation(publication))
        request = HELPERS.request(publication, self.publication_authority)
        runtime.close()
        publisher = HTTP.HTTPPublisherAdapter(
            "https://cms.example.test/v1/localization/callback",
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=HELPERS.WSGIReceiverTransport(runtime),
        )

        with self.assertRaises(HTTP.HTTPPublisherFailed) as caught:
            publisher.publish(request)

        self.assertEqual(caught.exception.code, "http_status")
        self.assertNotIn(publication["localizations"][0]["target_text"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
