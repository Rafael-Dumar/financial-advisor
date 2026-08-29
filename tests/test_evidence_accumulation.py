import hashlib
import unittest
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from advisor.evidence_schema import (
    canonical_content_sha256,
    canonical_json_bytes,
    classify_idempotency,
    decompress_single_member_gzip,
    deterministic_gzip,
    payload_sha256,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)


class CanonicalSerializationTests(unittest.TestCase):
    def test_canonical_json_is_deterministic_for_key_order(self):
        first = {
            "z": "ação",
            "a": {"β": 1, "α": 2},
            "list": [3, 2],
        }
        second = {
            "list": [3, 2],
            "a": {"α": 2, "β": 1},
            "z": "ação",
        }

        expected = '{"a":{"α":2,"β":1},"list":[3,2],"z":"ação"}'.encode(
            "utf-8"
        )
        self.assertEqual(canonical_json_bytes(first), expected)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))
        self.assertNotIn(b"\n", expected)

    def test_strict_json_loader_rejects_duplicate_raw_keys(self):
        raw_json = b'{"symbol":"AAPL","symbol":"NVDA"}'

        with self.assertRaises(ValueError):
            strict_json_loads_bytes(raw_json)

    def test_strict_json_loader_rejects_malformed_utf8_and_json(self):
        for raw_json in (b"\xff", b'{"symbol":'):
            with self.subTest(raw_json=raw_json):
                with self.assertRaises(ValueError):
                    strict_json_loads_bytes(raw_json)

    def test_strict_json_loader_rejects_nonfinite_constants(self):
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                with self.assertRaises(ValueError):
                    strict_json_loads_bytes(b'{"value":' + constant + b"}")

    def test_canonical_json_rejects_nonfinite_numbers(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    canonical_json_bytes({"value": number})

        unsupported_values = (
            Decimal("1.0"),
            datetime(2026, 8, 29, tzinfo=timezone.utc),
            Path("payload.json"),
            {"unsupported-set-value"},
        )
        for value in unsupported_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises((TypeError, ValueError)):
                    canonical_json_bytes({"value": value})

    def test_deterministic_gzip_is_byte_identical_and_single_member(self):
        json_bytes = b'{"message":"a\xc3\xa7\xc3\xa3o","value":1}'

        compressed = deterministic_gzip(json_bytes)
        self.assertEqual(compressed, deterministic_gzip(json_bytes))
        self.assertEqual(
            compressed[:10], b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"
        )

        reader = zlib.decompressobj(wbits=31)
        self.assertEqual(reader.decompress(compressed) + reader.flush(), json_bytes)
        self.assertTrue(reader.eof)
        self.assertEqual(reader.unused_data, b"")
        self.assertEqual(
            decompress_single_member_gzip(
                compressed, max_uncompressed_bytes=len(json_bytes)
            ),
            json_bytes,
        )

    def test_malformed_gzip_multi_member_and_oversized_input_are_rejected(self):
        json_bytes = b'{"a":1}'
        compressed = deterministic_gzip(json_bytes)

        invalid_gzip_values = (
            b"not-gzip",
            compressed + b"trailing",
            compressed + compressed,
        )
        for invalid_gzip in invalid_gzip_values:
            with self.subTest(invalid_gzip=invalid_gzip):
                with self.assertRaises(ValueError):
                    decompress_single_member_gzip(
                        invalid_gzip, max_uncompressed_bytes=len(json_bytes)
                    )

        corrupt_crc = bytearray(compressed)
        corrupt_crc[-8] ^= 0x01
        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                bytes(corrupt_crc), max_uncompressed_bytes=len(json_bytes)
            )

        corrupt_size = bytearray(compressed)
        corrupt_size[-4] ^= 0x01
        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                bytes(corrupt_size), max_uncompressed_bytes=len(json_bytes)
            )

        with self.assertRaises(ValueError):
            decompress_single_member_gzip(
                compressed, max_uncompressed_bytes=len(json_bytes) - 1
            )

    def test_validate_canonical_envelope_recomputes_hashes_and_rejects_incoherence(
        self,
    ):
        evidence_type = "observation"
        schema_version = "1.0"
        logical_identity = {"signal_id": "signal-1"}
        payload = {"symbol": "AAPL", "close": 123.45}
        semantic_provenance = {"price_provider": "fmp"}
        envelope = {
            "canonical_content_sha256": canonical_content_sha256(
                evidence_type=evidence_type,
                schema_version=schema_version,
                logical_identity=logical_identity,
                payload=payload,
                semantic_provenance=semantic_provenance,
            ),
            "evidence_type": evidence_type,
            "logical_identity": logical_identity,
            "payload": payload,
            "payload_sha256": payload_sha256(payload),
            "schema_version": schema_version,
            "semantic_provenance": semantic_provenance,
            "transport": {"artifact": "run-1", "attempt": 1},
        }

        self.assertIsNone(validate_canonical_envelope(envelope))

        bad_payload_hash = dict(envelope)
        bad_payload_hash["payload_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            validate_canonical_envelope(bad_payload_hash)

        bad_content_hash = dict(envelope)
        bad_content_hash["canonical_content_sha256"] = "1" * 64
        with self.assertRaises(ValueError):
            validate_canonical_envelope(bad_content_hash)

        bad_identity = dict(envelope)
        bad_identity["logical_identity"] = "signal-1"
        with self.assertRaises((TypeError, ValueError)):
            validate_canonical_envelope(bad_identity)


class CanonicalIdempotencyTests(unittest.TestCase):
    def _content_hash(self, *, payload, semantic_provenance):
        return canonical_content_sha256(
            evidence_type="market_bar",
            schema_version="1.0",
            logical_identity={"symbol": "AAPL", "date": "2026-08-28"},
            payload=payload,
            semantic_provenance=semantic_provenance,
        )

    def _envelope(self, *, canonical_hash, payload_sha="payload-hash"):
        return {
            "logical_identity": {"symbol": "AAPL", "date": "2026-08-28"},
            "canonical_content_sha256": canonical_hash,
            "payload_sha256": payload_sha,
        }

    def test_same_logical_identity_same_canonical_content_is_duplicate_same(self):
        canonical_hash = self._content_hash(
            payload={"open": 100.0, "close": 101.0},
            semantic_provenance={"price_provider": "fmp"},
        )

        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash),
                self._envelope(canonical_hash=canonical_hash),
            ),
            "duplicate_same",
        )

    def test_same_logical_identity_different_canonical_content_is_conflict(self):
        canonical_hash_a = self._content_hash(
            payload={"open": 100.0, "close": 101.0},
            semantic_provenance={"price_provider": "fmp"},
        )
        canonical_hash_b = self._content_hash(
            payload={"open": 100.0, "close": 102.0},
            semantic_provenance={"price_provider": "fmp"},
        )

        self.assertNotEqual(canonical_hash_a, canonical_hash_b)
        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash_a),
                self._envelope(canonical_hash=canonical_hash_b),
            ),
            "conflict",
        )

    def test_same_ohlc_different_semantic_provenance_is_conflict(self):
        payload = {"open": 100.0, "high": 103.0, "low": 99.0, "close": 101.0}
        canonical_hash_fmp = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "fmp"},
        )
        canonical_hash_yahoo = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "yahoo"},
        )

        self.assertNotEqual(canonical_hash_fmp, canonical_hash_yahoo)
        self.assertEqual(
            classify_idempotency(
                self._envelope(canonical_hash=canonical_hash_fmp),
                self._envelope(canonical_hash=canonical_hash_yahoo),
            ),
            "conflict",
        )

    def test_payload_sha256_is_not_idempotency_authority(self):
        payload = {"close": 101.0}
        payload_hash = payload_sha256(payload)
        self.assertEqual(
            payload_hash,
            hashlib.sha256(b'{"close":101.0}').hexdigest(),
        )

        canonical_hash = self._content_hash(
            payload=payload,
            semantic_provenance={"price_provider": "fmp"},
        )
        self.assertEqual(
            classify_idempotency(
                self._envelope(
                    canonical_hash=canonical_hash, payload_sha=payload_hash
                ),
                self._envelope(
                    canonical_hash=canonical_hash, payload_sha="different-payload-hash"
                ),
            ),
            "duplicate_same",
        )

    def test_transport_metadata_does_not_change_canonical_content_hash(self):
        semantic_fields = {
            "evidence_type": "observation",
            "schema_version": "1.0",
            "logical_identity": {"signal_id": "signal-1"},
            "payload": {"symbol": "AAPL", "close": 123.45},
            "semantic_provenance": {"price_provider": "fmp"},
        }
        transport_a = {"artifact": "run-1", "attempt": 1}
        transport_b = {"artifact": "run-2", "attempt": 2}
        self.assertNotEqual(transport_a, transport_b)

        expected = hashlib.sha256(
            b'{"evidence_type":"observation","logical_identity":{"signal_id":"signal-1"},"payload":{"close":123.45,"symbol":"AAPL"},"schema_version":"1.0","semantic_provenance":{"price_provider":"fmp"}}'
        ).hexdigest()
        self.assertEqual(
            canonical_content_sha256(**semantic_fields),
            expected,
        )
        self.assertEqual(
            canonical_content_sha256(**semantic_fields),
            canonical_content_sha256(**semantic_fields),
        )


if __name__ == "__main__":
    unittest.main()
