import hashlib
import subprocess
import tempfile
import threading
import time
import unittest
import zlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import get_args

from advisor.evidence_schema import (
    ArchiveStatus,
    CorporateActionReasonCode,
    CryptoPolicy,
    HorizonStatus,
    SignalBasisStatus,
    SplitPolicy,
    canonical_content_sha256,
    canonical_json_bytes,
    classify_idempotency,
    decompress_single_member_gzip,
    deterministic_gzip,
    payload_sha256,
    strict_json_loads_bytes,
    validate_canonical_envelope,
)
from advisor.evidence_archive import (
    EvidenceArchive,
    bootstrap_evidence_branch,
    oldest_canonical_provider_by_symbol,
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


class SharedEvidenceTypeContractTests(unittest.TestCase):
    def test_shared_type_aliases_match_approved_plan(self):
        self.assertEqual(
            frozenset(get_args(ArchiveStatus)),
            frozenset(
                {
                    "committed",
                    "no_op",
                    "conflict",
                    "rejected",
                    "evidence_branch_missing",
                }
            ),
        )
        self.assertEqual(
            frozenset(get_args(HorizonStatus)),
            frozenset(
                {
                    "market_data_unavailable",
                    "pending",
                    "conflict",
                    "signal_basis_unavailable",
                    "feed_unavailable",
                    "split_in_horizon_unavailable",
                    "verified_none",
                    "not_applicable",
                }
            ),
        )
        self.assertEqual(
            frozenset(get_args(CorporateActionReasonCode)),
            frozenset({"corporate_action_revision_conflict"}),
        )
        self.assertEqual(
            frozenset(get_args(SignalBasisStatus)),
            frozenset({"verified_raw_ohlcv", "signal_basis_unavailable"}),
        )
        self.assertEqual(
            frozenset(get_args(SplitPolicy)),
            frozenset({"verified_no_split_in_signal_horizon_v1"}),
        )
        self.assertEqual(
            frozenset(get_args(CryptoPolicy)),
            frozenset({"not_applicable_crypto_raw_ohlcv_v1"}),
        )


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


def _run_git(cwd: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _git_bytes(cwd: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    ).stdout


def _market_envelope(
    *,
    symbol: str = "AAPL",
    market_date: str = "2026-08-26",
    close: float = 100.0,
    provider: str = "fmp",
) -> dict[str, object]:
    logical_identity = {
        "asset_type": "stock",
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": "America/New_York",
        "schema_version": "1.0",
        "symbol": symbol,
    }
    payload = {
        "asset_type": "stock",
        "interval": "1d",
        "market_date": market_date,
        "market_timezone": "America/New_York",
        "ohlcv": {
            "close": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "open": close - 0.5,
            "volume": 123456,
        },
        "price_basis": "raw_ohlcv",
        "session_close_type": "regular",
        "session_status": "complete",
    }
    semantic_provenance = {
        "collection_policy_version": "1.0",
        "price_provider": provider,
        "source_response_sha256": "0" * 64,
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="market_bar",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "market_bar",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


def _corporate_action_envelope(
    *,
    symbol: str = "AAPL",
    provider: str = "alpha_vantage",
    coverage_start: str = "2020-01-01",
    coverage_end: str = "2026-08-26",
    events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    normalized_events = events if events is not None else [
        {
            "effective_date": "2025-01-15",
            "split_factor_raw": "2.0000",
            "split_ratio": {"new_shares": "2", "old_shares": "1"},
        }
    ]
    logical_identity = {
        "asset_type": "stock",
        "corporate_action_provider": provider,
        "coverage_end_date": coverage_end,
        "coverage_start_date": coverage_start,
        "function": "SPLITS",
        "schema_version": "1.0",
        "symbol": symbol,
    }
    payload = {
        "data": [],
        "normalized_events": normalized_events,
        "symbol": symbol,
    }
    semantic_provenance = {
        "corporate_action_provider": provider,
        "response_sha256": "1" * 64,
        "status": "ok",
    }
    return {
        "canonical_content_sha256": canonical_content_sha256(
            evidence_type="corporate_action",
            schema_version="1.0",
            logical_identity=logical_identity,
            payload=payload,
            semantic_provenance=semantic_provenance,
        ),
        "evidence_type": "corporate_action",
        "logical_identity": logical_identity,
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "schema_version": "1.0",
        "semantic_provenance": semantic_provenance,
    }


class _ArchiveRepositoryMixin:
    branch_name = "advisor-evidence"

    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.remote = self.root / "origin.git"
        self.caller = self.root / "caller"

        _run_git(self.root, "init", "--bare", str(self.remote))
        _run_git(self.root, "init", str(self.caller))
        _run_git(self.caller, "config", "user.name", "Task 2 Test")
        _run_git(self.caller, "config", "user.email", "task2@example.invalid")
        (self.caller / "README.md").write_bytes(b"caller tracked bytes\n")
        _run_git(self.caller, "add", "README.md")
        _run_git(self.caller, "commit", "-m", "initial main")
        _run_git(self.caller, "branch", "-M", "main")
        _run_git(self.caller, "remote", "add", "origin", str(self.remote))
        _run_git(self.caller, "push", "-u", "origin", "main")
        _run_git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.caller / "caller-untracked.bin").write_bytes(b"untracked caller bytes")
        self._transport_number = 0

    def tearDown(self):
        self._temporary_directory.cleanup()

    def _bootstrap(self) -> str:
        return bootstrap_evidence_branch(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        )

    def _write_transport(
        self,
        envelopes: list[dict[str, object]],
        *,
        names: list[str] | None = None,
    ) -> Path:
        self._transport_number += 1
        transport_dir = self.root / f"transport-{self._transport_number}"
        transport_dir.mkdir()
        file_names = names or [
            f"shard-{index}.json.gz" for index in range(len(envelopes))
        ]
        self.assertEqual(len(file_names), len(envelopes))
        for name, envelope in zip(file_names, envelopes):
            path = transport_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(deterministic_gzip(canonical_json_bytes(envelope)))
        return transport_dir

    def _archive(self, envelopes: list[dict[str, object]], **kwargs):
        transport_dir = self._write_transport(envelopes, **kwargs)
        return EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport_dir)

    def _remote_tree(self) -> list[str]:
        output = _run_git(
            self.remote,
            "ls-tree",
            "-r",
            "--name-only",
            self.branch_name,
        ).stdout
        return [line for line in output.splitlines() if line]

    def _remote_commit_count(self) -> int:
        return int(
            _run_git(
                self.remote,
                "rev-list",
                "--count",
                self.branch_name,
            ).stdout.strip()
        )

    def _remote_file(self, path: str) -> bytes:
        return _git_bytes(self.remote, "show", f"{self.branch_name}:{path}")

    def _clone_branch(self, destination: Path) -> Path:
        _run_git(
            self.root,
            "clone",
            "--branch",
            self.branch_name,
            "--single-branch",
            str(self.remote),
            str(destination),
        )
        return destination


class ArchiveGitTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_branch_bootstrap_is_orphan_and_evidence_only(self):
        root_sha = self._bootstrap()

        self.assertEqual(len(root_sha), 40)
        self.assertEqual(
            _run_git(self.remote, "rev-parse", self.branch_name).stdout.strip(),
            root_sha,
        )
        self.assertEqual(
            self._remote_tree(),
            ["evidence/branch-schema.json"],
        )
        schema = strict_json_loads_bytes(
            self._remote_file("evidence/branch-schema.json")
        )
        self.assertEqual(
            schema,
            {
                "branch_name": self.branch_name,
                "financial_evidence_count": 0,
                "schema_version": "1.0",
            },
        )
        parents = _run_git(
            self.remote,
            "rev-list",
            "--parents",
            "-n",
            "1",
            self.branch_name,
        ).stdout.split()
        self.assertEqual(parents, [root_sha])
        merge_base = _run_git(
            self.remote,
            "merge-base",
            self.branch_name,
            "main",
            check=False,
        )
        self.assertNotEqual(merge_base.returncode, 0)

    def test_bootstrap_is_one_time_and_refuses_recreation(self):
        first_root = self._bootstrap()
        before_tree = self._remote_tree()

        with self.assertRaises((ValueError, RuntimeError)):
            self._bootstrap()

        self.assertEqual(
            _run_git(self.remote, "rev-parse", self.branch_name).stdout.strip(),
            first_root,
        )
        self.assertEqual(self._remote_tree(), before_tree)

    def test_missing_evidence_branch_returns_evidence_branch_missing_without_auto_create(
        self,
    ):
        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(
            self._write_transport([_market_envelope()])
        )

        self.assertEqual(result.status, "evidence_branch_missing")
        self.assertFalse(result.durability_confirmed)
        self.assertNotEqual(
            _run_git(
                self.remote,
                "show-ref",
                "--verify",
                f"refs/heads/{self.branch_name}",
                check=False,
            ).returncode,
            0,
        )

    def test_archive_validates_full_batch_before_one_commit(self):
        self._bootstrap()
        before_count = self._remote_commit_count()
        transport = self._write_transport([_market_envelope()])
        (transport / "invalid.json.gz").write_bytes(b"not gzip")

        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport)

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(self._remote_commit_count(), before_count)
        self.assertEqual(self._remote_tree(), ["evidence/branch-schema.json"])

    def test_archive_never_leaves_partial_canonical_batch(self):
        self._bootstrap()
        before_tree = self._remote_tree()
        valid = _market_envelope(symbol="AAPL")
        invalid = _market_envelope(symbol="MSFT")
        invalid["payload_sha256"] = "f" * 64
        transport = self._write_transport([valid, invalid])

        result = EvidenceArchive(
            repo_dir=self.caller,
            branch_name=self.branch_name,
        ).archive(transport)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(self._remote_tree(), before_tree)
        self.assertFalse(any(path.startswith("evidence/manifests/") for path in self._remote_tree()))
        self.assertFalse(any(path.startswith("evidence/market-bars/") for path in self._remote_tree()))

    def test_identical_batch_retry_returns_no_op_without_second_manifest(self):
        self._bootstrap()
        first = self._archive(
            [
                _market_envelope(symbol="AAPL", market_date="2026-08-26"),
                _market_envelope(symbol="MSFT", market_date="2026-08-27"),
            ],
            names=["z.json.gz", "a.json.gz"],
        )
        commits_before_retry = self._remote_commit_count()
        manifests_before = [
            path for path in self._remote_tree() if path.startswith("evidence/manifests/")
        ]

        second = self._archive(
            [
                _market_envelope(symbol="MSFT", market_date="2026-08-27"),
                _market_envelope(symbol="AAPL", market_date="2026-08-26"),
            ],
            names=["different-name-1.json.gz", "different-name-2.json.gz"],
        )

        self.assertEqual(first.status, "committed")
        self.assertEqual(second.status, "no_op")
        self.assertTrue(second.durability_confirmed)
        self.assertEqual(second.manifest_path, first.manifest_path)
        self.assertEqual(self._remote_commit_count(), commits_before_retry)
        self.assertEqual(
            [
                path
                for path in self._remote_tree()
                if path.startswith("evidence/manifests/")
            ],
            manifests_before,
        )

    def test_no_op_is_operational_only_without_historical_record(self):
        self._bootstrap()
        self._archive([_market_envelope()])
        result = self._archive([_market_envelope()])

        self.assertEqual(result.status, "no_op")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(
            [
                path
                for path in self._remote_tree()
                if "no_op" in path.lower()
            ],
            [],
        )
        log = _run_git(self.remote, "log", "--format=%B", self.branch_name).stdout
        self.assertNotIn("no_op", log.lower())

    def test_identical_retry_does_not_overwrite_committed_manifest(self):
        self._bootstrap()
        first = self._archive([_market_envelope()])
        self.assertIsNotNone(first.manifest_path)
        before = self._remote_file(first.manifest_path)

        retry = self._archive(
            [
                {
                    **_market_envelope(),
                    "transport": {
                        "artifact": "retry-artifact",
                        "attempt": 99,
                    },
                }
            ]
        )

        self.assertEqual(retry.status, "no_op")
        self.assertEqual(self._remote_file(first.manifest_path), before)
        self.assertEqual(
            len([path for path in self._remote_tree() if path.startswith("evidence/manifests/")]),
            1,
        )

    def test_conflict_transaction_identity_is_derived_from_conflict_entries(self):
        self._bootstrap()
        self._archive(
            [
                _market_envelope(symbol="AAPL", close=100.0),
                _market_envelope(symbol="MSFT", close=200.0),
            ]
        )
        incoming = [
            _market_envelope(symbol="AAPL", close=101.0),
            _market_envelope(symbol="MSFT", close=201.0),
        ]

        first = self._archive(incoming, names=["one.json.gz", "two.json.gz"])
        commits_after_first = self._remote_commit_count()
        second = self._archive(
            list(reversed(incoming)),
            names=["reordered-a.json.gz", "reordered-b.json.gz"],
        )

        self.assertEqual(first.status, "conflict")
        self.assertEqual(second.status, "conflict")
        self.assertFalse(first.durability_confirmed)
        self.assertFalse(second.durability_confirmed)
        self.assertEqual(set(first.conflict_paths), set(second.conflict_paths))
        self.assertEqual(self._remote_commit_count(), commits_after_first)
        self.assertEqual(
            len([path for path in self._remote_tree() if path.startswith("evidence/conflicts/")]),
            len(first.conflict_paths),
        )

    def test_divergent_same_identity_writes_conflict_only_transaction(self):
        self._bootstrap()
        original = _market_envelope(symbol="AAPL", close=100.0)
        self._archive([original])
        canonical_path = next(
            path for path in self._remote_tree() if path.startswith("evidence/market-bars/")
        )
        original_bytes = self._remote_file(canonical_path)
        before_market_paths = {
            path for path in self._remote_tree() if path.startswith("evidence/market-bars/")
        }

        result = self._archive([_market_envelope(symbol="AAPL", close=101.0)])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertTrue(result.conflict_paths)
        self.assertTrue(all(path.startswith("evidence/conflicts/") for path in result.conflict_paths))
        self.assertEqual(self._remote_file(canonical_path), original_bytes)
        self.assertEqual(
            {path for path in self._remote_tree() if path.startswith("evidence/market-bars/")},
            before_market_paths,
        )
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(conflict["reason_code"], "divergent_payload")

    def test_push_retries_fast_forward_at_most_three_times_without_force_or_merge(self):
        self._bootstrap()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "archive_count_file=\"$GIT_DIR/task2-archive-push-count\"\n"
            "archive_count=0\n"
            "read old_sha new_sha ref_name\n"
            "subject=\"$(git log -1 --format=%s \"$new_sha\")\"\n"
            "if test \"$subject\" = \"archive evidence batch\"; then\n"
            "  if test -f \"$archive_count_file\"; then archive_count=$(cat \"$archive_count_file\"); fi\n"
            "  archive_count=$((archive_count + 1))\n"
            "  printf '%s' \"$archive_count\" > \"$archive_count_file\"\n"
            "  if test \"$archive_count\" -eq 1; then\n"
            "    : > \"$GIT_DIR/task2-race-triggered\"\n"
            "    sleep 1\n"
            "  fi\n"
            "  if test \"$archive_count\" -le 3; then exit 1; fi\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )

        race_clone = self.root / "race-clone"
        _run_git(
            self.root,
            "clone",
            "--branch",
            self.branch_name,
            "--single-branch",
            str(self.remote),
            str(race_clone),
        )
        _run_git(race_clone, "config", "user.name", "Race Writer")
        _run_git(race_clone, "config", "user.email", "race@example.invalid")

        def advance_remote_after_rejection_request():
            deadline = time.monotonic() + 5.0
            trigger = self.remote / "task2-race-triggered"
            while time.monotonic() < deadline and not trigger.exists():
                time.sleep(0.01)
            if not trigger.exists():
                return
            _run_git(race_clone, "commit", "--allow-empty", "-m", "concurrent writer")
            _run_git(race_clone, "push", "origin", f"HEAD:{self.branch_name}")

        race_thread = threading.Thread(target=advance_remote_after_rejection_request)
        race_thread.start()
        try:
            result = self._archive([_market_envelope()])
        finally:
            race_thread.join(timeout=10)

        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(result.error_code, "push_race_exhausted")
        self.assertEqual(
            int((self.remote / "task2-archive-push-count").read_text(encoding="utf-8")),
            3,
        )
        self.assertEqual(
            self._remote_tree(),
            ["evidence/branch-schema.json"],
        )
        commits = _run_git(
            self.remote,
            "rev-list",
            "--parents",
            self.branch_name,
        ).stdout.splitlines()
        self.assertTrue(all(len(line.split()) <= 2 for line in commits))
        self.assertNotEqual(
            _run_git(
                self.remote,
                "merge-base",
                self.branch_name,
                "main",
                check=False,
            ).returncode,
            0,
        )


class GitIsolationTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_bootstrap_archive_in_dedicated_clone_leaves_main_head_worktree_index_unchanged(
        self,
    ):
        tracked_paths = [
            path
            for path in _run_git(self.caller, "ls-files").stdout.splitlines()
            if path
        ]
        before = {
            "head": _run_git(self.caller, "rev-parse", "HEAD").stdout.strip(),
            "branch": _run_git(self.caller, "branch", "--show-current").stdout.strip(),
            "index": _run_git(self.caller, "ls-files", "-s").stdout,
            "status": _run_git(self.caller, "status", "--porcelain").stdout,
            "tracked": {path: (self.caller / path).read_bytes() for path in tracked_paths},
            "untracked": (self.caller / "caller-untracked.bin").read_bytes(),
        }

        self._bootstrap()
        self._archive([_market_envelope()])

        after = {
            "head": _run_git(self.caller, "rev-parse", "HEAD").stdout.strip(),
            "branch": _run_git(self.caller, "branch", "--show-current").stdout.strip(),
            "index": _run_git(self.caller, "ls-files", "-s").stdout,
            "status": _run_git(self.caller, "status", "--porcelain").stdout,
            "tracked": {path: (self.caller / path).read_bytes() for path in tracked_paths},
            "untracked": (self.caller / "caller-untracked.bin").read_bytes(),
        }
        self.assertEqual(after, before)


class CorporateArchiveConflictTests(_ArchiveRepositoryMixin, unittest.TestCase):
    def test_existing_canonical_overlap_revision_creates_conflict_transaction(self):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        canonical_path = next(
            path
            for path in self._remote_tree()
            if path.startswith("evidence/corporate-actions/")
        )
        before = self._remote_file(canonical_path)
        incoming = _corporate_action_envelope(
            coverage_start="2024-01-01",
            coverage_end="2027-01-01",
            events=[
                {
                    "effective_date": "2025-01-15",
                    "split_factor_raw": "3.0000",
                    "split_ratio": {"new_shares": "3", "old_shares": "1"},
                }
            ],
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "conflict")
        self.assertFalse(result.durability_confirmed)
        self.assertEqual(self._remote_file(canonical_path), before)
        conflict = strict_json_loads_bytes(
            decompress_single_member_gzip(
                self._remote_file(result.conflict_paths[0]),
                max_uncompressed_bytes=1024 * 1024,
            )
        )
        self.assertEqual(
            conflict["reason_code"],
            "corporate_action_revision_conflict",
        )

    def test_equal_overlap_against_canonical_evidence_is_accepted(self):
        self._bootstrap()
        existing = _corporate_action_envelope()
        self._archive([existing])
        incoming = _corporate_action_envelope(
            coverage_start="2024-01-01",
            coverage_end="2027-01-01",
        )

        result = self._archive([incoming])

        self.assertEqual(result.status, "committed")
        self.assertTrue(result.durability_confirmed)
        self.assertEqual(result.conflict_paths, ())
        self.assertEqual(
            len(
                [
                    path
                    for path in self._remote_tree()
                    if path.startswith("evidence/corporate-actions/")
                ]
            ),
            2,
        )


def _write_market_checkout_shard(
    checkout: Path,
    envelope: dict[str, object],
) -> Path:
    identity = envelope["logical_identity"]
    assert isinstance(identity, dict)
    market_date = identity["market_date"]
    assert isinstance(market_date, str)
    identity_sha = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    path = (
        checkout
        / "evidence"
        / "market-bars"
        / market_date[:4]
        / market_date[5:7]
        / market_date[8:10]
        / f"{identity_sha}.json.gz"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(deterministic_gzip(canonical_json_bytes(envelope)))
    return path


class CanonicalProviderReaderTests(unittest.TestCase):
    def test_oldest_canonical_provider_by_symbol_returns_oldest_provider(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-26",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-27",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="MSFT",
                    market_date="2026-08-26",
                    provider="yahoo",
                ),
            )

            result = oldest_canonical_provider_by_symbol(
                evidence_checkout=checkout,
                symbols=["AAPL", "MISSING"],
            )

            self.assertEqual(result, {"AAPL": "fmp"})

    def test_oldest_canonical_provider_by_symbol_rejects_mixed_provider_series(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory)
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-26",
                    provider="fmp",
                ),
            )
            _write_market_checkout_shard(
                checkout,
                _market_envelope(
                    symbol="AAPL",
                    market_date="2026-08-27",
                    provider="yahoo",
                ),
            )

            with self.assertRaisesRegex(ValueError, "mixed_price_provider"):
                oldest_canonical_provider_by_symbol(
                    evidence_checkout=checkout,
                    symbols=["AAPL"],
                )


if __name__ == "__main__":
    unittest.main()
