from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HELPERS = load(
    "blun_test_website_localization_cms_receiver_store_helpers",
    ROOT / "tests" / "test_website_localization_cms_receiver.py",
)
STORE = load(
    "blun_test_website_localization_cms_receiver_store",
    ROOT / "integrations" / "website_localization_cms_receiver_store.py",
)
RECEIVER = HELPERS.RECEIVER
CMS = HELPERS.CMS
HTTP = HELPERS.HTTP


def rebind_publication(payload):
    unsigned = {key: value for key, value in payload.items() if key != "delivery_id"}
    payload["delivery_id"] = "blun-cms-delivery-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return payload


def next_publication(payload):
    result = json.loads(json.dumps(payload))
    result.update({
        "event_id": "cms-event-202",
        "website_version": "website-202",
        "source_revision": "cms-202",
        "source_sequence": 202,
        "source_sha256": "a" * 64,
    })
    target = "Aloita maksutta – uusi hinta 480 € vuodessa."
    evidence = HELPERS.release_evidence(target)
    result["localizations"] = [{
        "locale": "fi-FI",
        "target_text": target,
        "target_sha256": evidence["target_sha256"],
        "approval_id": evidence["approval_id"],
        "approval_expires_at": 2000,
        "release_evidence": evidence,
    }]
    return rebind_publication(result)


def tombstone_for(publication, publication_payload_sha256):
    unsigned = {
        "schema": CMS.TOMBSTONE_DELIVERY_SCHEMA,
        "tombstone_id": "cms-tombstone-201",
        "event_id": publication["event_id"],
        "site_id": publication["site_id"],
        "website_version": publication["website_version"],
        "plan_id": publication["plan_id"],
        "source_id": publication["source_id"],
        "source_sequence": publication["source_sequence"],
        "publication_delivery_id": publication["delivery_id"],
        "publication_payload_sha256": publication_payload_sha256,
        "locales": [item["locale"] for item in publication["localizations"]],
    }
    delivery_id = "blun-cms-tombstone-" + hashlib.sha256(
        RECEIVER._canonical_json(unsigned, maximum=RECEIVER.MAX_REQUEST_BYTES)
    ).hexdigest()
    return {**unsigned, "delivery_id": delivery_id}


class DurableCMSReceiverStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.store = STORE.DurableCMSReceiverStore(
            self.connection, clock=lambda: 1000,
        )
        self.assertEqual(
            self.connection.execute("PRAGMA secure_delete").fetchone()[0], 1,
        )
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

    def tearDown(self):
        self.connection.close()

    def application(self):
        return RECEIVER.CMSReceiverApplication(
            publication_authority=self.publication_authority,
            acknowledgement_authority=self.acknowledgement_authority,
            authenticate=lambda headers: (
                headers.get("authorization") == "Bearer secret"
            ),
            resolve_publication_expectation=(
                self.store.resolve_publication_expectation
            ),
            commit=self.store.commit,
            resolve_tombstone_expectation=self.store.resolve_tombstone_expectation,
            delete=self.store.delete,
            check=self.store.check,
            contract_sha256=self.contract_sha256,
            clock=lambda: 1000,
        )

    def publisher(self):
        return HTTP.HTTPPublisherAdapter(
            "https://cms.example.test" + RECEIVER.RECEIVER_PATH,
            lambda: {"Authorization": "Bearer secret"},
            self.acknowledgement_authority,
            transport=HELPERS.WSGIReceiverTransport(self.application()),
            probe_id_factory=lambda: "publisher-health-probe-401",
        )

    def test_durable_callbacks_complete_publication_deletion_and_health(self):
        publication = HELPERS.publication_payload()
        expected = HELPERS.expectation(publication)
        self.store.register_source(expected)
        request = HELPERS.request(publication, self.publication_authority)
        publisher = self.publisher()

        first = publisher.publish(request)
        replay = publisher.publish(request)
        active = self.store.read_active_bundle(expected)
        tombstone = tombstone_for(publication, request.payload_sha256)
        self.store.register_tombstone(
            HELPERS.tombstone_expectation(tombstone),
        )
        deletion = HELPERS.tombstone_request(
            tombstone, self.publication_authority,
        )
        deleted = publisher.publish(deletion)
        deleted_replay = publisher.publish(deletion)
        registration_replay = self.store.register_tombstone(
            HELPERS.tombstone_expectation(tombstone),
        )
        healthy = publisher.check(contract_sha256=self.contract_sha256)

        self.assertEqual(first["status"], "accepted")
        self.assertEqual(replay, first)
        self.assertEqual(active, publication)
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(deleted_replay, deleted)
        self.assertEqual(registration_replay["status"], "deleted")
        self.assertEqual(healthy["status"], "healthy")
        self.assertIsNone(
            self.store.read_active_bundle(expected)
        )
        stored = self.connection.execute(
            "SELECT status, payload_json FROM cms_receiver_publications"
        ).fetchone()
        self.assertEqual(tuple(stored), ("deleted", None))
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cms_receiver_localizations"
            ).fetchone()[0],
            0,
        )

    def test_new_expectation_preserves_last_good_until_atomic_commit(self):
        first = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(first))
        first_verified = HELPERS.request(first, self.publication_authority)
        self.store.commit(first_verified)
        second = next_publication(first)

        self.store.register_source(HELPERS.expectation(second))

        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(first)),
            first,
        )
        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.read_active_bundle(HELPERS.expectation(second))
        self.store.commit(HELPERS.request(second, self.publication_authority))
        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(second)),
            second,
        )
        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.read_active_bundle(HELPERS.expectation(first))
        states = self.connection.execute(
            "SELECT source_sequence, status, payload_json "
            "FROM cms_receiver_publications "
            "ORDER BY source_sequence"
        ).fetchall()
        self.assertEqual(states[0]["source_sequence"], 201)
        self.assertEqual(states[0]["status"], "superseded")
        self.assertIsNone(states[0]["payload_json"])
        self.assertEqual(states[1]["source_sequence"], 202)
        self.assertEqual(states[1]["status"], "active")
        self.assertIsNotNone(states[1]["payload_json"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM cms_receiver_localizations "
                "WHERE delivery_id = ?",
                (first["delivery_id"],),
            ).fetchone()[0],
            0,
        )

    def test_superseded_cleanup_failure_rolls_back_to_last_good(self):
        first = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(first))
        self.store.commit(HELPERS.request(first, self.publication_authority))
        second = next_publication(first)
        self.store.register_source(HELPERS.expectation(second))
        self.connection.execute("""
            CREATE TRIGGER reject_superseded_cleanup
            BEFORE DELETE ON cms_receiver_localizations
            BEGIN
                SELECT RAISE(ABORT, 'private simulated cleanup failure');
            END
        """)

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.commit(HELPERS.request(second, self.publication_authority))

        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(first)),
            first,
        )
        rows = self.connection.execute(
            "SELECT source_sequence, status, payload_json "
            "FROM cms_receiver_publications"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0][:2]), (201, "active"))
        self.assertIsNotNone(rows[0]["payload_json"])

    def test_health_rejects_content_restored_to_superseded_record(self):
        first = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(first))
        self.store.commit(HELPERS.request(first, self.publication_authority))
        second = next_publication(first)
        self.store.register_source(HELPERS.expectation(second))
        self.store.commit(HELPERS.request(second, self.publication_authority))
        self.connection.execute(
            "UPDATE cms_receiver_publications SET payload_json = ? "
            "WHERE delivery_id = ?",
            (STORE._canonical_json(first), first["delivery_id"]),
        )

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.check(SimpleNamespace(
                probe_id="receiver-store-health-restore-1",
                contract_sha256=self.contract_sha256,
            ))

    def test_operations_block_if_secure_deletion_is_disabled(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        self.connection.execute("PRAGMA secure_delete = OFF")

        for operation in (
            lambda: self.store.commit(
                HELPERS.request(publication, self.publication_authority)
            ),
            lambda: self.store.check(SimpleNamespace(
                probe_id="receiver-store-health-secure-delete-1",
                contract_sha256=self.contract_sha256,
            )),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(STORE.CMSReceiverStoreBlocked):
                    operation()

    def test_source_advance_between_resolution_and_commit_blocks(self):
        first = HELPERS.publication_payload()
        verified = HELPERS.request(first, self.publication_authority)
        self.store.register_source(HELPERS.expectation(first))
        resolved = self.store.resolve_publication_expectation(verified)
        second = next_publication(first)

        self.store.register_source(HELPERS.expectation(second))

        self.assertEqual(resolved["source_sequence"], 201)
        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.commit(verified)
        self.assertIsNone(
            self.store.read_active_bundle(HELPERS.expectation(second))
        )

    def test_failed_bundle_write_rolls_back_and_preserves_last_good(self):
        first = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(first))
        self.store.commit(HELPERS.request(first, self.publication_authority))
        second = next_publication(first)
        self.store.register_source(HELPERS.expectation(second))
        self.connection.execute("""
            CREATE TRIGGER reject_receiver_locale
            BEFORE INSERT ON cms_receiver_localizations
            BEGIN
                SELECT RAISE(ABORT, 'private simulated storage failure');
            END
        """)

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.commit(HELPERS.request(second, self.publication_authority))

        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(first)),
            first,
        )
        rows = self.connection.execute(
            "SELECT source_sequence, status FROM cms_receiver_publications"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(201, "active")])

    def test_unregistered_or_wrong_tombstone_cannot_remove_content(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        request = HELPERS.request(publication, self.publication_authority)
        self.store.commit(request)
        tombstone = tombstone_for(publication, request.payload_sha256)
        wrong = HELPERS.tombstone_expectation(
            tombstone, publication_payload_sha256="0" * 64,
        )

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.register_tombstone(wrong)
        with self.assertRaises(HTTP.HTTPPublisherFailed) as caught:
            self.publisher().publish(
                HELPERS.tombstone_request(tombstone, self.publication_authority),
            )

        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "http_status")
        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(publication)),
            publication,
        )

    def test_pending_tombstone_prevents_source_generation_advance(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        request = HELPERS.request(publication, self.publication_authority)
        self.store.commit(request)
        tombstone = tombstone_for(publication, request.payload_sha256)
        self.store.register_tombstone(HELPERS.tombstone_expectation(tombstone))

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.register_source(
                HELPERS.expectation(next_publication(publication))
            )

        self.assertEqual(
            self.store.read_active_bundle(HELPERS.expectation(publication)),
            publication,
        )

    def test_health_blocks_content_free_on_durable_state_tampering(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        self.store.commit(HELPERS.request(publication, self.publication_authority))
        self.connection.execute(
            "UPDATE cms_receiver_publications SET payload_json = NULL"
        )
        self.connection.commit()
        body, headers = HELPERS.wire_health(
            "publisher-health-probe-401", self.contract_sha256,
        )

        status, _, response = HELPERS.call_wsgi(
            self.application(), HELPERS.wsgi_environ(body, headers),
        )

        result = json.loads(response)
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(result["error"], "receiver.health_check_failed")
        self.assertTrue(result["retryable"])
        self.assertNotIn("Aloita", response.decode("utf-8"))

    def test_health_detects_tampered_locale_row(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        self.store.commit(HELPERS.request(publication, self.publication_authority))
        self.connection.execute(
            "UPDATE cms_receiver_localizations SET target_text = ?",
            ("Manipulated target text",),
        )
        self.connection.commit()

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.check(SimpleNamespace(
                probe_id="publisher-health-probe-401",
                contract_sha256=self.contract_sha256,
            ))

    def test_health_detects_tampered_source_and_tombstone_expectations(self):
        publication = HELPERS.publication_payload()
        self.store.register_source(HELPERS.expectation(publication))
        probe = SimpleNamespace(
            probe_id="publisher-health-probe-401",
            contract_sha256=self.contract_sha256,
        )
        self.connection.execute(
            "UPDATE cms_receiver_sources SET source_revision = 'tampered'"
        )
        self.connection.commit()

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.check(probe)

        self.connection.execute(
            "UPDATE cms_receiver_sources SET source_revision = 'cms-201'"
        )
        self.connection.commit()
        request = HELPERS.request(publication, self.publication_authority)
        self.store.commit(request)
        tombstone = tombstone_for(publication, request.payload_sha256)
        self.store.register_tombstone(HELPERS.tombstone_expectation(tombstone))
        self.connection.execute(
            "UPDATE cms_receiver_tombstones SET event_id = 'tampered-event'"
        )
        self.connection.commit()

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            self.store.check(probe)

    def test_file_backed_restart_preserves_idempotency(self):
        publication = HELPERS.publication_payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cms-receiver.sqlite3"
            first_connection = sqlite3.connect(path)
            first = STORE.DurableCMSReceiverStore(
                first_connection, clock=lambda: 1000,
            )
            first.register_source(HELPERS.expectation(publication))
            verified = HELPERS.request(publication, self.publication_authority)
            receipt = first.commit(verified)
            first_connection.close()

            second_connection = sqlite3.connect(path)
            second = STORE.DurableCMSReceiverStore(
                second_connection, clock=lambda: 1001,
            )
            replay = second.commit(verified)
            active = second.read_active_bundle(HELPERS.expectation(publication))
            second_connection.close()

        self.assertEqual(replay, receipt)
        self.assertEqual(active, publication)

    def test_schema_drift_blocks_restart(self):
        self.connection.execute(
            "ALTER TABLE cms_receiver_sources ADD COLUMN unexpected TEXT"
        )
        self.connection.commit()

        with self.assertRaises(STORE.CMSReceiverStoreBlocked):
            STORE.DurableCMSReceiverStore(self.connection, clock=lambda: 1000)


if __name__ == "__main__":
    unittest.main()
