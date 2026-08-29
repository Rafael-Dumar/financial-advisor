# Canonical Evidence Accumulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved canonical evidence accumulation boundary so that reports emit observation sidecars, assigned-provider collectors produce validated local transport, an orphan `advisor-evidence` branch becomes the sole durable authority, and materialization can produce split/basis proofs and frozen 3B.2 outcomes only after archive durability is confirmed. The implementation must preserve historical decisions when databases, runners, caches, workspaces, or transport artifacts disappear.

**Architecture:** Keep report generation and the frozen signal/outcome contracts intact. Add four narrowly separated components: `evidence_schema` owns canonical serialization, hashes, envelopes, and validation; `evidence_archive` owns staging, append-only Git publication, manifest/conflict semantics, and branch enforcement; `evidence_collector` owns deterministic provider assignment and local market/corporate-action transport; `evidence_materializer` reads only a freshly fetched canonical branch, qualifies horizons, calls the frozen 3B.2 evaluator, and emits proof/outcome transport. `cli.py` provides thin command dispatch and report-sidecar wiring. GitHub Actions jobs are separated by provider-secret and repository-write permissions.

**Tech Stack:** Python 3.12, existing `unittest` test discovery, `decimal.Decimal` for split ratios, standard-library JSON/Gzip/Hashlib/Pathlib/TemporaryDirectory/Subprocess APIs, the existing frozen signal observation and forward outcome modules, SQLite only as a rebuild target, and local temporary Git repositories for archive and recovery tests. No real provider calls or real GitHub repository writes are part of the deterministic test suite.

**Spec:** `docs/superpowers/specs/2026-08-27-canonical-evidence-accumulation-design.md`

## Global Constraints

- This plan is the implementation plan for approved Phase 3B.3.3 only. It does not reopen the design, create `advisor-evidence`, create a branch, alter workflows now, call providers now, or start implementation/TDD now.
- The authoritative source is `main + advisor-evidence`. SQLite, GitHub Actions caches, runners, local workspaces, and transport files are operational surfaces and never canonical authority.
- Losing:
  - `data/advisor.db`;
  - all GitHub Actions caches;
  - runners;
  - local workspaces;
  - transport artifacts;

  cannot destroy canonical history.
- Using only `main + advisor-evidence` must reconstruct:
  - observations;
  - market evidence;
  - corporate-action evidence;
  - horizon proofs;
  - outcomes;
  - pending maturation state;

  without recalculating old decisions.
- The canonical manifest exists at most once for each `batch_identity` and manifest path. The first valid commit writes it. An identical retry detects the committed manifest and shards, returns operational `no_op`, writes no second manifest, and never changes the committed manifest. A conflict transaction has its own identity derived from its conflict entries.
- Canonical content is logical identity plus payload plus semantic provenance plus schema/evidence type. Transport metadata is excluded from `canonical_content_sha256`. `payload_sha256` is diagnostic only and is never the idempotency authority.
- Overlapping corporate-action snapshots for the same provider and symbol must normalize to identical events. A differing presence/absence, effective date, or split ratio creates `corporate_action_revision_conflict`; a relevant existing conflict blocks a new `verified_none` proof, while frozen outcomes are not rewritten.
- The signal price basis remains a sidecar keyed by `signal_id + observation_hash`; `SignalObservation` is not changed. In v1, stock/ETF maturation accepts only `verified_raw_ohlcv`. Missing proof is `signal_basis_unavailable` and prevents delivery of that stock horizon to 3B.2.
- Price assignment is deterministic and versioned as `price_provider_assignment_v1`: stock and ETF use FMP, HYPE uses Hyperliquid, and other supported crypto uses Binance. Existing series use the oldest canonical provider and remain sticky. An unavailable assigned provider returns `market_data_unavailable`; it never silently switches to a first-success provider.
- The split policy is exactly `verified_no_split_in_signal_horizon_v1` over inclusive `[signal_market_date, horizon_end_date]`. Feed unavailability is never `verified_none`.
- Maturation is strictly `collector -> local transport -> validation -> first archive -> push confirmed -> fresh read/fetch -> materializer -> horizon proofs/outcomes -> second archive`. Uncommitted transport cannot support a proof or outcome. First archive failure prevents maturation.
- `advisor/scoring.py`, `advisor/risk.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `advisor/predictive_evaluation.py`, and `advisor/predictive_statistics.py` are protected read-only modules. If implementation requires a change to any of them, mark `DESIGN_CONFLICT` and stop; do not introduce a hidden workaround.
- Out of scope: calibration; scoring/risk changes; Telegram; broker integration; dashboards; legacy `signal_journal` backfill; automatic 3B.3.1; automatic 3B.3.2; and Phase 3B.4.
- Repository branch protection is an operational recommendation, not a unit-test prerequisite. The writer itself must enforce fast-forward-only, no-force, no-merge-hiding-conflict, and bounded-push behavior without an administrative GitHub API call.
- No mutation is committed. Only mutations already registered in the approved spec are used, each in an isolated temporary copy, with `mutations_survived=0` required.

## File Map

### Files to create

| File | Responsibility and concrete consumer |
|---|---|
| `advisor/evidence_schema.py` | Canonical evidence types, schema/evidence-type versions, logical identities, canonical JSON, deterministic gzip, canonical/payload hashes, envelope validation, path partitioning, and duplicate/nonfinite/hash checks. Consumed by the archive, collector, materializer, and CLI sidecar writer. It performs no network, Git, SQLite, scoring, or outcome evaluation. |
| `advisor/evidence_archive.py` | Validates local transport, stages a complete batch, bootstraps or verifies an orphan `advisor-evidence` branch only through an explicit deployment operation, publishes one atomic Git commit, classifies `committed`/`no_op`/`conflict`, writes conflict-only transactions, and performs at most three non-force push attempts. Consumed by CLI and the evidence workflow. |
| `advisor/evidence_collector.py` | Implements `price_provider_assignment_v1`, assigned-provider market collection, Alpha Vantage `SPLITS` fixture-compatible normalization using `Decimal`, coverage windows, session-date handling, and local transport output. It never invokes the report loader’s dynamic fallback chain. Consumed by the collector job and materializer tests. |
| `advisor/evidence_materializer.py` | Reads a freshly fetched canonical evidence checkout only, reconstructs observations/market/corporate shards, applies basis/split/conflict/feed gates, calls `evaluate_signal_observation` for eligible horizons, and emits proof/outcome transport plus pending maturation state. Consumed by CLI and the publish/mature job. |
| `tests/test_evidence_accumulation.py` | Deterministic unit, property-style, local-Git integration, recovery acceptance, workflow contract, security, and mutation-resistance tests for all new components. Fixtures are in-memory or temporary-directory data. |
| `.github/workflows/financial-advisor-evidence.yml` | Separate collector and publish/mature jobs with the required provider-secret and repository-write boundaries, concurrency group, first-archive/fresh-read/second-archive order, and no provider secrets in writer jobs. |

### Files to modify during implementation of this plan

| File | Narrow change and consumer |
|---|---|
| `advisor/cli.py` | Return the already-built observation list from the existing report persistence helper, write the sidecar from that same in-memory list, and add thin `evidence collect`, `evidence archive`, `evidence materialize`, and `evidence mature` dispatch. Existing report behavior and the frozen `outcomes evaluate` interface remain unchanged. |
| `.github/workflows/financial-advisor-reports.yml` | Preserve the existing report job and provider behavior, upload the observation-sidecar transport with the report output, and add the read-only-to-writer handoff required by the archive job without giving provider secrets to the writer. |
| `docs/AUTOMATION_SETUP.md` | Document the evidence workflow, exact command boundaries, orphan-branch bootstrap operation, permissions, concurrency, fresh-read requirement, and recovery assumptions. |

### Read-only contract anchors

`advisor/cache.py`, `advisor/live_loader.py`, `advisor/data_sources.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `.github/workflows/financial-advisor-nightly-review.yml`, and the four frozen Phase 3B contract documents are inspected inputs. They are not implementation targets in this plan. The existing live report fallback chain remains a regression surface only; it is not reused as evidence authority.

### Protected modules — never list under `Files: Modify`

`advisor/scoring.py`, `advisor/risk.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `advisor/predictive_evaluation.py`, and `advisor/predictive_statistics.py` remain unchanged. Any required change is `DESIGN_CONFLICT` and stops execution.

## Shared Interfaces and Data Contracts

The following names and meanings are fixed before task execution so later tasks do not invent incompatible interfaces.

```python
# advisor/evidence_schema.py
EvidenceStatus = Literal[
    "committed", "no_op", "conflict", "rejected",
    "evidence_branch_missing", "market_data_unavailable",
    "corporate_action_revision_conflict", "signal_basis_unavailable",
    "pending", "split_in_signal_horizon",
    "verified_none", "verified_no_split_in_signal_horizon_v1", "crypto_not_applicable",
]

def canonical_json_bytes(value: object) -> bytes:
    pass
def deterministic_gzip(json_bytes: bytes) -> bytes:
    pass
def decompress_single_member_gzip(data: bytes, *, max_uncompressed_bytes: int) -> bytes:
    pass
def canonical_content_sha256(
    *, evidence_type: str, schema_version: str,
    logical_identity: Mapping[str, object],
    payload: Mapping[str, object],
    semantic_provenance: Mapping[str, object],
) -> str:
    pass
def payload_sha256(payload: object) -> str:
    pass
def validate_canonical_envelope(envelope: Mapping[str, object]) -> None:
    pass
```

`canonical_json_bytes` uses UTF-8, `ensure_ascii=False`, `sort_keys=True`, separators `(",", ":")`, `allow_nan=False`, rejects duplicate object keys, and emits no final newline. Semantic arrays are sorted by the identity-defined order before serialization. `deterministic_gzip` emits one DEFLATE-9 member with `MTIME=0`, `FLG=0`, `XFL=2`, and `OS=255`; decompression rejects trailing bytes, unused data, CRC/size mismatch, multiple members, and the configured uncompressed-size limit.

```python
# advisor/evidence_archive.py
@dataclass(frozen=True)
class ArchiveResult:
    status: Literal["committed", "no_op", "conflict", "rejected", "evidence_branch_missing"]
    batch_identity: str
    manifest_path: str | None
    committed_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    error_code: str | None

class EvidenceArchive:
    def __init__(self, *, repo_dir: Path, branch_name: str = "advisor-evidence", max_push_attempts: int = 3):
        pass
    def archive(self, transport_dir: Path) -> ArchiveResult:
        pass

def bootstrap_evidence_branch(*, repo_dir: Path, branch_name: str = "advisor-evidence") -> str:
    pass
```

`bootstrap_evidence_branch` is an explicit one-time deployment operation. It creates an orphan root containing only `evidence/branch-schema.json`, with the fixed schema/version and zero financial evidence. `EvidenceArchive.archive` never calls it automatically.

```python
# advisor/evidence_collector.py
PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION = "price_provider_assignment_v1"

def assigned_price_provider(*, asset_type: str, symbol: str) -> str | None:
    pass
def normalize_split_ratio(*, numerator: str | Decimal, denominator: str | Decimal) -> str:
    pass

class EvidenceCollector:
    def __init__(self, *, fetch_json: Callable[..., object], transport_root: Path, today: date | None = None):
        pass
    def collect_market(self, *, symbols: Sequence[str], existing_provider_by_symbol: Mapping[str, str]) -> Path:
        pass
    def collect_corporate_actions(self, *, symbols: Sequence[str], coverage_windows: Mapping[str, tuple[date, date]]) -> Path:
        pass
```

The assigned provider function has no success-based fallback. For an existing series, the caller supplies the provider from the oldest canonical evidence record; a mismatch is a provider-assignment conflict, not a provider switch.

```python
# advisor/evidence_materializer.py
@dataclass(frozen=True)
class HorizonQualification:
    status: EvidenceStatus
    proof_record: Mapping[str, object] | None

class EvidenceMaterializer:
    def __init__(self, *, evidence_checkout: Path, db_path: Path):
        pass
    def materialize(self) -> "MaterializationResult":
        pass
    def qualify_horizon(
        self, *, observation: SignalObservation,
        horizon: int, signal_price_basis_status: str,
    ) -> HorizonQualification:
        pass

@dataclass(frozen=True)
class MaterializationResult:
    completed_horizons: tuple[str, ...]
    pending_horizons: tuple[str, ...]
    proof_transport: Path | None
    outcome_transport: Path | None
```

`EvidenceMaterializer` has no transport-path input. It reads canonical shards from `evidence_checkout`, verifies the checkout revision was fetched after the first archive, and uses `SQLiteCache` only to rebuild the operational materialization. It calls `evaluate_signal_observation` exactly once for each eligible signal/horizon evaluation through the frozen 3B.2 contract and never calls scoring or risk.

## Task 1 — Canonical evidence schema and idempotency content

**Reviewer boundary:** A reviewer can approve or reject canonical bytes, gzip bytes, envelope validation, logical identities, and duplicate/conflict classification without reviewing Git publication or collection.

**Files:** Create `advisor/evidence_schema.py` and `tests/test_evidence_accumulation.py` with the `CanonicalSerializationTests` and `CanonicalIdempotencyTests` classes. No protected file is modified.

**Interfaces to implement:** `canonical_json_bytes`, `deterministic_gzip`, `decompress_single_member_gzip`, `canonical_content_sha256`, `payload_sha256`, `validate_canonical_envelope`, and a `classify_idempotency(existing, incoming) -> Literal["duplicate_same", "conflict"]` helper that compares logical identity and `canonical_content_sha256` only.

**TDD sequence:**

- [ ] Write these exact failing tests before implementation:
  - `CanonicalSerializationTests.test_canonical_json_is_deterministic_for_key_order`
  - `CanonicalSerializationTests.test_canonical_json_rejects_duplicate_keys`
  - `CanonicalSerializationTests.test_canonical_json_rejects_nonfinite_numbers`
  - `CanonicalSerializationTests.test_deterministic_gzip_is_byte_identical_and_single_member`
  - `CanonicalSerializationTests.test_malformed_gzip_multi_member_and_oversized_input_are_rejected`
  - `CanonicalIdempotencyTests.test_same_logical_identity_same_canonical_content_is_duplicate_same`
  - `CanonicalIdempotencyTests.test_same_logical_identity_different_canonical_content_is_conflict`
  - `CanonicalIdempotencyTests.test_same_ohlc_different_semantic_provenance_is_conflict`
  - `CanonicalIdempotencyTests.test_payload_sha256_is_not_idempotency_authority`
  - `CanonicalIdempotencyTests.test_transport_metadata_does_not_change_canonical_content_hash`
- [ ] Run the first test as RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests.test_canonical_json_is_deterministic_for_key_order`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_schema'`.
- [ ] Implement the smallest schema module with strict recursive JSON validation, an object-pairs decoder that raises on duplicate keys, explicit finite-number checks, deterministic key/array normalization, and the exact gzip header/trailer/member checks above. The canonical-content input must serialize a structure containing only `evidence_type`, `schema_version`, `logical_identity`, `payload`, and `semantic_provenance`; transport metadata must not be accepted by that hash function.
- [ ] Run the targeted schema tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests`
  Expected result after the implementation: all selected tests pass with zero skips.
- [ ] Run the relevant frozen regression subset:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome`
- [ ] Commit the task result as `feat: add canonical evidence schema` after `git diff --check` reports no errors and only the task files are staged.

**Acceptance:** Reordering object keys produces identical canonical bytes and hashes; duplicate keys, NaN, Infinity, malformed/multi-member/oversized gzip, and hash mismatches fail closed; same identity plus same canonical content is `duplicate_same`; same identity plus a different canonical content hash is `conflict`; identical OHLC payload with different semantic provenance is `conflict`; different transport metadata does not change canonical content.

## Task 2 — Append-only archive, orphan bootstrap, and manifest transactions

**Reviewer boundary:** A reviewer can approve or reject branch shape, staging atomicity, manifest identity, conflict transactions, retry behavior, and push safety using only local temporary Git repositories.

**Files:** Create/extend `advisor/evidence_archive.py` and `tests/test_evidence_accumulation.py`. Do not create `advisor-evidence` in the user repository and do not modify any workflow.

**Interfaces to implement:** `bootstrap_evidence_branch`, `EvidenceArchive`, `ArchiveResult`, and internal reader/writer helpers that validate every staged object before one commit. The archive API must accept a local transport directory and return a result without requiring provider credentials.

**TDD sequence:**

- [ ] Write these exact failing tests using `tempfile.TemporaryDirectory`, `git init`, and subprocess calls against disposable repositories:
  - `ArchiveGitTests.test_branch_bootstrap_is_orphan_and_evidence_only`
  - `ArchiveGitTests.test_missing_evidence_branch_returns_evidence_branch_missing_without_auto_create`
  - `ArchiveGitTests.test_archive_validates_full_batch_before_one_commit`
  - `ArchiveGitTests.test_archive_never_leaves_partial_canonical_batch`
  - `ArchiveGitTests.test_identical_batch_retry_returns_no_op_without_second_manifest`
  - `ArchiveGitTests.test_no_op_is_operational_only_without_historical_record`
  - `ArchiveGitTests.test_identical_retry_does_not_overwrite_committed_manifest`
  - `ArchiveGitTests.test_conflict_transaction_identity_is_derived_from_conflict_entries`
  - `ArchiveGitTests.test_divergent_same_identity_writes_conflict_only_transaction`
  - `ArchiveGitTests.test_push_retries_fast_forward_at_most_three_times_without_force_or_merge`
- [ ] Run the first test as RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests.test_branch_bootstrap_is_orphan_and_evidence_only`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_archive'`.
- [ ] Implement explicit bootstrap as `git checkout --orphan advisor-evidence`, remove the index/worktree contents in the temporary repository, write only `evidence/branch-schema.json`, commit the root, and verify the root has no `main` ancestor and no financial evidence. Runtime archive code must return `evidence_branch_missing` with nonzero CLI status when the branch is absent; it must not invoke bootstrap.
- [ ] Implement transport staging in a separate temporary directory inside the repository, validate all envelopes, paths, gzip members, hashes, identities, batch completeness, and provider assignments before adding any canonical file. Write one deterministic manifest for a valid new batch, stage all shards and the manifest, and create one commit. On any validation error, remove the staging directory and leave the branch unchanged.
- [ ] Derive `batch_identity` from the canonical batch identity, never from a transient run ID. On retry, inspect the existing manifest and all referenced shards before returning operational `no_op`; do not write a second manifest, no `no_op` evidence record, or any rewrite of the committed one. For divergent canonical content, write a conflict-only transaction whose identity is derived from sorted conflict entries and whose paths are under `conflicts/`.
- [ ] Implement fast-forward-only publication with a hard maximum of three push attempts. Re-read/fetch before each retry, reject non-fast-forward/merge-required states, never pass `--force` or `--force-with-lease`, and surface the final failure without altering the canonical branch.
- [ ] Run the targeted archive tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests`
- [ ] Run the evidence schema and frozen append-only regression subset:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests tests.test_signal_observation tests.test_signal_outcome`
- [ ] Commit the task result as `feat: add append-only evidence archive` after checking the staged diff and confirming no branch was created in the real repository.

**Acceptance:** The bootstrap root is orphan/evidence-only; missing runtime branch is a nonzero `evidence_branch_missing`; valid batches have one atomic commit; invalid batches have no canonical partials; identical retries return `no_op` without another manifest; divergence produces an independent conflict transaction; publication is bounded, fast-forward-only, and non-force.

## Task 3 — Observation sidecar and signal-basis integration

**Reviewer boundary:** A reviewer can approve or reject sidecar binding and report integration without changing the `SignalObservation` schema or the report’s existing scoring/risk behavior.

**Files:** Modify `advisor/cli.py`; use the sidecar serialization helpers from `advisor/evidence_schema.py`; extend `tests/test_evidence_accumulation.py`.

The frozen observation contract remains read-only.

**Interfaces to implement:**

```python
def build_observation_sidecar(
    observations: Sequence[SignalObservation],
    *,
    basis_by_observation: Mapping[tuple[str, str], Mapping[str, object]],
    output_path: Path,
) -> Path:
    pass

def resolve_signal_price_basis_status(
    *, observation: SignalObservation,
    sidecar: Mapping[str, object],
) -> Literal["verified_raw_ohlcv", "signal_basis_unavailable"]:
    pass
```

The sidecar key is exactly `(signal_id, observation_hash)`. It carries explicit sanitized semantic provenance, the price-basis status, source snapshot identity, and the sidecar schema version. `verified_raw_ohlcv` is emitted only when the source metadata explicitly proves the basis used by the snapshot that generated the decision; absence or ambiguity emits `signal_basis_unavailable`. No code infers raw basis from a provider name or from a `payload_sha256`.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `ObservationSidecarTests.test_sidecar_binds_basis_by_signal_id_and_observation_hash`
  - `ObservationSidecarTests.test_sidecar_rejects_unknown_observation_hash_binding`
  - `ObservationSidecarTests.test_unproven_basis_is_signal_basis_unavailable`
  - `ObservationSidecarTests.test_verified_raw_ohlcv_is_the_only_v1_stock_basis`
  - `ObservationSidecarTests.test_report_sidecar_reuses_the_same_in_memory_observations`
  - `ObservationSidecarTests.test_signal_observation_schema_and_hash_are_unchanged`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ObservationSidecarTests.test_sidecar_binds_basis_by_signal_id_and_observation_hash`
  Expected failure: `ImportError: cannot import name 'build_observation_sidecar' from 'advisor.evidence_schema'` or the equivalent missing-module failure before the helper exists.
- [ ] Implement sidecar construction from the list returned by the existing report observation-building path. Adjust the private CLI persistence helper to return that exact list after its existing cache write, then call the sidecar writer with the same objects; do not reload observations from SQLite and do not alter `SignalObservation` fields, identity, or hash computation.
- [ ] Keep report authority independent: a sidecar/archive error must be surfaced to the evidence transport path and must not mutate a generated report or invoke a fallback provider. The existing report fallback behavior in `live_loader.py` remains unchanged.
- [ ] Run the targeted sidecar tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ObservationSidecarTests`
- [ ] Run frozen observation/report regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome tests.test_live_loader tests.test_cache_config_cli`
- [ ] Commit the task result as `feat: persist observation evidence sidecars` after `git diff --check` and exact scope inspection.

**Acceptance:** The sidecar is derived from the report’s in-memory observations, is keyed by both immutable identifiers, cannot bind a different hash, never changes `SignalObservation`, and marks unproven stock/ETF basis as `signal_basis_unavailable`.

## Task 4 — Deterministic market/provider and corporate-action collection

**Reviewer boundary:** A reviewer can approve or reject assigned-provider determinism, sticky recovery, no silent fallback, session coverage, and Alpha Vantage split normalization without reviewing maturation.

**Files:** Create/extend `advisor/evidence_collector.py` and `tests/test_evidence_accumulation.py`. `advisor/live_loader.py` and `advisor/data_sources.py` remain unmodified; the collector owns its bounded Alpha Vantage `SPLITS` request construction so the existing report source surface is not expanded.

**Interfaces to implement:** `PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION`, `assigned_price_provider`, `EvidenceCollector.collect_market`, `EvidenceCollector.collect_corporate_actions`, and `normalize_split_ratio`. The collector writes transport only and has no Git or SQLite authority.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `ProviderAssignmentTests.test_price_provider_assignment_v1_is_deterministic`
  - `ProviderAssignmentTests.test_oldest_canonical_provider_is_sticky_for_existing_series`
  - `ProviderAssignmentTests.test_unknown_or_unsupported_symbol_is_market_data_unavailable`
  - `ProviderAssignmentTests.test_assigned_provider_unavailable_does_not_fallback`
  - `ProviderAssignmentTests.test_fmp_unavailable_does_not_call_yahoo_as_evidence_fallback`
  - `CorporateActionTests.test_alpha_vantage_splits_fixture_normalizes_decimal_ratios`
  - `CorporateActionTests.test_aapl_nvda_tsla_and_igv_split_fixtures_are_supported`
  - `CorporateActionTests.test_reverse_split_point_two_five_normalizes_to_one_over_four`
  - `CorporateActionTests.test_overlapping_revision_for_same_provider_and_symbol_creates_conflict`
  - `CorporateActionTests.test_equal_overlapping_normalized_events_are_consistent`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ProviderAssignmentTests.test_price_provider_assignment_v1_is_deterministic`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_collector'`.
- [ ] Implement the literal mapping:
  - `stock -> fmp`;
  - `etf -> fmp`;
  - `crypto:HYPE -> hyperliquid`;
  - every other supported crypto -> `binance`.
  Include the policy version in semantic provenance. For existing series, select the provider from the oldest canonical market evidence record before constructing the request. If that provider cannot answer, emit `market_data_unavailable` and make no second-provider request. Do not call the existing Yahoo/CoinGecko/Alpha fallback chain from the collector.
- [ ] Implement daily market transport with explicit symbol, asset type, interval, date, timezone, assigned provider, raw OHLCV basis, source request identity, and coverage window. Use the repository’s existing symbol support lists as inputs; unsupported symbols produce the unavailable status rather than dynamic provider selection.
- [ ] Implement Alpha Vantage `SPLITS` fixture-compatible parsing with `Decimal`, reject binary floating-point split arithmetic, and serialize ratios as canonical decimal strings. Include fixtures for AAPL 4:1, NVDA 10:1, TSLA 5:1, IGV 5:1, and reverse split `0.25 -> 1/4`. Network tests are opt-in and never run in the deterministic default suite.
- [ ] For corporate snapshots with the same `corporate_action_provider + symbol` and overlapping coverage windows, normalize and compare event presence, effective date, and split ratio. Equal events are accepted; any difference emits `corporate_action_revision_conflict` with its own conflict entries. Do not rewrite already committed outcomes.
- [ ] Run targeted provider/corporate tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ProviderAssignmentTests tests.test_evidence_accumulation.CorporateActionTests`
- [ ] Run existing data-source and live-loader regressions to prove report fallback behavior was not altered:
  `.\.venv\Scripts\python.exe -m unittest tests.test_data_sources tests.test_live_loader`
- [ ] Commit the task result as `feat: add deterministic evidence collectors` after scope and diff checks.

**Acceptance:** Provider assignment is versioned, deterministic, sticky, and fail-closed; FMP unavailability makes no Yahoo evidence request; market transport is raw/provenance-qualified; corporate-action fixtures use decimal ratios; equal overlap is consistent and differing overlap is an audit conflict.

## Task 5 — Canonical materialization and horizon qualification

**Reviewer boundary:** A reviewer can approve or reject canonical-only reads, basis gating, inclusive split windows, corporate-conflict blocking, feed-unavailable handling, and proof statuses without reviewing GitHub Actions.

**Files:** Create/extend `advisor/evidence_materializer.py` and `tests/test_evidence_accumulation.py`.

The frozen observation and outcome contracts remain read-only.

**Interfaces to implement:** `EvidenceMaterializer`, `HorizonQualification`, `MaterializationResult`, and `qualify_horizon`. The materializer accepts only an evidence checkout and DB target; it has no API for a transport directory.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `HorizonQualificationTests.test_unknown_signal_basis_blocks_stock_maturation_before_3b2`
  - `HorizonQualificationTests.test_verified_raw_ohlcv_is_required_for_stock_verified_none`
  - `HorizonQualificationTests.test_split_on_signal_market_date_blocks_horizon`
  - `HorizonQualificationTests.test_split_on_horizon_end_date_blocks_horizon`
  - `HorizonQualificationTests.test_split_after_h5_before_h10_blocks_only_h10_and_later`
  - `HorizonQualificationTests.test_no_split_produces_verified_no_split_in_signal_horizon_v1`
  - `HorizonQualificationTests.test_feed_unavailable_is_not_verified_none`
  - `HorizonQualificationTests.test_relevant_corporate_action_conflict_blocks_new_verified_none`
  - `HorizonQualificationTests.test_frozen_outcome_is_not_rewritten_by_new_corporate_revision`
  - `HorizonQualificationTests.test_materializer_has_no_unarchived_transport_input`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.HorizonQualificationTests.test_unknown_signal_basis_blocks_stock_maturation_before_3b2`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_materializer'`.
- [ ] Implement canonical checkout readers that validate branch schema, path partitions, gzip members, envelope hashes, logical identities, and manifest references before exposing records. Reconstruct observations from observation shards and market/corporate data from their canonical shards. Ignore SQLite rows and `signal_journal` as source authority.
- [ ] Implement the exact priority for each horizon: `market_data_unavailable`, `pending`, relevant `corporate_action_revision_conflict`, `signal_basis_unavailable` for stock/ETF, `feed_unavailable`, split in inclusive interval, verified no split, and crypto not applicable where the frozen contract says so. No feed-unavailable case may produce a `verified_none` proof.
- [ ] Implement split evaluation over `[signal_market_date, horizon_end_date]`, including both endpoints. A split after h5 and before h10 blocks h10 and h20 while leaving h5 governed by its own interval. Emit exactly `verified_no_split_in_signal_horizon_v1` only when the basis gate, feed, and corporate conflict gates all pass.
- [ ] Before any frozen evaluator call, resolve sidecar basis by `(signal_id, observation_hash)`. Unknown basis returns the blocked qualification and must not construct a 3B.2 input for that stock horizon.
- [ ] Run targeted materializer tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.HorizonQualificationTests`
- [ ] Run frozen observation/outcome and predictive contract regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome tests.test_predictive_evaluation tests.test_predictive_statistics`
- [ ] Commit the task result as `feat: qualify canonical evidence horizons` after `git diff --check` and protected-file scope inspection.

**Acceptance:** The materializer reads only canonical evidence, recognizes all required blocked statuses, applies the exact inclusive split policy, blocks unknown basis and relevant corporate conflicts, preserves frozen outcomes, and never treats unavailable feed as verified absence.

## Task 6 — Frozen 3B.2 outcome maturation and second archive

**Reviewer boundary:** A reviewer can approve or reject the authority boundary between evidence qualification and frozen outcome evaluation, including call tracing and pending-state persistence.

**Files:** Modify only `advisor/evidence_materializer.py` and `advisor/cli.py` within their new integration surfaces; extend `tests/test_evidence_accumulation.py`.

The frozen outcome, scoring, and risk contracts remain read-only.

**Interfaces to implement:** A materializer method that accepts eligible canonical observation/market records and calls `evaluate_signal_observation`, plus the existing `SQLiteCache.save_signal_forward_outcomes_for_signal` as the operational materialization sink. The method returns the exact `SignalForwardEvaluation` outcome tuple and pending tuple from the frozen authority without reimplementing any return/MFE/MAE/barrier math.

**TDD sequence:**

  - [ ] Write these exact failing tests:
    - `ArchiveDurabilityTests.test_local_transport_cannot_satisfy_maturation_before_first_archive`
    - `ArchiveDurabilityTests.test_push_confirmation_and_fresh_read_are_required_before_maturation`
  - `OutcomeMaturationTests.test_maturation_calls_evaluate_signal_observation`
  - `OutcomeMaturationTests.test_maturation_does_not_call_scoring`
  - `OutcomeMaturationTests.test_maturation_does_not_call_risk`
  - `OutcomeMaturationTests.test_maturation_stores_exact_frozen_evaluator_result`
  - `OutcomeMaturationTests.test_ineligible_horizon_is_not_sent_to_frozen_evaluator`
  - `OutcomeMaturationTests.test_first_archive_failure_prevents_maturation_and_second_archive`
  - `OutcomeMaturationTests.test_pending_horizons_are_preserved_without_recalculation`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.OutcomeMaturationTests.test_maturation_calls_evaluate_signal_observation`
  Expected failure: `ImportError: cannot import name 'mature_outcomes' from 'advisor.evidence_materializer'`.
- [ ] Use `unittest.mock.patch`/spy on `advisor.evidence_materializer.evaluate_signal_observation`, `advisor.evidence_materializer.classify_asset` only if present in the new integration boundary, and the scoring/risk entry points. Assert the evaluator is called for eligible inputs, scoring and risk are not called, and the stored outcome records equal the exact frozen return objects after conversion to the existing persistence record.
- [ ] Build the forward-market input from canonical market shards with the frozen `ForwardMarketSeries` contract. Do not duplicate frozen outcome mathematics, modify SQL schema, modify append-only triggers, or write legacy journal rows. Use `save_signal_forward_outcomes_for_signal` for operational rows only after canonical proof/outcome transport is prepared.
- [ ] Implement the two durability tests with a temporary Git repository: leave collector transport outside the evidence branch, invoke maturation against the branch checkout, and assert no proof/evaluator call occurs; then archive and confirm push, make a fresh checkout, and assert maturation becomes eligible. A materializer cannot receive a transport directory as an authority input.
- [ ] Enforce the sequence in the orchestration method: validate local transport; call `EvidenceArchive.archive` for the first market/corporate batch; require committed push confirmation; perform a fresh branch read; qualify horizons; evaluate only eligible horizons; write proof/outcome transport; call `EvidenceArchive.archive` for the second transaction. If the first archive is not confirmed, return without a proof/outcome and leave all horizons pending.
- [ ] Run targeted maturation tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.OutcomeMaturationTests`
- [ ] Run frozen outcome and report isolation regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_outcome tests.test_signal_observation tests.test_live_loader`
- [ ] Commit the task result as `feat: mature outcomes from canonical evidence` after diff and scope checks.

**Acceptance:** Only canonical, durably archived evidence reaches frozen 3B.2; evaluator output is authoritative; scoring/risk are never called; pending horizons remain pending; first archive failure prevents both maturation and second archive; outcome storage remains append-only and frozen.

## Task 7 — GitHub Actions permission boundaries and CLI dispatch

**Reviewer boundary:** A reviewer can approve or reject workflow job boundaries, secrets, concurrency, artifact handoffs, and the unchanged nightly workflow without running GitHub.

**Files:** Create `.github/workflows/financial-advisor-evidence.yml`; modify `advisor/cli.py` and `.github/workflows/financial-advisor-reports.yml`; extend `tests/test_evidence_accumulation.py`. Modify no `.github/workflows/financial-advisor-nightly-review.yml`.

**Interfaces to implement:** Add an `evidence` CLI namespace with exact subcommands and nonzero status propagation:

```text
python -m advisor evidence collect --symbols AAPL,NVDA --transport-dir .tmp/evidence-transport
python -m advisor evidence archive --transport-dir .tmp/evidence-transport --repo-dir .
python -m advisor evidence materialize --evidence-checkout .tmp/evidence-checkout --db .tmp/evidence.db
python -m advisor evidence mature --evidence-checkout .tmp/evidence-checkout --db .tmp/evidence.db --transport-dir .tmp/evidence-proof-transport
```

The existing `python -m advisor outcomes evaluate --input-path .tmp/forward-input.json --db .tmp/evidence.db` remains unchanged and is not routed through the new evidence commands.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `WorkflowContractTests.test_reports_job_has_contents_read_and_provider_secrets_only`
  - `WorkflowContractTests.test_archive_job_has_contents_write_and_no_provider_secrets`
  - `WorkflowContractTests.test_collector_job_has_contents_read_and_provider_secrets`
  - `WorkflowContractTests.test_publish_mature_job_has_contents_write_and_no_provider_secrets`
  - `WorkflowContractTests.test_publish_mature_order_is_first_archive_fresh_read_mature_second_archive`
  - `WorkflowContractTests.test_evidence_writer_concurrency_does_not_cancel_in_progress`
  - `WorkflowContractTests.test_nightly_workflow_is_not_modified_or_consumed_by_evidence_flow`
  - `WorkflowContractTests.test_evidence_cli_propagates_branch_missing_and_archive_failure`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.WorkflowContractTests.test_archive_job_has_contents_write_and_no_provider_secrets`
  Expected failure: `FileNotFoundError` for `.github/workflows/financial-advisor-evidence.yml`.
- [ ] Implement report-job sidecar upload with `contents: read` and only the existing provider secrets. The archive job consumes the uploaded transport, has `contents: write`, and receives no provider secrets. The collector job has `contents: read` plus provider secrets and emits transport only. The publish/mature job has `contents: write`, receives no provider secrets, performs first archive, fresh read, materialization/maturation, and second archive.
- [ ] Set concurrency group exactly `advisor-evidence-writer` with `cancel-in-progress: false`. Make artifact names, paths, and job outputs explicit; do not pass provider response data through writer environment variables. Preserve the existing report invocations and report fallback behavior.
- [ ] Add CLI parsers that delegate to the new components and return nonzero for `evidence_branch_missing`, archive rejection, validation failure, and first-archive failure. Keep `financial-advisor-nightly-review.yml` byte/scope unchanged and prove it does not consume runtime/evidence artifacts.
- [ ] Run targeted workflow and CLI tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.WorkflowContractTests`
- [ ] Run existing workflow/automation regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_github_actions_workflow tests.test_automation_scripts`
- [ ] Commit the task result as `ci: separate evidence collection and publication permissions` after exact workflow scope inspection.

**Acceptance:** Provider secrets exist only in report/collector jobs; write permissions exist only in archive/publish jobs; publish/mature has no provider secrets; writer concurrency is non-cancelling; the nightly workflow is unchanged; CLI errors are explicit and nonzero.

## Task 8 — Recovery acceptance, security properties, and registered mutations

**Reviewer boundary:** A reviewer can approve or reject rebuildability and fail-closed behavior from temporary files/repos without touching `data/advisor.db`, GitHub, provider quotas, or user branches.

**Files:** Extend `tests/test_evidence_accumulation.py`; modify implementation files only when a failing security/recovery test identifies a task-scoped defect. No production scope expansion is permitted.

**TDD sequence:**

- [ ] Write the mandatory acceptance test `RecoveryAcceptanceTests.test_rebuild_from_main_and_evidence_branch_after_operational_loss`. It must:
  1. write canonical observation, market, corporate-action, proof, outcome, manifest, and pending-state evidence into a temporary repository;
  2. create the operational materialization DB in a temporary directory;
  3. delete that DB, all operational transport, and all temporary workspace/cache copies;
  4. start a fresh materialization using only the temporary `main` checkout and the temporary `advisor-evidence` branch;
  5. compare observation IDs/hashes/counts, market/corporate IDs, proof IDs, outcome IDs/hashes/counts, and pending state with the original snapshot;
  6. spy that scoring was never called and that `signal_journal` was never read.
- [ ] Write these exact security tests:
  - `SecurityPropertyTests.test_path_traversal_is_rejected`
  - `SecurityPropertyTests.test_absolute_path_is_rejected`
  - `SecurityPropertyTests.test_symlink_path_is_rejected`
  - `SecurityPropertyTests.test_malformed_gzip_is_rejected`
  - `SecurityPropertyTests.test_multi_member_gzip_is_rejected`
  - `SecurityPropertyTests.test_oversized_decompression_is_rejected`
  - `SecurityPropertyTests.test_duplicate_json_key_is_rejected`
  - `SecurityPropertyTests.test_nan_and_infinity_are_rejected`
  - `SecurityPropertyTests.test_provider_and_path_injection_are_rejected`
  - `SecurityPropertyTests.test_schema_mismatch_is_rejected`
  - `SecurityPropertyTests.test_hash_mismatch_is_rejected`
- [ ] Run RED for recovery:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.RecoveryAcceptanceTests.test_rebuild_from_main_and_evidence_branch_after_operational_loss`
  Expected failure: `AssertionError` because recovery cannot yet prove equal canonical IDs, hashes, counts, and pending state.
- [ ] Run the deterministic recovery/security target after implementation:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.RecoveryAcceptanceTests tests.test_evidence_accumulation.SecurityPropertyTests`
- [ ] Apply only the registered spec mutations in isolated temporary copies. The mutation set is: change inclusive split `<=` to `<`; exclude the signal date; map `feed_unavailable` to `verified_none`; overwrite divergent same-identity payload; accept path traversal; accept provider mixing; import `signal_journal` during recovery; let archive failure alter the report; create a second manifest on identical retry; allow `verified_none` through corporate revision conflict; treat `signal_basis_unavailable` as raw; read unarchived transport; and switch provider after assigned-provider unavailability.
- [ ] For each mutation, make exactly one source edit in a disposable copy, run the exact targeted unittest command for the affected property, record the expected failing assertion, then discard the disposable copy immediately. The original worktree and every commit remain unchanged. A mutation is survived only if the targeted test unexpectedly passes; the final record must be `mutations_survived=0`.
- [ ] Run the evidence suite after mutation restoration:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation`
- [ ] Commit the task result as `test: prove evidence recovery and adversarial boundaries` only after every mutation is restored and the recovery evidence is recorded.

**Acceptance:** The complete rebuild works from `main + advisor-evidence` alone, all operational loss is survivable, legacy `signal_journal` is ignored, scoring is not called, all listed security properties fail closed, and every registered mutation is killed with `mutations_survived=0`.

## Task 9 — Documentation and final integration verification

**Reviewer boundary:** A reviewer can approve or reject the final implementation package, documentation, frozen boundaries, regression evidence, and Git scope before a later human freeze decision.

**Files:** Modify `docs/AUTOMATION_SETUP.md`; update only the new evidence tests if a final assertion needs a stable test location. Do not modify the design spec, protected modules, nightly workflow, or Phase 3B.4 materials.

**TDD/verification sequence:**

- [ ] Write the documentation contract test `DocumentationTests.test_automation_setup_documents_evidence_branch_permissions_and_recovery` before editing the documentation. Run:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.DocumentationTests.test_automation_setup_documents_evidence_branch_permissions_and_recovery`
  Expected RED: `AssertionError` because the evidence workflow/permissions/recovery section is absent.
- [ ] Document the exact orphan bootstrap, evidence branch missing behavior, provider assignments, first-archive/fresh-read/maturation/second-archive order, no-secret writer boundaries, concurrency group, no-nightly-change rule, and recovery source of truth. Do not document provider fallback as evidence behavior.
- [ ] Run the evidence suite and relevant frozen subsets:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation`

  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome tests.test_predictive_evaluation tests.test_predictive_statistics tests.test_live_loader tests.test_data_sources tests.test_cache_config_cli`
- [ ] Run workflow/automation integration checks:
  `.\.venv\Scripts\python.exe -m unittest tests.test_github_actions_workflow tests.test_automation_scripts`
- [ ] Run the first full-suite gate after core Python and workflow integration are complete:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests`
  If and only if the result reproduces exactly the two historical `test_nightly_auth_dry_run` failures, classify the result as `PASS_WITH_BASELINE_KNOWN_FAILURES` and do not modify those historical tests. Any new failure is a blocker and stops the freeze.
- [ ] Perform the final plan/implementation scope checks:
  - `git diff --check` returns no output and exit code 0;
  - `git status --short` contains only the intended implementation files for this future phase;
  - no protected module appears in a `Files: Modify` list;
  - no provider call was made by tests marked deterministic;
  - the case-insensitive forbidden-placeholder scan returns zero matches in implementation/docs;
  - no automatic 3B.3.1, automatic 3B.3.2, or Phase 3B.4 task/job/command exists;
  - `financial-advisor-nightly-review.yml` has no diff;
  - `mutations_survived=0` is recorded.
- [ ] Commit the task result as `docs: document canonical evidence operations` after the final verification report. This plan itself is committed separately by the current documentation-only session.

**Acceptance:** Documentation matches executable permission and recovery contracts; evidence and frozen regression subsets pass; the full-suite result is either clean or exactly the pre-existing two-test baseline classification; new failures block completion; protected modules and excluded phases remain untouched.

## Regression Checkpoint Matrix

The implementation worker runs the smallest relevant checkpoint after each task and does not run the full suite after every task.

| Checkpoint | Exact command | Purpose |
|---|---|---|
| Canonical schema | `\.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests` | Canonical bytes, gzip, hashes, duplicate/conflict semantics. |
| Archive | `\.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests` | Orphan branch, atomic batch, retry, conflicts, push safety. |
| Evidence suite | `\.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation` | All new properties, recovery, security, workflow, and mutation guards. |
| Frozen observation/outcome | `\.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome` | Protected contract regression and append-only operational sink. |
| Existing report data path | `\.\.venv\Scripts\python.exe -m unittest tests.test_live_loader tests.test_data_sources tests.test_cache_config_cli` | Confirm existing live fallback/report behavior remains unchanged. |
| Workflow/automation | `\.\.venv\Scripts\python.exe -m unittest tests.test_github_actions_workflow tests.test_automation_scripts` | Existing report/nightly contracts plus automation documentation. |
| Frozen predictive subsets | `\.\.venv\Scripts\python.exe -m unittest tests.test_predictive_evaluation tests.test_predictive_statistics` | Confirm no predictive contract drift while evidence gates are added. |
| Full gate | `\.\.venv\Scripts\python.exe -m unittest discover -s tests` | Run only after core Python, workflow integration, and before final freeze. |

No full suite is run in the current documentation-only session. No current task starts TDD; the commands above are the future execution sequence after explicit implementation authorization.

## Final Implementation Gate

Before a future implementation is presented for human review, the worker must provide:

- the exact evidence branch commit/revision used for fresh-read materialization;
- archive results for first and second transactions, including any `no_op` or conflict-only result;
- the basis/split/provider qualification counts and pending state;
- frozen evaluator call-spy evidence showing evaluator calls and zero scoring/risk calls;
- recovery ID/hash/count comparison;
- security-test result and `mutations_survived=0`;
- targeted/regression/full-suite results with any exact baseline-known failures separated;
- `git diff --check` result and exact changed-file list;
- confirmation that no protected module, nightly workflow, design spec, legacy journal, automatic 3B.3.1/3B.3.2, or Phase 3B.4 artifact was changed.

The current session ends after committing this Markdown plan. It does not create Python files, create `advisor-evidence`, create a branch, edit workflows, call providers, start TDD, alter the design spec, or start Phase 3B.4.
