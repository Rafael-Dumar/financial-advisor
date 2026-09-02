# Canonical Evidence Accumulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or **superpowers:executing-plans** to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved canonical evidence accumulation boundary so that reports emit observation sidecars, assigned-provider collectors produce validated local transport, an orphan `advisor-evidence` branch becomes the sole durable authority, and materialization can produce split/basis proofs and frozen 3B.2 outcomes only after archive durability is confirmed. The implementation must preserve historical decisions when databases, runners, caches, workspaces, or transport artifacts disappear.

**Architecture:** Keep report generation and the frozen signal/outcome contracts intact. Add four narrowly separated components: `evidence_schema` owns canonical serialization, hashes, envelopes, and validation; `evidence_archive` owns staging, append-only Git publication, manifest/conflict semantics, and branch enforcement; `evidence_collector` owns deterministic provider assignment and local market/corporate-action transport; `evidence_materializer` reads only a freshly fetched canonical branch, qualifies horizons, calls the frozen 3B.2 evaluator, and emits proof/outcome transport. `cli.py` provides thin command dispatch and report-sidecar wiring. GitHub Actions jobs are separated by provider-secret and repository-write permissions.

**Tech Stack:** Python 3.12, existing `unittest` test discovery, `decimal.Decimal` for split ratios, standard-library JSON/Gzip/Hashlib/Pathlib/TemporaryDirectory/Subprocess APIs, the existing frozen signal observation and forward outcome modules, SQLite only as a rebuild target, and local temporary Git repositories for archive and recovery tests. No real provider calls or real GitHub repository writes are part of the deterministic test suite.

**Spec:** `docs/superpowers/specs/2026-08-27-canonical-evidence-accumulation-design.md`

## Global Constraints

- This plan is the implementation plan for approved Phase 3B.3.3 only. It does not reopen the design, create `advisor-evidence`, create a branch, alter workflows now, call providers now, or start implementation/TDD now.
- The approved atomic-source-binding design is frozen in the spec at `df617f9a3d07157ed396dad0f576733838ea3567`; Task 1 remains frozen and Task 2 remains frozen at `92d96f6d7be062f05e03739a002553e492d6467b`, while earlier experimental Task 3 commits are historical inputs to the amended implementation and are not treated as the contract.
- The authoritative source is `main + advisor-evidence`. SQLite, GitHub Actions caches, runners, local workspaces, and transport files are operational surfaces and never canonical authority.
- `SignalObservation` remains frozen. Observation provenance MUST be captured atomically as `ObservationEvidenceRecord(observation, source_binding)` from the exact `AssetSnapshot` used by the decision; no retrospective reconstruction from an observation is allowed.
- `snapshot_projection_v1` is a closed-world, explicitly enumerated, versioned projection. A future `AssetSnapshot` field does not enter v1 automatically, and `snapshot_sha256_v1` is `SHA256(canonical_json_bytes(snapshot_projection_v1(exact_snapshot_X))).hexdigest()`.
- An unarchived observation sidecar is transport only. Only a confirmed canonical archive result (`committed` plus durable confirmation, or validated `no_op` plus durable confirmation) promotes `ObservationEvidenceRecord` to durable authority in `advisor-evidence`.
- Historical source binding is never rebuilt with current providers, current market data, a symbol lookup, SQLite, or a later snapshot. The captured `ObservationSourceBinding` is the only source provenance passed from construction to the sidecar.
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
- Bootstrap/archive operations MUST NOT checkout, switch branch, reset, clean, or remove files from the caller's supplied `main` worktree. They must use a dedicated temporary clone/worktree or equivalent Git plumbing, and must prove that caller `HEAD`, branch, index, tracked/untracked worktree bytes, and unrelated files are unchanged.
- No mutation is committed. Only mutations already registered in the approved spec are used, each in an isolated temporary copy, with `mutations_survived=0` required.

## File Map

### Files to create

| File | Responsibility and concrete consumer |
|---|---|
| `advisor/evidence_schema.py` | Canonical evidence types, schema/evidence-type versions, logical identities, strict JSON parsing, canonical JSON, deterministic gzip, canonical/payload hashes, envelope validation, sidecar shape/basis binding, path partitioning, and duplicate/nonfinite/hash checks. Consumed by the archive, collector, materializer, and CLI sidecar writer. It performs no network, Git, SQLite, scoring, or outcome evaluation. |
| `advisor/evidence_archive.py` | Validates local transport, stages a complete batch, bootstraps or verifies an orphan `advisor-evidence` branch only through an explicit deployment operation, publishes one atomic Git commit, classifies `committed`/`no_op`/`conflict`, writes conflict-only transactions, and performs at most three non-force push attempts. Consumed by CLI and the evidence workflow. |
| `advisor/evidence_collector.py` | Implements `price_provider_assignment_v1`, typed asset collection, assigned-provider market collection, Alpha Vantage `SPLITS` fixture-compatible normalization using `Decimal`, coverage windows, the local deterministic `us_equities_session_v1` DST/holiday/session authority, and local transport output. It never invokes the report loader’s dynamic fallback chain or compares history. Consumed by the collector job and archive/materializer tests. |
| `advisor/evidence_materializer.py` | Reads a freshly fetched canonical evidence checkout only, reconstructs observations/market/corporate shards, applies basis/split/conflict/feed gates, calls `evaluate_signal_observation(observation, series)` at most once per observation over the largest eligible prefix, and emits proof/outcome transport plus pending maturation state. Consumed by CLI and the publish/mature job. |
| `tests/test_evidence_accumulation.py` | Deterministic unit, property-style, local-Git integration, recovery acceptance, workflow contract, security, and mutation-resistance tests for all new components. Fixtures are in-memory or temporary-directory data. |
| `.github/workflows/financial-advisor-evidence.yml` | Separate collector and publish/mature jobs with the required provider-secret and repository-write boundaries, concurrency group, first-archive/fresh-read/second-archive order, and no provider secrets in writer jobs. |

### Files to modify during implementation of this plan

| File | Narrow change and consumer |
|---|---|
| `advisor/evidence_schema.py` | Task 1 owns the canonical primitives; amended Task 3 adds `ObservationSourceBinding`, `ObservationEvidenceRecord`, the closed-world `snapshot_projection_v1`/`snapshot_sha256_v1` helpers, records-only sidecar serialization, and record-scoped fail-closed basis validation. |
| `advisor/cli.py` | Replace the observation-only construction boundary with atomic `ObservationEvidenceRecord` construction, derive the SQLite observation tuple from completed records, and pass records directly to the sidecar. Keep evidence command dispatch for later tasks. |
| `.github/workflows/financial-advisor-reports.yml` | Preserve the existing report job and provider behavior, upload the observation-sidecar transport with the report output, and add the read-only-to-writer handoff required by the archive job without giving provider secrets to the writer. |
| `docs/AUTOMATION_SETUP.md` | Document the evidence workflow, exact command boundaries, orphan-branch bootstrap operation, permissions, concurrency, fresh-read requirement, and recovery assumptions. |

### Read-only contract anchors

`advisor/cache.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `.github/workflows/financial-advisor-nightly-review.yml`, and the four frozen Phase 3B contract documents are inspected inputs. `advisor/models.py`, `advisor/data_sources.py`, `advisor/data_pipeline.py`, and `advisor/live_loader.py` already carry the qualified claim propagation and are regression-only inputs for amended Task 3; no change is expected in this plan. No candle, decision, scoring, risk, or frozen-contract behavior changes. The existing live report fallback chain remains a regression surface only; it is not reused as evidence authority.

### Protected modules — never list under `Files: Modify`

`advisor/scoring.py`, `advisor/risk.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `advisor/predictive_evaluation.py`, and `advisor/predictive_statistics.py` remain unchanged. Any required change is `DESIGN_CONFLICT` and stops execution.

## Shared Interfaces and Data Contracts

The following names and meanings are fixed before task execution so later tasks do not invent incompatible interfaces.

```python
# advisor/evidence_schema.py
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Mapping, Sequence
from advisor.models import AssetSnapshot, PriceBasisClaim
from advisor.signal_observation import SignalObservation

ArchiveStatus = Literal[
    "committed", "no_op", "conflict", "rejected", "evidence_branch_missing",
]
HorizonStatus = Literal[
    "market_data_unavailable", "pending", "conflict",
    "signal_basis_unavailable", "feed_unavailable",
    "split_in_horizon_unavailable", "verified_none", "not_applicable",
]
CorporateActionReasonCode = Literal["corporate_action_revision_conflict"]
SignalBasisStatus = Literal["verified_raw_ohlcv", "signal_basis_unavailable"]
SplitPolicy = Literal["verified_no_split_in_signal_horizon_v1"]
CryptoPolicy = Literal["not_applicable_crypto_raw_ohlcv_v1"]

@dataclass(frozen=True)
class ObservationSourceBinding:
    signal_id: str
    observation_hash: str
    snapshot_sha256: str
    price_basis_claim: PriceBasisClaim | None

@dataclass(frozen=True)
class ObservationEvidenceRecord:
    observation: SignalObservation
    source_binding: ObservationSourceBinding

def canonical_json_bytes(value: object) -> bytes:
    pass
def strict_json_loads_bytes(data: bytes) -> object:
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

def snapshot_projection_v1(snapshot: AssetSnapshot) -> Mapping[str, object]:
    pass
def snapshot_sha256_v1(snapshot: AssetSnapshot) -> str:
    pass

def build_observation_sidecar(
    records: Sequence[ObservationEvidenceRecord],
    *,
    output_path: Path,
) -> Path:
    pass

def resolve_signal_price_basis_status(
    *, record: ObservationEvidenceRecord,
) -> SignalBasisStatus:
    pass
```

`ObservationSourceBinding` and `ObservationEvidenceRecord` are the only Task 3
provenance containers. `snapshot_sha256_v1` is computed exactly once at the
atomic report construction boundary; `build_observation_sidecar` accepts only
the already-built records and never accepts an `AssetSnapshot`, a provider, a
cache, SQLite, or `snapshots_by_symbol`. The sidecar top level is exactly
`{"schema_version": "1.0", "source_sha": ..., "run_id": ..., "report_type": ..., "records": [...]}`.
The four top-level metadata values are copied from the records' frozen
observations, and all records must agree on them. Each `records` item is exactly
`{"observation": <the frozen SignalObservation canonical object>, "source_binding": {"signal_id": ..., "observation_hash": ..., "snapshot_sha256": ..., "price_basis_claim": null or {"price_basis": ..., "price_basis_policy_version": ..., "source_contract": ...}}}`.
There are no parallel provenance arrays and no late symbol-based rejoin.
For every record, `source_binding.signal_id` MUST equal `observation.signal_id`
and `source_binding.observation_hash` MUST equal `observation.observation_hash`;
the builder and resolver fail closed on either mismatch.

The binding is evidence-only. It does not alter `AssetDecision`, scoring, risk,
`ideal_entry`, stop, targets, report rendering, provider selection, fallback
behavior, decision confidence, or the frozen observation/hash contract.

The legacy experimental sidecar fields `signal_input_hash`,
`snapshot_binding`, and `entry_integrity_sha256` are removed from the Task 3
serializer, parser, resolver, and tests. They remain historical implementation
artifacts only; they are not a compatibility contract and are not retained as a
second checksum stack after atomic capture exists. A private parser may parse a
canonical sidecar item into `ObservationEvidenceRecord`, but no new public
provenance API is added for that conversion.

```python
# advisor/models.py
@dataclass(frozen=True)
class PriceBasisClaim:
    price_basis: Literal["raw_ohlcv"]
    price_basis_policy_version: Literal["price_basis_v1"]
    source_contract: str

# appended to DataFetchMetadata without changing existing fields
price_basis_claim: PriceBasisClaim | None = None
```

```python
# advisor/data_sources.py
def is_qualified_raw_ohlcv_claim(claim: PriceBasisClaim | None) -> bool:
    pass
```

`canonical_json_bytes` uses UTF-8, `ensure_ascii=False`, `sort_keys=True`, separators `(",", ":")`, `allow_nan=False`, rejects nonfinite/unsupported values, and emits no final newline. `strict_json_loads_bytes` is the only function that detects duplicate keys because it receives raw JSON bytes; it uses strict UTF-8, `object_pairs_hook`, and `parse_constant`. `canonical_json_bytes` sorts object keys but never reorders arrays; an owning serializer must establish any identity-defined order before calling it. `snapshot_projection_v1` preserves the explicitly declared semantic order of each snapshot sequence. `deterministic_gzip` emits one DEFLATE-9 member with `MTIME=0`, `FLG=0`, `XFL=2`, and `OS=255`; decompression rejects trailing bytes, unused data, CRC/size mismatch, multiple members, and the configured uncompressed-size limit.

```python
# advisor/evidence_archive.py
@dataclass(frozen=True)
class ArchiveResult:
    status: ArchiveStatus
    batch_identity: str
    manifest_path: str | None
    committed_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    error_code: str | None
    durability_confirmed: bool

class EvidenceArchive:
    def __init__(self, *, repo_dir: Path, branch_name: str = "advisor-evidence", max_push_attempts: int = 3):
        pass
    def archive(self, transport_dir: Path) -> ArchiveResult:
        pass

def bootstrap_evidence_branch(*, repo_dir: Path, branch_name: str = "advisor-evidence") -> str:
    pass

def oldest_canonical_provider_by_symbol(
    *, evidence_checkout: Path, symbols: Sequence[str]
) -> Mapping[str, str]:
    pass
```

`bootstrap_evidence_branch` is an explicit one-time deployment operation. It creates an orphan root containing only `evidence/branch-schema.json`, with the fixed schema/version and zero financial evidence. A second bootstrap refuses to recreate or rewrite an existing branch. `EvidenceArchive.archive` never calls it automatically. Both bootstrap and archive operate in a dedicated temporary clone/worktree; the caller's main worktree is never switched, reset, cleaned, or emptied.

```python
# advisor/evidence_collector.py
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION = "price_provider_assignment_v1"

@dataclass(frozen=True)
class CollectionAsset:
    symbol: str
    asset_type: Literal["stock", "etf", "crypto"]

@dataclass(frozen=True)
class CanonicalSplitRatio:
    new_shares: str
    old_shares: str

def assigned_price_provider(*, asset: CollectionAsset, existing_provider: str | None) -> str | None:
    pass
def normalize_split_factor(*, split_factor: str | Decimal) -> CanonicalSplitRatio:
    pass

class EvidenceCollector:
    def __init__(
        self, *, fetch_json: Callable[..., object], transport_root: Path,
        now_utc: datetime | None = None,
    ):
        pass
    def collect_market(
        self, *, assets: Sequence[CollectionAsset],
        existing_provider_by_symbol: Mapping[str, str],
    ) -> Path:
        pass
    def collect_corporate_actions(
        self, *, assets: Sequence[CollectionAsset],
        coverage_windows: Mapping[str, tuple[date, date]],
    ) -> Path:
        pass

US_EQUITIES_SESSION_POLICY_VERSION = "us_equities_session_v1"
SessionCandleStatus = Literal["accepted", "rejected"]

def us_eastern_dst_transition_utc(
    year: int, *, transition: Literal["start", "end"]
) -> datetime:
    pass
def us_eastern_offset_for_utc(utc_datetime: datetime) -> timedelta:
    pass
def us_eastern_local(utc_datetime: datetime) -> datetime:
    pass
def us_market_holidays(year: int) -> frozenset[date]:
    pass
def us_early_close_dates(year: int) -> frozenset[date]:
    pass
def us_equity_session_close(session_date: date) -> time:
    pass
def validate_us_equity_candle(
    *, market_date: date, now_utc: datetime, synthetic: bool
) -> SessionCandleStatus:
    pass
```

The assigned provider function has no success-based fallback. `oldest_canonical_provider_by_symbol` scans canonical market-bar shards in ascending `market_date` order, uses the provider on the oldest record for each symbol, and requires all records in that series to agree; mixed providers raise a provider-assignment conflict before collection. For an existing series, the caller supplies that provider; a mismatch is a provider-assignment conflict, not a provider switch. The v1 mapping constant lives in this main-branch Python module and is included in semantic provenance, never in workflow YAML.

`us_equities_session_v1` is a local stdlib-only authority inside `advisor/evidence_collector.py`. It computes the second Sunday in March at 07:00 UTC as the DST start and the first Sunday in November at 06:00 UTC as the DST end, returning fixed `-05:00`/`-04:00` offsets before/after those instants. It defines regular close as 16:00 local and early close as 13:00 local; it derives observed New Year, Martin Luther King Jr., Presidents, Good Friday, Memorial, Juneteenth, Independence Day, Labor, Thanksgiving, and Christmas holidays. Its exact early-close set is the immediately preceding weekday session for Independence Day and Christmas when that session is not an observed full-market holiday, plus the Friday after Thanksgiving; when the calendar eve is a weekend, the preceding weekday is used, but an observed full-market holiday is never reclassified as an early close. Weekends and those holidays are rejected. `validate_us_equity_candle` rejects a synthetic row and a current/incomplete session before the applicable close, and accepts only a completed prior session. It does not import a private session helper from `advisor/signal_outcome.py`. No third-party timezone database or network calendar service is used.

```python
# advisor/evidence_materializer.py
@dataclass(frozen=True)
class HorizonQualification:
    status: HorizonStatus
    policy: SplitPolicy | CryptoPolicy | None
    reason_code: CorporateActionReasonCode | None
    proof: Mapping[str, object] | None

class EvidenceMaterializer:
    def __init__(self, *, evidence_checkout: Path, db_path: Path):
        pass
    def materialize(self) -> "MaterializationResult":
        pass
    def read_observation_evidence_record(
        self, *, signal_id: str, observation_hash: str
    ) -> ObservationEvidenceRecord:
        pass
    def qualify_horizon(
        self, *, record: ObservationEvidenceRecord, horizon: int,
    ) -> HorizonQualification:
        pass

    def largest_continuously_eligible_horizon(
        self, *, qualifications: Mapping[int, HorizonQualification]
    ) -> int | None:
        pass

    def evaluate_observation_once(
        self, *, observation: SignalObservation,
        series: ForwardMarketSeries,
        qualifications: Mapping[int, HorizonQualification],
    ) -> SignalForwardEvaluation:
        pass

@dataclass(frozen=True)
class MaterializationResult:
    completed_horizons: tuple[str, ...]
    pending_horizons: tuple[str, ...]
    proof_transport: Path | None
    outcome_transport: Path | None
```

`EvidenceMaterializer` has no transport-path input. It reads canonical shards from `evidence_checkout`, verifies the checkout revision was fetched after the first archive, and uses `SQLiteCache` only to rebuild the operational materialization. It qualifies every horizon externally, finds the largest continuously eligible prefix in `(5, 10, 20, 40)`, limits the `ForwardMarketSeries` to that prefix, and calls `evaluate_signal_observation(observation, series)` at most once per observation per maturation cycle. It accepts only returned outcomes whose horizons have an externally eligible status and leaves every blocked horizon in its external status/pending state. It never calls scoring or risk.

For the frozen evaluator, the only externally eligible statuses are `verified_none` for a stock horizon with the required split policy and `not_applicable` for a crypto horizon with the required crypto policy. `pending`, `market_data_unavailable`, `conflict`, `signal_basis_unavailable`, `feed_unavailable`, `split_in_horizon_unavailable`, and `not_applicable` on any non-crypto asset are blocked and cannot be passed to or accepted from 3B.2. The frozen call has no horizon argument: `evaluate_signal_observation(observation, series)` internally traverses `5, 10, 20, 40`. The largest prefix is therefore the greatest member of `(5, 10, 20, 40)` whose own horizon and every shorter prefix are eligible.

## Call-Graph Finding: Explicit Signal Price Basis

The current HEAD already contains the non-protected qualified-claim propagation
from the experimental Task 3 history. `Candle` contains only date and numeric
OHLCV fields; `DataFetchMetadata` contains provider, endpoint, timestamps,
freshness, fallback, granularity, `market_data_kind`, and the optional explicit
`price_basis_claim`. The relevant report path is:

```text
LiveDataLoader._fetch / _fetch_optional
  -> _fetch_metadata
  -> stock_snapshot_from_payloads / crypto_snapshot_from_payloads
  -> _price_fetch_metadata
  -> AssetSnapshot.data_fetch_metadata
  -> _scan decision + exact AssetSnapshot X
  -> _build_signal_observation_records
       -> build_signal_observation(decision, X) = O
       -> snapshot_sha256_v1(X) + X.data_fetch_metadata.price_basis_claim = B(X)
       -> ObservationEvidenceRecord(O, B(X))
  -> observations = tuple(record.observation for record in records)
  -> SQLite receives observations; sidecar receives records
```

The amended Task 3 does not reopen or rewrite that propagation. It changes only
`advisor/evidence_schema.py`, `advisor/cli.py`, and the evidence tests. The
qualified source-contract factories and loader/cache propagation remain a
regression surface; if they are insufficient, implementation must stop with a
scoped design conflict instead of changing a protected module or inferring
provenance.

The existing qualified parser routes are:

- FMP `historical-price-eod/full` parsed through the existing raw OHLCV `historical` fields;
- Binance `klines`;
- Hyperliquid `candleSnapshot`.

Each factory returns `PriceBasisClaim(price_basis="raw_ohlcv", price_basis_policy_version="price_basis_v1", source_contract=source_contract_id)` where `source_contract_id` is one of these exact values: `fmp.historical_price_eod.full.raw_ohlcv_v1`, `binance.futures_klines.raw_ohlcv_v1`, or `hyperliquid.candle_snapshot.raw_ohlcv_v1`. The claim is passed by the corresponding loader call site, not inferred by `_fetch_metadata`. A cache hit may reattach that claim only when the current call site is the same qualified route, its request identity is the exact cache key, and the same versioned parser will process the payload; the cached payload does not create the claim. FMP light, Yahoo, Stooq, Alpha Vantage adjusted daily history, cache entries without an exact qualified route/parser claim, and any route not in the qualified registry return no claim and therefore produce `signal_basis_unavailable`.

Under Approach B, `_build_signal_observation_records` copies the claim from the
same in-memory snapshot X immediately after constructing O and never asks a
provider or a later snapshot for a claim. The sidecar copies B exactly; its
resolver validates the record's captured claim and does not pretend to derive
historical provenance from O alone.

This path does not alter `Candle`, `AssetDecision`, `SignalObservation`,
scoring, risk, or report rendering. RED tests compare decisions and rendered
reports built from snapshots with and without the optional metadata, and spy
that the scoring/risk call behavior is unchanged. If a source/parser cannot
satisfy the explicit claim contract, the implementation must leave the claim
absent and return `signal_basis_unavailable`; it must not infer raw basis from
provider name or payload shape.

The call-graph inspection found a safe non-protected path. If implementation discovers that the claim cannot be propagated and validated without changing a protected module or changing a frozen decision contract, stop immediately and record `DESIGN_CONFLICT`; do not infer the basis and do not introduce a hidden workaround.

## Task 1 — Canonical evidence schema and idempotency content

**Reviewer boundary:** A reviewer can approve or reject canonical bytes, gzip bytes, envelope validation, logical identities, and duplicate/conflict classification without reviewing Git publication or collection.

**Files:** Create `advisor/evidence_schema.py` and `tests/test_evidence_accumulation.py` with the `CanonicalSerializationTests` and `CanonicalIdempotencyTests` classes. No protected file is modified.

**Interfaces to implement in `advisor/evidence_schema.py`:** `canonical_json_bytes(value: object) -> bytes`, `strict_json_loads_bytes(data: bytes) -> object`, `deterministic_gzip(json_bytes: bytes) -> bytes`, `decompress_single_member_gzip(data: bytes, *, max_uncompressed_bytes: int) -> bytes`, `canonical_content_sha256(*, evidence_type: str, schema_version: str, logical_identity: Mapping[str, object], payload: Mapping[str, object], semantic_provenance: Mapping[str, object]) -> str`, `payload_sha256(payload: object) -> str`, `validate_canonical_envelope(envelope: Mapping[str, object]) -> None`, and `classify_idempotency(existing, incoming) -> Literal["duplicate_same", "conflict"]`. The classifier compares logical identity and `canonical_content_sha256` only.

**TDD sequence:**

- [ ] Write these exact failing tests before implementation:
  - `CanonicalSerializationTests.test_canonical_json_is_deterministic_for_key_order`
  - `CanonicalSerializationTests.test_strict_json_loader_rejects_duplicate_raw_keys`
  - `CanonicalSerializationTests.test_strict_json_loader_rejects_malformed_utf8_and_json`
  - `CanonicalSerializationTests.test_strict_json_loader_rejects_nonfinite_constants`
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
- [ ] Implement the smallest schema module with strict recursive JSON validation, `strict_json_loads_bytes(data: bytes) -> object` using strict UTF-8 decoding, `object_pairs_hook` that raises on duplicate keys, `parse_constant` that raises on NaN/Infinity, explicit finite-number checks for already parsed values, deterministic key/array normalization, and the exact gzip header/trailer/member checks above. The duplicate-key test input must be raw bytes such as `b'{"symbol":"AAPL","symbol":"NVDA"}'`; a Python dict is not a valid duplicate-key test. The canonical-content input must serialize a structure containing only `evidence_type`, `schema_version`, `logical_identity`, `payload`, and `semantic_provenance`; transport metadata must not be accepted by that hash function.
- [ ] Run the targeted schema tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests`
  Expected result after the implementation: all selected tests pass with zero skips.
- [ ] Run the relevant frozen regression subset:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome`
- [ ] Commit the task result as `feat: add canonical evidence schema` after `git diff --check` reports no errors and only the task files are staged.

**Acceptance:** Reordering object keys produces identical canonical bytes and hashes; raw duplicate keys, invalid UTF-8, malformed JSON, NaN, Infinity, malformed/multi-member/oversized gzip, and hash mismatches fail closed; same identity plus same canonical content is `duplicate_same`; same identity plus a different canonical content hash is `conflict`; identical OHLC payload with different semantic provenance is `conflict`; different transport metadata does not change canonical content.

## Task 2 — Append-only archive, orphan bootstrap, and manifest transactions

**Reviewer boundary:** A reviewer can approve or reject branch shape, staging atomicity, manifest identity, conflict transactions, retry behavior, and push safety using only local temporary Git repositories.

**Files:** Create/extend `advisor/evidence_archive.py` and `tests/test_evidence_accumulation.py`. Do not create `advisor-evidence` in the user repository and do not modify any workflow.

**Interfaces to implement:** `bootstrap_evidence_branch`, `EvidenceArchive`, `ArchiveResult`, `oldest_canonical_provider_by_symbol`, and internal reader/writer helpers that validate every staged object before one commit. The archive API must accept a local transport directory and return a result without requiring provider credentials. `ArchiveResult.durability_confirmed` is true for `committed` and for `no_op` only after the existing manifest/shards are read back and match the incoming canonical content; storage exceptions and rejected/conflict results are never durable success.

**TDD sequence:**

- [ ] Write these exact failing tests using `tempfile.TemporaryDirectory`, `git init`, and subprocess calls against disposable repositories:
  - `ArchiveGitTests.test_branch_bootstrap_is_orphan_and_evidence_only`
  - `ArchiveGitTests.test_bootstrap_is_one_time_and_refuses_recreation`
  - `ArchiveGitTests.test_missing_evidence_branch_returns_evidence_branch_missing_without_auto_create`
  - `ArchiveGitTests.test_archive_validates_full_batch_before_one_commit`
  - `ArchiveGitTests.test_archive_never_leaves_partial_canonical_batch`
  - `ArchiveGitTests.test_identical_batch_retry_returns_no_op_without_second_manifest`
  - `ArchiveGitTests.test_no_op_is_operational_only_without_historical_record`
  - `ArchiveGitTests.test_identical_retry_does_not_overwrite_committed_manifest`
  - `ArchiveGitTests.test_conflict_transaction_identity_is_derived_from_conflict_entries`
  - `ArchiveGitTests.test_divergent_same_identity_writes_conflict_only_transaction`
  - `ArchiveGitTests.test_push_retries_fast_forward_at_most_three_times_without_force_or_merge`
  - `GitIsolationTests.test_bootstrap_archive_in_dedicated_clone_leaves_main_head_worktree_index_unchanged`
  - `CorporateArchiveConflictTests.test_existing_canonical_overlap_revision_creates_conflict_transaction`
  - `CorporateArchiveConflictTests.test_equal_overlap_against_canonical_evidence_is_accepted`
- [ ] Run the first test as RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests.test_branch_bootstrap_is_orphan_and_evidence_only`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_archive'`.
- [ ] Implement explicit bootstrap in a dedicated temporary clone/worktree using Git commands scoped to that disposable directory: create the orphan `advisor-evidence` root, write only `evidence/branch-schema.json`, commit the root, and verify no `main` ancestor or financial evidence. Runtime archive code must return `evidence_branch_missing` with nonzero CLI status when the branch is absent; it must not invoke bootstrap.
- [ ] Implement transport staging in a separate temporary directory inside the dedicated clone/worktree, validate all envelopes, paths, gzip members with `strict_json_loads_bytes` after decompression, hashes, identities, batch completeness, provider assignments, and corporate-action overlaps against canonical shards before adding any canonical file. Write one deterministic manifest for a valid batch, stage all shards and the manifest, and create one commit. On any validation error, remove only the disposable staging/clone directories and leave the caller branch unchanged.
- [ ] Derive `batch_identity` from the canonical batch identity, never from a transient run ID. On retry, inspect the existing manifest and all referenced shards before returning operational `no_op`; do not write a second manifest, no `no_op` evidence record, or any rewrite of the committed one. For divergent canonical content, write a conflict-only transaction whose identity is derived from sorted conflict entries and whose paths are under `conflicts/`.
- [ ] Implement fast-forward-only publication with a hard maximum of three push attempts. Re-read/fetch before each retry, reject non-fast-forward/merge-required states, never pass `--force` or `--force-with-lease`, and surface the final failure without altering the canonical branch.
- [ ] Implement `CorporateArchiveConflictTests` against a seeded canonical `corporate-actions` shard in the dedicated clone: compare incoming snapshots only in `EvidenceArchive`, using same provider plus symbol plus overlapping coverage; differing presence/effective date/split ratio creates `corporate_action_revision_conflict` and publishes only a conflict transaction, while equal normalized overlap remains publishable/idempotent. The collector does not perform this historical comparison.
- [ ] Capture caller `HEAD`, current branch, `git ls-files -s` index output, tracked/untracked status, and bytes of an unrelated file before bootstrap/archive; assert all values are unchanged afterward in `GitIsolationTests`. The disposable clone/worktree is the only location allowed to switch or create the orphan branch.
- [ ] Run the targeted archive tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests`
- [ ] Run the evidence schema and frozen append-only regression subset:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests tests.test_signal_observation tests.test_signal_outcome`
- [ ] Commit the task result as `feat: add append-only evidence archive` after checking the staged diff and confirming no branch was created in the real repository.

**Acceptance:** The bootstrap root is orphan/evidence-only; missing runtime branch is a nonzero `evidence_branch_missing`; valid batches have one atomic commit; invalid batches have no canonical partials; identical retries return `no_op` without another manifest; divergence produces an independent conflict transaction; publication is bounded, fast-forward-only, and non-force.

## Task 3 — Atomic observation source binding and signal-basis integration

**Reviewer boundary:** A reviewer can approve or reject construction-time source binding and sidecar integration without changing the frozen `SignalObservation` schema or report decision/scoring/risk/rendering behavior.

**Files:** Modify only `advisor/evidence_schema.py` and `advisor/cli.py`; extend `tests/test_evidence_accumulation.py`. Inspect and regression-test the existing claim propagation in `advisor/models.py`, `advisor/data_sources.py`, `advisor/data_pipeline.py`, and `advisor/live_loader.py` without modifying those files. Do not modify `advisor/evidence_archive.py` or protected modules.

The historical experimental Task 3 commits `4f063c5c500f87635f6f8fe7c3a5b56c01179518`, `0955da594db17f1fafe355ee3f518bf968f2ca11`, and `33481cbdb912bb45ddd7b3d7c736f464463e633c` are not reverted in this plan. The amended implementation replaces their superseded sidecar/binding behavior in a new correction commit and treats the frozen spec as authority.

The frozen observation contract remains read-only.

The old experimental path is **SUPERSEDED**: it built O, retained only `SignalObservation`, and later looked up `snapshots_by_symbol`. The approved path builds O and B in one iteration from the exact same snapshot X. The sidecar builder never receives a snapshot and never performs a symbol lookup.

`snapshots_by_symbol` is allowed only as the already-associated collection used
by the report run: the atomic builder selects its exact X once inside the same
decision iteration. It is not passed to `build_observation_sidecar`, and no
later lookup may rejoin an existing O to a snapshot by symbol.

**Exact interfaces in `advisor/evidence_schema.py`:**

```python
@dataclass(frozen=True)
class ObservationSourceBinding:
    signal_id: str
    observation_hash: str
    snapshot_sha256: str
    price_basis_claim: PriceBasisClaim | None

@dataclass(frozen=True)
class ObservationEvidenceRecord:
    observation: SignalObservation
    source_binding: ObservationSourceBinding

def snapshot_projection_v1(snapshot: AssetSnapshot) -> Mapping[str, object]:
    pass

def snapshot_sha256_v1(snapshot: AssetSnapshot) -> str:
    pass
```

```python
def _build_signal_observation_records(
    decisions: Sequence[AssetDecision],
    *,
    snapshots_by_symbol: Mapping[str, AssetSnapshot],
    stock_regime: str,
    crypto_regime: str,
    run_metadata: SignalRunMetadata,
) -> list[ObservationEvidenceRecord]:
    pass

def _persist_signal_observations(
    cache: SQLiteCache,
    observations: Sequence[SignalObservation],
) -> str:
    pass
```

The sidecar key is exactly `(signal_id, observation_hash)`. It carries explicit sanitized semantic provenance, the price-basis claim, source snapshot identity, and the sidecar schema version. `verified_raw_ohlcv` is emitted only when `AssetSnapshot.data_fetch_metadata.price_basis_claim` contains `price_basis="raw_ohlcv"`, `price_basis_policy_version="price_basis_v1"`, and one of the three qualified source-contract IDs from the call-graph finding. Absence, partial fields, an unqualified source contract, or ambiguous provenance emits `signal_basis_unavailable`. No code infers raw basis from a provider name, a fallback label, `market_data_kind`, or a `payload_sha256`.

The qualified source-contract propagation already exists in the current
experimental history and is regression-only for this amendment. Inspect that
FMP-full, Binance-klines, and Hyperliquid-candleSnapshot routes, their exact
cache-key checks, and the preservation through DataFetchMetadata. Do not
modify models.py, data_sources.py, data_pipeline.py, or live_loader.py here.
Provider name, namespace, fallback label, market_data_kind, and payload hash
alone never qualify raw OHLCV.

### Superseded tests

The following tests from the experimental sidecar contract are not active
acceptance criteria and must be removed or rewritten during the amended Task 3:

- `test_same_symbol_different_snapshot_cannot_verify_basis` is rewritten as an
  atomic-construction test; it must not claim that a resolver can authenticate a
  coherent pre-archive rewrite from an observation alone.
- `test_allowlisted_source_contract_tampering_invalidates_basis_binding` is
  rewritten as a captured-record/archived-authority test; allowlist membership
  alone is not historical provenance.

The replacement is not a coherent-tamper rejection requirement for an
unarchived sidecar. Durable divergence is tested exclusively by Task 6 after
the first canonical archive confirmation.

### TDD sequence

No production edit is allowed before RED. Add these exact architectural tests:

- [ ] ObservationSourceBindingTests.test_atomic_record_captures_observation_and_binding_from_same_snapshot
- [ ] ObservationSourceBindingTests.test_sidecar_builder_accepts_records_without_snapshots_by_symbol
- [ ] ObservationSourceBindingTests.test_binding_captures_exact_price_basis_claim
- [ ] SnapshotProjectionTests.test_snapshot_projection_v1_matches_literal_expected_projection
- [ ] SnapshotProjectionTests.test_snapshot_projection_v1_is_closed_world
- [ ] ObservationSourceBindingTests.test_mutating_snapshot_state_after_record_construction_does_not_change_binding
- [ ] ObservationSourceBindingTests.test_sqlite_failure_preserves_prebuilt_binding
- [ ] ObservationSourceBindingTests.test_sidecar_serializes_prebuilt_binding_without_snapshot_recomputation
- [ ] ObservationSourceBindingTests.test_same_record_serializes_deterministically

### Implementation steps (2–5 minutes each)

- [ ] Record RED evidence for each named Task 3 test before touching the amended production path; stop if a RED is caused by an invalid fixture or an interface mismatch.
- [ ] Add the two frozen dataclasses and their imports without adding fields to SignalObservation; run the atomic-record and exact-claim tests in isolation.
- [ ] Add the explicit snapshot projection helpers with the top-level and nested matrices below; run the literal-projection test before wiring the sidecar.
- [ ] Add the closed-world test-only extended AssetSnapshot fixture and verify that its synthetic future_field is absent from the v1 projection.
- [ ] Replace the sidecar serializer/parser/resolver boundary with records-only input; run the records-only, prebuilt-binding, and deterministic-serialization tests.
- [ ] Replace the CLI observation construction boundary with the same-iteration O+B loop; run the mutation-after-construction and construction-failure tests.
- [ ] Derive the SQLite observation tuple only after the complete record list exists; run the SQLite-failure preservation test.
- [ ] Run the amended Task 3 targeted tests, then Task 1, Task 2, and frozen observation/outcome regressions before requesting the independent Task 3 review.

### Closed-world snapshot projection

The implementation must copy every current AssetSnapshot field explicitly. The
hashed object has exactly projection_version=snapshot_projection_v1 plus the
34 fields below. Every row has Action exactly INCLUDE; no field is conditional
and no current field is EXCLUDE.

| AssetSnapshot field | Action | Canonical representation | Rationale |
| --- | --- | --- | --- |
| `symbol` | INCLUDE | string literal | identity and decision input |
| `asset_type` | INCLUDE | string literal | decision and market policy input |
| `theme` | INCLUDE | string literal | scoring and benchmark context |
| `candles` | INCLUDE | array of Candle projections, preserving order | technical decision input |
| `fundamentals` | INCLUDE | Fundamentals projection | valuation and quality input |
| `event` | INCLUDE | EventInfo projection or JSON `null` | earnings/event input |
| `funding_rate` | INCLUDE | JSON number or `null` | crypto decision input |
| `open_interest_change` | INCLUDE | JSON number or `null` | crypto decision input |
| `cvd_proxy` | INCLUDE | JSON number or `null` | crypto decision input |
| `coinbase_premium` | INCLUDE | JSON number or `null` | crypto decision input |
| `liquidation_imbalance` | INCLUDE | JSON number or `null` | crypto decision input |
| `missing_data` | INCLUDE | array of strings, preserving order | data-quality and limitation input |
| `news_events` | INCLUDE | array of JSON objects with string keys, preserving order | news decision and report input |
| `provider_capabilities` | INCLUDE | array of ProviderCapability projections, preserving order | provider availability and fallback context |
| `earnings_status` | INCLUDE | string literal | report/data-quality state |
| `guidance_status` | INCLUDE | string literal | report/data-quality state |
| `macro_status` | INCLUDE | string literal | report/data-quality state |
| `news_status` | INCLUDE | string literal | report/data-quality state |
| `sec_filings_status` | INCLUDE | string literal | report/data-quality state |
| `data_source` | INCLUDE | string literal | source used by the snapshot |
| `data_timestamp` | INCLUDE | string or JSON `null` | source/decision timing |
| `cache_age_seconds` | INCLUDE | integer or JSON `null` | affects stale classification and report |
| `data_fetch_metadata` | INCLUDE | DataFetchMetadata projection or JSON `null` | source provenance, freshness and claim |
| `quote_status` | INCLUDE | string literal | quote/report state |
| `quote_price` | INCLUDE | JSON number or `null` | quote/report input |
| `quote_timestamp` | INCLUDE | string or JSON `null` | quote/report timing |
| `quote_source` | INCLUDE | string or JSON `null` | quote source provenance |
| `quote_age_seconds` | INCLUDE | integer or JSON `null` | quote freshness/report state |
| `quote_is_intraday` | INCLUDE | JSON boolean | quote basis/report state |
| `previous_close` | INCLUDE | JSON number or `null` | market/quote context |
| `daily_change` | INCLUDE | JSON number or `null` | market/quote context |
| `daily_change_pct` | INCLUDE | JSON number or `null` | market/quote context |
| `benchmark_provenance` | INCLUDE | JSON mapping projection | benchmark/report provenance |
| `crypto_metric_provenance` | INCLUDE | nested JSON mapping projection | crypto source provenance |

The nested matrices are closed as follows. Every row is either INCLUDE or
EXCLUDE; all current nested fields are explicitly INCLUDE.

**Candle**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `date` | INCLUDE | string literal |
| `open` | INCLUDE | JSON number from the model's `float` |
| `high` | INCLUDE | JSON number from the model's `float` |
| `low` | INCLUDE | JSON number from the model's `float` |
| `close` | INCLUDE | JSON number from the model's `float` |
| `volume` | INCLUDE | JSON number from the model's `float` |

**Fundamentals**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `pe` | INCLUDE | JSON number or `null` |
| `peg` | INCLUDE | JSON number or `null` |
| `historical_pe` | INCLUDE | JSON number or `null` |
| `revenue_growth` | INCLUDE | JSON number or `null` |
| `eps_growth` | INCLUDE | JSON number or `null` |
| `margin_trend` | INCLUDE | JSON number or `null` |
| `free_cash_flow_positive` | INCLUDE | JSON boolean or `null` |
| `market_cap` | INCLUDE | JSON number or `null` |
| `average_volume` | INCLUDE | JSON number or `null` |
| `market_cap_rank` | INCLUDE | JSON integer or `null` |

**EventInfo**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `days_to_earnings` | INCLUDE | JSON integer or `null` |
| `guidance_recent` | INCLUDE | JSON boolean or `null` |
| `post_earnings_gap_percent` | INCLUDE | JSON number or `null` |
| `last_earnings_date` | INCLUDE | string or `null` |
| `next_earnings_date` | INCLUDE | string or `null` |

**ProviderCapability**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `provider` | INCLUDE | string literal |
| `capability` | INCLUDE | string literal |
| `configured` | INCLUDE | JSON boolean |
| `supported_by_plan` | INCLUDE | JSON boolean |
| `implemented` | INCLUDE | JSON boolean |
| `last_status` | INCLUDE | string literal |
| `fallback_available` | INCLUDE | JSON boolean |

**DataFetchMetadata**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `provider` | INCLUDE | string literal |
| `endpoint` | INCLUDE | string literal |
| `fetched_at` | INCLUDE | string or JSON `null` |
| `cache_fetched_at` | INCLUDE | string or JSON `null` |
| `source_timestamp` | INCLUDE | string or JSON `null` |
| `cache_age_seconds` | INCLUDE | JSON integer or `null` |
| `source_age_seconds` | INCLUDE | JSON integer or `null` |
| `is_fresh` | INCLUDE | JSON boolean or `null` |
| `cache_hit` | INCLUDE | JSON boolean |
| `fallback_used` | INCLUDE | JSON boolean |
| `fallback_from` | INCLUDE | string or JSON `null` |
| `fallback_to` | INCLUDE | string or JSON `null` |
| `granularity` | INCLUDE | string or JSON `null` |
| `market_data_kind` | INCLUDE | string or JSON `null` |
| `price_basis_claim` | INCLUDE | PriceBasisClaim projection or JSON `null` |

**PriceBasisClaim**

| Field | Action | Canonical representation |
| --- | --- | --- |
| `price_basis` | INCLUDE | underlying Literal string value |
| `price_basis_policy_version` | INCLUDE | underlying Literal string value |
| `source_contract` | INCLUDE | string literal |

`DataFetchMetadata.price_basis_claim` MUST be INCLUDE. The key is always
present in its projection and is JSON `null` when the claim is absent. Changing
`price_basis`, `price_basis_policy_version`, or `source_contract` changes the
projection and therefore `snapshot_sha256_v1`. There is no current nested
EXCLUDE field.

The cache, fetch-timing, fallback, and capability names may look operational,
but in the current model they are captured source state: `cache_age_seconds`
participates in stale classification, while the remaining fields are carried
into provenance, report state, or fallback-path descriptions. The current
AssetSnapshot and DataFetchMetadata definitions contain no runner retry state,
publication timestamp, artifact path, process ID, exception diagnostic, or
other exclusively transport-only field. If such a field is added in the
future, it must be named and classified in a new projection review rather than
silently excluded by category.

The mapping fields have these exact current types: `news_events` is
`list[dict[str, object]]`, `benchmark_provenance` is `dict[str, object]`, and
`crypto_metric_provenance` is `dict[str, dict[str, object]]`. Their keys
preserve mapping semantics and MUST be strings; `canonical_json_bytes` provides
deterministic object-key ordering. Their nested values are limited to JSON
`null`, boolean, integer, finite float, string, mapping, or array, and no value
is implicitly stringified. Arrays nested inside these mappings have
ORDER IS SEMANTIC and preserve the supplied order.

The current models contain no datetime, date, Decimal, or enum instance in
these fields. Dates/timestamps remain strings, Literal values use their
underlying strings, and float/int/bool retain JSON number/integer/boolean
types. Mapping keys are strings and canonical_json_bytes alone sorts object
keys. Arrays in candles, missing_data, news_events, provider_capabilities, and
nested provenance arrays have ORDER IS SEMANTIC and preserve input order. No
generic list sort, default=str, repr, Python hash(), object identity, runtime
timestamp, asdict, vars, __dict__, dataclass reflection, or automatic
recursive serialization is allowed. canonical_json_bytes remains the sole
canonical JSON algorithm with UTF-8, compact separators, ensure_ascii=False,
and allow_nan=False.

The complete normative pseudocode is:

```python
def json_value_v1(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non_finite_json_number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value.keys()):
            raise ValueError("non_string_json_object_key")
        return {key: json_value_v1(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value_v1(item) for item in value]
    raise ValueError("unsupported_snapshot_projection_value")

def price_basis_claim_projection_v1(value):
    if value is None:
        return None
    return {
        "price_basis": value.price_basis,
        "price_basis_policy_version": value.price_basis_policy_version,
        "source_contract": value.source_contract,
    }

def snapshot_projection_v1(snapshot):
    return {
        "projection_version": "snapshot_projection_v1",
        "symbol": snapshot.symbol,
        "asset_type": snapshot.asset_type,
        "theme": snapshot.theme,
        "candles": [
            {
                "date": value.date,
                "open": value.open,
                "high": value.high,
                "low": value.low,
                "close": value.close,
                "volume": value.volume,
            }
            for value in snapshot.candles
        ],
        "fundamentals": {
            "pe": snapshot.fundamentals.pe,
            "peg": snapshot.fundamentals.peg,
            "historical_pe": snapshot.fundamentals.historical_pe,
            "revenue_growth": snapshot.fundamentals.revenue_growth,
            "eps_growth": snapshot.fundamentals.eps_growth,
            "margin_trend": snapshot.fundamentals.margin_trend,
            "free_cash_flow_positive": snapshot.fundamentals.free_cash_flow_positive,
            "market_cap": snapshot.fundamentals.market_cap,
            "average_volume": snapshot.fundamentals.average_volume,
            "market_cap_rank": snapshot.fundamentals.market_cap_rank,
        },
        "event": None if snapshot.event is None else {
            "days_to_earnings": snapshot.event.days_to_earnings,
            "guidance_recent": snapshot.event.guidance_recent,
            "post_earnings_gap_percent": snapshot.event.post_earnings_gap_percent,
            "last_earnings_date": snapshot.event.last_earnings_date,
            "next_earnings_date": snapshot.event.next_earnings_date,
        },
        "funding_rate": snapshot.funding_rate,
        "open_interest_change": snapshot.open_interest_change,
        "cvd_proxy": snapshot.cvd_proxy,
        "coinbase_premium": snapshot.coinbase_premium,
        "liquidation_imbalance": snapshot.liquidation_imbalance,
        "missing_data": [value for value in snapshot.missing_data],
        "news_events": [json_value_v1(value) for value in snapshot.news_events],
        "provider_capabilities": [
            {
                "provider": value.provider,
                "capability": value.capability,
                "configured": value.configured,
                "supported_by_plan": value.supported_by_plan,
                "implemented": value.implemented,
                "last_status": value.last_status,
                "fallback_available": value.fallback_available,
            }
            for value in snapshot.provider_capabilities
        ],
        "earnings_status": snapshot.earnings_status,
        "guidance_status": snapshot.guidance_status,
        "macro_status": snapshot.macro_status,
        "news_status": snapshot.news_status,
        "sec_filings_status": snapshot.sec_filings_status,
        "data_source": snapshot.data_source,
        "data_timestamp": snapshot.data_timestamp,
        "cache_age_seconds": snapshot.cache_age_seconds,
        "data_fetch_metadata": (
            None if snapshot.data_fetch_metadata is None else {
                "provider": snapshot.data_fetch_metadata.provider,
                "endpoint": snapshot.data_fetch_metadata.endpoint,
                "fetched_at": snapshot.data_fetch_metadata.fetched_at,
                "cache_fetched_at": snapshot.data_fetch_metadata.cache_fetched_at,
                "source_timestamp": snapshot.data_fetch_metadata.source_timestamp,
                "cache_age_seconds": snapshot.data_fetch_metadata.cache_age_seconds,
                "source_age_seconds": snapshot.data_fetch_metadata.source_age_seconds,
                "is_fresh": snapshot.data_fetch_metadata.is_fresh,
                "cache_hit": snapshot.data_fetch_metadata.cache_hit,
                "fallback_used": snapshot.data_fetch_metadata.fallback_used,
                "fallback_from": snapshot.data_fetch_metadata.fallback_from,
                "fallback_to": snapshot.data_fetch_metadata.fallback_to,
                "granularity": snapshot.data_fetch_metadata.granularity,
                "market_data_kind": snapshot.data_fetch_metadata.market_data_kind,
                "price_basis_claim": price_basis_claim_projection_v1(
                    snapshot.data_fetch_metadata.price_basis_claim
                ),
            }
        ),
        "quote_status": snapshot.quote_status,
        "quote_price": snapshot.quote_price,
        "quote_timestamp": snapshot.quote_timestamp,
        "quote_source": snapshot.quote_source,
        "quote_age_seconds": snapshot.quote_age_seconds,
        "quote_is_intraday": snapshot.quote_is_intraday,
        "previous_close": snapshot.previous_close,
        "daily_change": snapshot.daily_change,
        "daily_change_pct": snapshot.daily_change_pct,
        "benchmark_provenance": json_value_v1(snapshot.benchmark_provenance),
        "crypto_metric_provenance": json_value_v1(
            snapshot.crypto_metric_provenance
        ),
    }

def snapshot_sha256_v1(snapshot):
    return hashlib.sha256(
        canonical_json_bytes(snapshot_projection_v1(snapshot))
    ).hexdigest()
```

The projection_version key is inside the hashed object. Adding a future
AssetSnapshot field does not add it to v1. Any inclusion, exclusion, rename,
or representation change requires explicit review, a new projection version
when historical bytes change, and a migration/compatibility decision.

The excluded-field list is empty:
`snapshot_projection_v1 excludes no current AssetSnapshot fields`. The nested
Candle, Fundamentals, EventInfo, ProviderCapability, DataFetchMetadata, and
PriceBasisClaim projections also exclude no current fields. Any future
transport-only or runtime field must be named and classified explicitly in a
future projection review; it is never covered by an open-ended exclusion rule.

Test E must instantiate the real nested model types and define a literal
expected_projection with all 34 top-level keys, all nested fields, the three
claim fields, explicit nulls, mapping values, and sequence order. It computes
SHA256(canonical_json_bytes(expected_projection)) and compares that digest with
record.source_binding.snapshot_sha256; it cannot use the production projection
helper as its only oracle. The fixture must include two ordered candles,
populated Fundamentals, EventInfo, DataFetchMetadata, ProviderCapability, an
FMP claim, missing_data, nested news/benchmark mappings, and an empty crypto
provenance mapping. It must kill omission of an INCLUDE field, addition of an
EXCLUDE field, source_contract mutation, sequence reordering, and automatic
future-field inclusion. A separate closed-world test adds future_field to a
structural input and asserts it is absent.

The literal Test E oracle is:

```python
expected_projection = {
    "projection_version": "snapshot_projection_v1",
    "symbol": "AMD",
    "asset_type": "stock",
    "theme": "semiconductors",
    "candles": [
        {
            "date": "2026-08-27",
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 1000000.0,
        },
        {
            "date": "2026-08-28",
            "open": 102.0,
            "high": 105.0,
            "low": 101.0,
            "close": 104.0,
            "volume": 1200000.0,
        },
    ],
    "fundamentals": {
        "pe": 30.0,
        "peg": 1.5,
        "historical_pe": 28.0,
        "revenue_growth": 0.2,
        "eps_growth": 0.25,
        "margin_trend": 0.1,
        "free_cash_flow_positive": True,
        "market_cap": 1000000000.0,
        "average_volume": 1100000.0,
        "market_cap_rank": 12,
    },
    "event": {
        "days_to_earnings": 5,
        "guidance_recent": False,
        "post_earnings_gap_percent": 0.02,
        "last_earnings_date": "2026-05-28",
        "next_earnings_date": "2026-09-01",
    },
    "funding_rate": None,
    "open_interest_change": None,
    "cvd_proxy": None,
    "coinbase_premium": None,
    "liquidation_imbalance": None,
    "missing_data": ["macro_not_collected", "news_not_collected"],
    "news_events": [
        {"headline": "confirmed catalyst", "news_event_type": "news"},
        {"headline": "SEC filing", "news_event_type": "sec_filing"},
    ],
    "provider_capabilities": [
        {
            "provider": "fmp",
            "capability": "historical_price_eod_full",
            "configured": True,
            "supported_by_plan": True,
            "implemented": True,
            "last_status": "available",
            "fallback_available": False,
        }
    ],
    "earnings_status": "available",
    "guidance_status": "not_implemented",
    "macro_status": "not_implemented",
    "news_status": "available",
    "sec_filings_status": "available",
    "data_source": "fmp",
    "data_timestamp": "2026-08-28T20:00:00Z",
    "cache_age_seconds": 0,
    "data_fetch_metadata": {
        "provider": "fmp",
        "endpoint": "/stable/historical-price-eod/full",
        "fetched_at": "2026-08-28T20:00:03Z",
        "cache_fetched_at": None,
        "source_timestamp": "2026-08-28",
        "cache_age_seconds": 0,
        "source_age_seconds": 0,
        "is_fresh": True,
        "cache_hit": False,
        "fallback_used": False,
        "fallback_from": None,
        "fallback_to": None,
        "granularity": "1d",
        "market_data_kind": "historical",
        "price_basis_claim": {
            "price_basis": "raw_ohlcv",
            "price_basis_policy_version": "price_basis_v1",
            "source_contract": "fmp.historical_price_eod.full.raw_ohlcv_v1",
        },
    },
    "quote_status": "available",
    "quote_price": 104.0,
    "quote_timestamp": "2026-08-28T20:00:04Z",
    "quote_source": "fmp",
    "quote_age_seconds": 2,
    "quote_is_intraday": False,
    "previous_close": 102.0,
    "daily_change": 2.0,
    "daily_change_pct": 0.0196078431372549,
    "benchmark_provenance": {
        "sector": {
            "symbol": "SMH",
            "status": "available",
            "relative_strength": 0.12,
        }
    },
    "crypto_metric_provenance": {},
}
expected_sha256 = hashlib.sha256(
    canonical_json_bytes(expected_projection)
).hexdigest()
self.assertEqual(
    record.source_binding.snapshot_sha256,
    expected_sha256,
)
```

The test must also assert that reversing the two candles changes the digest
and that replacing only the claim source_contract changes the digest. The
closed-world test uses a local `@dataclass(frozen=True)` subclass of
`AssetSnapshot` with one additional `future_field = "sentinel"`; it passes that
subclass to `snapshot_projection_v1` and asserts the result is byte-identical
to the base fixture's projection and contains no `future_field`. This test-only
subclass changes no production model and kills `asdict`, `vars`, `__dict__`,
dataclass-field reflection, and automatic future-field expansion.

### Atomic construction and sidecar authority

For every decision, the CLI uses the exact snapshot X that fed the decision
inputs and performs this sequence before advancing:

```python
records = []
for decision in decisions:
    snapshot_X = snapshots_by_symbol[decision.symbol]
    observation_O = build_signal_observation(
        decision, snapshot_X, run_metadata,
        stock_regime=stock_regime, crypto_regime=crypto_regime,
    )
    binding_B = ObservationSourceBinding(
        signal_id=observation_O.signal_id,
        observation_hash=observation_O.observation_hash,
        snapshot_sha256=snapshot_sha256_v1(snapshot_X),
        price_basis_claim=(
            None if snapshot_X.data_fetch_metadata is None
            else snapshot_X.data_fetch_metadata.price_basis_claim
        ),
    )
    records.append(ObservationEvidenceRecord(observation_O, binding_B))
```

The function returns only after every record is structurally valid and
serializable. If record N fails, there is no partial sidecar, synthetic
binding, alternate snapshot, or provider reload. After the complete list:

```python
observations = tuple(record.observation for record in records)
_persist_signal_observations(cache, observations)
build_observation_sidecar(records, output_path=sidecar_path)
```

SQLite receives O only; the sidecar receives O+B. SQLite failure does not
discard or mutate B. The sidecar top level is exactly schema_version,
source_sha, run_id, report_type, and records. Each item contains only the
complete frozen observation and source_binding with signal_id,
observation_hash, snapshot_sha256, and price_basis_claim (JSON null or the
complete three-field object). All top-level metadata values agree. There are
no parallel arrays and no post-construction symbol rejoin.

The legacy signal_input_hash, snapshot_binding, and entry_integrity_sha256
fields are removed from the serializer/parser/resolver path; they are not
compatibility fields and do not form a second checksum authority. The resolver
accepts the typed record and validates schema/version, observation and binding
IDs/hashes, digest format, claim shape, raw_ohlcv, price_basis_v1, and the
exact qualified source-contract allowlist. Missing, malformed, duplicate, or
ambiguous entries return signal_basis_unavailable. It does not reconstruct X
from O. An unarchived sidecar is transport only; archive confirmation creates
durable authority.

**Acceptance:** Exact decision-time X produces immutable O+B; the records-only
builder serializes prebuilt binding; snapshot_projection_v1 is explicit,
versioned, closed-world, and deterministic; SQLite receives O only after the
complete record list; construction failure emits no sidecar; SQLite failure
preserves B; current providers do not reconstruct history; and the frozen
observation, decision, scoring, risk, rendering, and fallback behavior remains
unchanged.

### Task 3 review gate

- [ ] Run all amended Task 3 targeted tests, including the literal projection,
  closed-world, atomic-record, sidecar, and SQLite-failure tests.
- [ ] Run the frozen Task 1 canonical serialization/idempotency regression and
  the frozen Task 2 archive/isolation/provider-reader regression.
- [ ] Run the frozen SignalObservation and SignalForwardOutcome regressions and
  confirm decision, scoring, risk, rendering, and fallback bytes are unchanged.
- [ ] Use these exact frozen-regression commands at the gate:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests tests.test_evidence_accumulation.SharedEvidenceTypeContractTests`
  and
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests tests.test_evidence_accumulation.GitIsolationTests tests.test_evidence_accumulation.CorporateArchiveConflictTests tests.test_evidence_accumulation.CanonicalProviderReaderTests`
  followed by
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome`.
- [ ] Obtain an independent bounded Task 3 review. Do not mark Task 3 complete
  or begin Task 4 while a CRITICAL or IMPORTANT finding remains.

The corrected Task 3 diff is expected to be limited to `advisor/evidence_schema.py`,
`advisor/cli.py`, and `tests/test_evidence_accumulation.py`. If the already
present claim propagation fails regression, stop with a scoped design conflict;
do not expand the task into `advisor/models.py`, `advisor/data_sources.py`,
`advisor/data_pipeline.py`, or `advisor/live_loader.py` without a separately
approved scope decision.

## Task 4 — Deterministic market/provider and corporate-action collection

**Reviewer boundary:** A reviewer can approve or reject assigned-provider determinism, sticky recovery, no silent fallback, session coverage, and Alpha Vantage split normalization without reviewing maturation.

**Files:** Create/extend `advisor/evidence_collector.py` and `tests/test_evidence_accumulation.py`. The collector consumes the explicit source-contract methods from `advisor/data_sources.py` and does not modify loader/report behavior.

**Interfaces to implement:** `CollectionAsset`, `PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION`, `assigned_price_provider`, `EvidenceCollector.collect_market`, `EvidenceCollector.collect_corporate_actions`, and `normalize_split_factor`. The collector writes transport only and has no Git or SQLite authority. Before collection, the read-only collector job calls `oldest_canonical_provider_by_symbol(evidence_checkout=evidence_checkout, symbols=tuple(asset.symbol for asset in assets))` for the typed asset list and passes the returned mapping to `collect_market`; a missing symbol key means the deterministic v1 mapping applies.

**TDD sequence:**

Task 4 does not construct, infer, or repair ObservationSourceBinding. That
binding captures the source contract actually used by the decision snapshot in
Task 3 and does not execute price_provider_assignment_v1.

- [ ] Write these exact failing tests:
  - `ProviderAssignmentTests.test_price_provider_assignment_v1_is_deterministic`
  - `ProviderAssignmentTests.test_oldest_canonical_provider_is_sticky_for_existing_series`
  - `ProviderAssignmentTests.test_unknown_or_unsupported_symbol_is_market_data_unavailable`
  - `ProviderAssignmentTests.test_assigned_provider_unavailable_does_not_fallback`
  - `ProviderAssignmentTests.test_fmp_unavailable_does_not_call_yahoo_as_evidence_fallback`
  - `CorporateActionCollectionTests.test_alpha_vantage_splits_fixture_normalizes_decimal_split_factor`
  - `CorporateActionCollectionTests.test_aapl_nvda_tsla_and_igv_split_fixtures_are_supported`
  - `CorporateActionCollectionTests.test_reverse_split_point_two_five_normalizes_to_one_over_four`
  - `CorporateActionCollectionTests.test_collector_emits_windowed_transport_without_history_comparison`
  - `SessionCompletenessTests.test_weekend_stock_date_is_rejected`
  - `SessionCompletenessTests.test_us_market_holiday_is_rejected`
  - `SessionCompletenessTests.test_valid_early_close_session_is_accepted`
  - `SessionCompletenessTests.test_current_incomplete_us_session_candle_is_not_archived`
  - `SessionCompletenessTests.test_synthetic_fill_is_rejected`
  - `SessionCompletenessTests.test_current_utc_crypto_day_candle_is_rejected`
  - `SessionCompletenessTests.test_completed_prior_utc_crypto_candle_is_accepted`
  - `SessionCompletenessTests.test_us_eastern_dst_before_march_transition`
  - `SessionCompletenessTests.test_us_eastern_dst_after_march_transition`
  - `SessionCompletenessTests.test_us_eastern_dst_before_november_transition`
  - `SessionCompletenessTests.test_us_eastern_dst_after_november_transition`
  - `SessionCompletenessTests.test_regular_close_in_est`
  - `SessionCompletenessTests.test_regular_close_in_edt`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ProviderAssignmentTests.test_price_provider_assignment_v1_is_deterministic`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_collector'`.
- [ ] Implement the typed `CollectionAsset(symbol: str, asset_type: Literal["stock", "etf", "crypto"])` input. Reject an unknown asset type before any provider request. Implement the literal mapping:
  - `stock -> fmp`;
  - `etf -> fmp`;
  - `crypto:HYPE -> hyperliquid`;
  - every other supported crypto -> `binance`.
  Include the policy version in semantic provenance. For existing series, the collector job first reads the oldest canonical market evidence through `oldest_canonical_provider_by_symbol` from its read-only evidence checkout, then passes that provider mapping into `collect_market`. If that assigned provider cannot answer, emit `market_data_unavailable` and make no second-provider request. Do not infer asset type from a provider attempt and do not call the existing Yahoo/CoinGecko/Alpha fallback chain from the collector. Business logic stays in Python interfaces, not YAML.
- [ ] Keep `etf` as an evidence-collector classification for benchmark market evidence only. The collector may archive its typed market transport, but the materializer must reject it before constructing `ForwardMarketSeries`; frozen 3B.2 receives only the existing `stock` or `crypto` asset types and never receives `asset_type="etf"`.
- [ ] Implement daily market transport with explicit symbol, asset type, interval, date, timezone, assigned provider, the qualified versioned raw-OHLCV basis claim, source request identity, and coverage window. Use the repository’s existing symbol support lists as inputs; unsupported symbols produce the unavailable status rather than dynamic provider selection.
- [ ] Implement Alpha Vantage `SPLITS` fixture-compatible parsing with `Decimal` from the real `split_factor` field and return `CanonicalSplitRatio(new_shares: str, old_shares: str)`. Normalize AAPL 4:1, NVDA 10:1, TSLA 5:1, IGV 5:1, and reverse split `0.25 -> 1/4`. Network tests are opt-in and never run in the deterministic default suite. The collector emits coverage-windowed transport and performs no comparison with canonical history.
- [ ] Implement deterministic session completeness through the local `us_equities_session_v1` helpers in `advisor/evidence_collector.py`: require aware UTC inputs; use the second Sunday in March at 07:00 UTC and the first Sunday in November at 06:00 UTC to select fixed EST/EDT offsets; convert the applicable 16:00 regular or 13:00 early close locally; reject weekends, the explicit observed US market-holiday set, and synthetic/forward-filled rows. Reject Saturday `2026-08-29`, reject US holiday `2026-09-07`, accept a completed `2026-11-27` early-close session after 13:00 local, reject and do not archive a current regular-session candle before 16:00 local, reject a synthetic/forward-filled row, reject crypto row date equal to the current UTC date, and accept a completed prior UTC date. These checks use only deterministic local helpers and never use network or a machine-installed timezone database.
- [ ] Make the six boundary tests use fixed aware UTC values: `2026-03-08T06:59:00Z` is EST and `2026-03-08T07:00:00Z` is EDT; `2026-11-01T05:59:00Z` is EDT and `2026-11-01T06:00:00Z` is EST; `2026-01-05T21:00:00Z` is the 16:00 EST regular close and `2026-07-06T20:00:00Z` is the 16:00 EDT regular close. Assert the local offset/close directly without consulting host-local time settings.
- [ ] Run targeted provider/corporate/session tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ProviderAssignmentTests tests.test_evidence_accumulation.CorporateActionCollectionTests tests.test_evidence_accumulation.SessionCompletenessTests`
- [ ] Run existing data-source and live-loader regressions to prove report fallback behavior was not altered:
  `.\.venv\Scripts\python.exe -m unittest tests.test_data_sources tests.test_live_loader`
- [ ] Commit the task result as `feat: add deterministic evidence collectors` after scope and diff checks.

**Acceptance:** Provider assignment is typed, versioned, deterministic, sticky, and fail-closed; FMP unavailability makes no Yahoo evidence request; oldest canonical provider is read before collection; market transport is raw/provenance-qualified; `split_factor` fixtures use exact decimal `new_shares/old_shares`; session completeness rejects non-sessions, incomplete candles, and synthetic fills; historical corporate overlap comparison is left to the archive.

## Task 5 — Canonical materialization and horizon qualification

**Reviewer boundary:** A reviewer can approve or reject canonical-only reads, basis gating, inclusive split windows, corporate-conflict blocking, feed-unavailable handling, and proof statuses without reviewing GitHub Actions.

**Files:** Create/extend `advisor/evidence_materializer.py` and `tests/test_evidence_accumulation.py`.

The frozen observation and outcome contracts remain read-only.

Task 5 reads one canonical ObservationEvidenceRecord from the freshly fetched
evidence checkout by the exact (signal_id, observation_hash) key. It consumes
the archived source_binding.snapshot_sha256 and the exact captured
price_basis_claim as provenance; it never reconstructs historical binding from
a current provider, current market data, a symbol lookup, a current snapshot,
or SQLite. The materializer may validate the captured record, but it may not
recompute an old snapshot digest from a future model version.

**Interfaces to implement:** `EvidenceMaterializer`, `read_observation_evidence_record`, `HorizonQualification`, `MaterializationResult`, `qualify_horizon`, and `resolve_signal_price_basis_status(record=record)` imported from `advisor.evidence_schema`. `HorizonQualification.status` is a `HorizonStatus`; its `policy` is a separate `SplitPolicy | CryptoPolicy | None`; and its `reason_code` is a separate `CorporateActionReasonCode | None`. The materializer accepts only an evidence checkout and DB target; it has no API for a transport directory.

If the canonical record or its source binding is missing or invalid, Task 5
returns `signal_basis_unavailable` for the stock/ETF horizon and does not
construct a 3B.2 input. A qualified claim is accepted only from the archived
record's captured binding; provider-name inference and current-snapshot lookup
are forbidden.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `HorizonQualificationTests.test_unknown_signal_basis_blocks_stock_maturation_before_3b2`
  - `HorizonQualificationTests.test_verified_raw_ohlcv_is_required_for_stock_verified_none`
  - `HorizonQualificationTests.test_split_on_signal_market_date_blocks_horizon`
  - `HorizonQualificationTests.test_split_on_horizon_end_date_blocks_horizon`
  - `HorizonQualificationTests.test_split_after_h5_before_h10_blocks_only_h10_and_later`
  - `HorizonQualificationTests.test_no_split_produces_verified_none_with_split_policy`
  - `HorizonQualificationTests.test_status_and_proof_policy_are_distinct`
  - `HorizonQualificationTests.test_feed_unavailable_is_not_verified_none`
  - `HorizonQualificationTests.test_relevant_corporate_action_conflict_blocks_new_verified_none`
  - `HorizonQualificationTests.test_frozen_outcome_is_not_rewritten_by_new_corporate_revision`
  - `HorizonQualificationTests.test_etf_evidence_never_enters_frozen_3b2_asset_types`
  - `HorizonQualificationTests.test_materializer_has_no_unarchived_transport_input`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.HorizonQualificationTests.test_unknown_signal_basis_blocks_stock_maturation_before_3b2`
  Expected failure: `ModuleNotFoundError: No module named 'advisor.evidence_materializer'`.
- [ ] Implement canonical checkout readers that validate branch schema, path partitions, gzip members, envelope hashes, logical identities, and manifest references before exposing records. Reconstruct observations from observation shards and market/corporate data from their canonical shards. Ignore SQLite rows and `signal_journal` as source authority.
- [ ] Implement the exact status/reason/policy separation for each horizon: `market_data_unavailable`, `pending`, `conflict` with reason `corporate_action_revision_conflict`, `signal_basis_unavailable` for stock/ETF, `feed_unavailable`, `split_in_horizon_unavailable`, `verified_none`, and `not_applicable`. No policy literal is stored as a status, and no feed-unavailable case may produce a `verified_none` proof.
- [ ] Implement split evaluation over `[signal_market_date, horizon_end_date]`, including both endpoints. A split after h5 and before h10 gives h10 and later `split_in_horizon_unavailable` while leaving h5 governed by its own interval. A no-split stock/ETF horizon returns `qualification.status="verified_none"` and `qualification.proof["corporate_action_policy"]="verified_no_split_in_signal_horizon_v1"`; the status and proof policy are asserted separately. A crypto horizon returns `status="not_applicable"` with `policy="not_applicable_crypto_raw_ohlcv_v1"` where the frozen contract permits crypto evaluation.
- [ ] Before any frozen evaluator call, resolve sidecar basis by `(signal_id, observation_hash)`. Unknown basis returns the blocked qualification and must not construct a 3B.2 input for that stock horizon.
- [ ] In `HorizonQualificationTests.test_unknown_signal_basis_blocks_stock_maturation_before_3b2`, patch `advisor.signal_outcome.evaluate_signal_observation` and assert call count `0`, no frozen input is built for that stock horizon, and the qualification remains `signal_basis_unavailable`.
- [ ] Keep `etf` available only as a collector/evidence classification for benchmark evidence. Before constructing `ForwardMarketSeries` or calling frozen 3B.2, accept only the existing frozen asset types `stock` and `crypto`; an ETF record is excluded and never passed with `asset_type="etf"`.
- [ ] Read relevant `corporate_action_revision_conflict` entries from canonical conflict transactions. A seeded canonical conflict blocks a new `verified_none` proof for the overlapping interval, while any already frozen outcome remains unchanged; v1 records the conflict for audit and performs no reevaluation.
- [ ] Run targeted materializer tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.HorizonQualificationTests`
- [ ] Run frozen observation/outcome and predictive contract regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome tests.test_predictive_evaluation tests.test_predictive_statistics`
- [ ] Commit the task result as `feat: qualify canonical evidence horizons` after `git diff --check` and protected-file scope inspection.

**Acceptance:** The materializer reads only canonical evidence, separates `HorizonStatus`, reason code, and policy, recognizes all required blocked statuses, applies the exact inclusive split policy, blocks unknown basis and relevant corporate conflicts, preserves frozen outcomes, and never treats unavailable feed as verified absence.

## Task 6 — Frozen 3B.2 outcome maturation and second archive

**Reviewer boundary:** A reviewer can approve or reject the authority boundary between evidence qualification and frozen outcome evaluation, including call tracing and pending-state persistence.

**Files:** Modify only `advisor/evidence_materializer.py` and `advisor/cli.py` within their new integration surfaces; extend `tests/test_evidence_accumulation.py`.

The frozen outcome, scoring, and risk contracts remain read-only.

Task 6 owns the transport-to-durable-authority transition. The first archive
confirmation, not a local sidecar write and not an SQLite save, makes the
observation record authoritative in advisor-evidence. The exact ownership test
is ArchiveAuthorityTransitionTests.test_different_source_binding_for_same_observation_conflicts_after_first_archive:
after canonical O+B(X) is confirmed, the same observation logical identity
paired with B(Y) must be handled as an archive conflict, with the original
canonical record preserved. This test belongs to Task 6 because it verifies the
handoff from construction transport to durable archive authority; it is not
duplicated as a Task 3 resolver test.

Only ArchiveResult.status == committed with durability_confirmed == True, or
ArchiveResult.status == no_op with durability_confirmed == True after the
required fresh validation, authorizes a record as canonical durable evidence.
A local sidecar file, an SQLite save, or an unconfirmed Git commit never
satisfies this transition.

The Task 6 test matrix includes:

- [ ] ArchiveAuthorityTransitionTests.test_different_source_binding_for_same_observation_conflicts_after_first_archive

This test archives O+B(X), confirms the first durable transition, then submits
the same observation identity with B(Y) and asserts conflict plus byte-preserved
canonical O+B(X). A local sidecar write or SQLite save alone must not satisfy
the transition.

**Interfaces to implement:** `EvidenceMaterializer.largest_continuously_eligible_horizon` and `EvidenceMaterializer.evaluate_observation_once` from the shared interface, plus the existing `SQLiteCache.save_signal_forward_outcomes_for_signal` as the operational materialization sink. `evaluate_observation_once` receives one observation, one canonical `ForwardMarketSeries`, and the externally qualified horizons; it limits the series to the largest continuously eligible prefix and calls only `advisor.signal_outcome.evaluate_signal_observation(observation, series)` at most once. The method returns the exact `SignalForwardEvaluation` from the frozen authority without reimplementing any return/MFE/MAE/barrier math.

**TDD sequence:**

  - [ ] Write these exact failing tests:
    - `ArchiveDurabilityTests.test_local_transport_cannot_satisfy_maturation_before_first_archive`
    - `ArchiveDurabilityTests.test_push_confirmation_and_fresh_read_are_required_before_maturation`
    - `OutcomeMaturationTests.test_maturation_calls_frozen_evaluator_at_most_once_per_observation_cycle`
    - `OutcomeMaturationTests.test_largest_continuously_eligible_prefix_limits_forward_series`
    - `OutcomeMaturationTests.test_maturation_does_not_call_scoring`
    - `OutcomeMaturationTests.test_maturation_does_not_call_risk`
    - `OutcomeMaturationTests.test_maturation_stores_exact_frozen_evaluator_result`
    - `OutcomeMaturationTests.test_ineligible_horizon_is_not_sent_to_frozen_evaluator`
    - `OutcomeMaturationTests.test_outcomes_for_externally_blocked_horizons_are_rejected`
    - `OutcomeMaturationTests.test_first_archive_failure_prevents_maturation_and_second_archive`
    - `OutcomeMaturationTests.test_second_archive_failure_writes_zero_new_operational_outcome_rows`
    - `OutcomeMaturationTests.test_pending_horizons_are_preserved_without_recalculation`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.OutcomeMaturationTests.test_maturation_calls_frozen_evaluator_at_most_once_per_observation_cycle`
  Expected failure: `AssertionError` because the new call-count and prefix contract is not yet implemented.
- [ ] Use `unittest.mock.patch`/spy on `advisor.signal_outcome.evaluate_signal_observation` and the scoring/risk entry points. Require the materializer to call that module-level frozen authority directly, then assert the evaluator call count is `<= 1` for each observation in one maturation cycle, scoring and risk are not called, and the accepted outcome records equal the exact frozen return objects after conversion to the existing persistence record. Assert that a blocked horizon returned by the frozen evaluator is rejected rather than accepted.
- [ ] Build the forward-market input from canonical market shards with the frozen `ForwardMarketSeries` contract. Compute the largest continuously eligible prefix from externally qualified statuses: for h5/h10/h20/h40, every lower prefix must also be eligible; if h10 is blocked, cap the series at 5 bars; if h20 is blocked after h5/h10, cap it at 10 bars. For `h5=verified_none`, `h10=split_in_horizon_unavailable`, and h20/h40 unavailable, pass only 5 forward bars, make one evaluator call, accept only h5, and retain the external h10/h20/h40 states. Pass only that limited series to one `evaluate_signal_observation(observation, series)` call. Do not duplicate frozen outcome mathematics, modify SQL schema, modify append-only triggers, or write legacy journal rows.
- [ ] Implement the two durability tests with a temporary Git repository: leave collector transport outside the evidence branch, invoke maturation against the branch checkout, and assert no proof/evaluator call occurs; then archive and confirm push, make a fresh checkout, and assert maturation becomes eligible. A materializer cannot receive a transport directory as an authority input.
- [ ] Enforce the sequence in the orchestration method: validate local transport; call `EvidenceArchive.archive` for the first market/corporate batch; require `status="committed"` and `durability_confirmed=True` or a validated `status="no_op"`; perform a fresh branch read; qualify horizons; make at most one frozen evaluator call per observation over the largest eligible prefix; write proof/outcome transport only; call `EvidenceArchive.archive` for the second transaction; require the same durable `committed`/validated `no_op` result; only then call `SQLiteCache.save_signal_forward_outcomes_for_signal` and update operational pending/materialized rows. If either archive is not durably confirmed, write zero new SQLite outcome rows; a first-archive failure also prevents proof/outcome transport, and a second-archive conflict, rejection, or storage failure leaves the pre-cycle SQLite count unchanged.
- [ ] Run targeted maturation tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.OutcomeMaturationTests`
- [ ] Run frozen outcome and report isolation regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_outcome tests.test_signal_observation tests.test_live_loader`
- [ ] Commit the task result as `feat: mature outcomes from canonical evidence` after diff and scope checks.

**Acceptance:** Only canonical, durably archived evidence reaches frozen 3B.2; one observation/cycle makes at most one evaluator call over the largest continuously eligible prefix; evaluator output is authoritative; blocked horizons are not accepted; scoring/risk are never called; pending horizons remain pending; SQLite receives no new operational outcome before the second archive is confirmed; second-archive failure writes zero new rows; outcome storage remains append-only and frozen.

Task 6 owns the first-archive transition and the durable-divergence Test J;
Task 8 owns the broad recovery drill after SQLite, caches, transport, and
workspaces are lost. Task 6 may prove that recovery reads the already archived
binding, but it does not duplicate Task 8's full recovery acceptance.

## Task 7 — GitHub Actions permission boundaries and CLI dispatch

Task 7 transports and archives the records-only observation sidecar produced by
Task 3. Workflow jobs do not receive snapshots_by_symbol and never construct a
new ObservationSourceBinding; they only hand off the prebuilt O+B artifact to
the archive/materializer stages in the specified order.

**Reviewer boundary:** A reviewer can approve or reject workflow job boundaries, secrets, concurrency, artifact handoffs, and the unchanged nightly workflow without running GitHub.

**Files:** Create `.github/workflows/financial-advisor-evidence.yml`; modify `advisor/cli.py` and `.github/workflows/financial-advisor-reports.yml`; extend `tests/test_evidence_accumulation.py`. Modify no `.github/workflows/financial-advisor-nightly-review.yml`.

**Interfaces to implement:** Add an `evidence` CLI namespace with exact subcommands and nonzero status propagation:

```text
python -m advisor evidence collect --assets-file .tmp/evidence-assets.json --transport-dir .tmp/evidence-transport
python -m advisor evidence archive --transport-dir .tmp/evidence-transport --repo-dir .
python -m advisor evidence materialize --evidence-checkout .tmp/evidence-checkout --db .tmp/evidence.db
python -m advisor evidence mature --evidence-checkout .tmp/evidence-checkout --db .tmp/evidence.db --transport-dir .tmp/evidence-proof-transport
```

`.tmp/evidence-assets.json` is a strict JSON document containing `{"assets":[{"symbol":"AAPL","asset_type":"stock"},{"symbol":"SPY","asset_type":"etf"},{"symbol":"HYPE","asset_type":"crypto"}]}`. The CLI constructs `CollectionAsset` values from this document; it never infers asset type from provider success.

The existing `python -m advisor outcomes evaluate --input-path .tmp/forward-input.json --db .tmp/evidence.db` remains unchanged and is not routed through the new evidence commands.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `WorkflowContractTests.test_reports_job_has_contents_read_and_provider_secrets_only`
  - `WorkflowContractTests.test_archive_job_has_contents_write_and_no_provider_secrets`
  - `WorkflowContractTests.test_collector_job_has_contents_read_and_provider_secrets`
  - `WorkflowContractTests.test_collector_job_reads_oldest_provider_from_read_only_evidence_checkout`
  - `WorkflowContractTests.test_publish_mature_job_has_contents_write_and_no_provider_secrets`
  - `WorkflowContractTests.test_publish_mature_order_is_first_archive_fresh_read_mature_second_archive`
  - `WorkflowContractTests.test_evidence_writer_concurrency_does_not_cancel_in_progress`
  - `WorkflowContractTests.test_nightly_workflow_is_not_modified_or_consumed_by_evidence_flow`
  - `WorkflowContractTests.test_evidence_cli_propagates_branch_missing_and_archive_failure`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.WorkflowContractTests.test_archive_job_has_contents_write_and_no_provider_secrets`
  Expected failure: `FileNotFoundError` for `.github/workflows/financial-advisor-evidence.yml`.
- [ ] Implement report-job sidecar upload with `contents: read` and only the existing provider secrets. The archive job consumes the uploaded transport, has `contents: write`, and receives no provider secrets. The collector job has `contents: read` plus provider secrets and emits transport only. The publish/mature job has `contents: write`, receives no provider secrets, performs first archive, fresh read, materialization/maturation, and second archive.
- [ ] Make the collector job check out `advisor-evidence` read-only, call `oldest_canonical_provider_by_symbol` for the typed assets from `.tmp/evidence-assets.json`, and pass that mapping into `EvidenceCollector.collect_market`. The YAML supplies paths and credentials only; provider assignment, asset typing, session rules, and error statuses remain in Python.
- [ ] Set concurrency group exactly `advisor-evidence-writer` with `cancel-in-progress: false`. Make artifact names, paths, and job outputs explicit; do not pass provider response data through writer environment variables. Preserve the existing report invocations and report fallback behavior.
- [ ] Add CLI parsers that delegate to the new components and return nonzero for `evidence_branch_missing`, archive rejection, validation failure, and first-archive failure. Keep `financial-advisor-nightly-review.yml` byte/scope unchanged and prove it does not consume runtime/evidence artifacts.
- [ ] Run targeted workflow and CLI tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.WorkflowContractTests`
- [ ] Run existing workflow/automation regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_github_actions_workflow tests.test_automation_scripts`
- [ ] Commit the task result as `ci: separate evidence collection and publication permissions` after exact workflow scope inspection.

**Acceptance:** Provider secrets exist only in report/collector jobs; write permissions exist only in archive/publish jobs; publish/mature has no provider secrets; writer concurrency is non-cancelling; the nightly workflow is unchanged; CLI errors are explicit and nonzero.

## Task 8 — Recovery acceptance, security properties, and registered mutations

Task 8 may mutate and validate unarchived transport artifacts in disposable
copies, but those artifacts never become historical authority. Recovery and
security acceptance remain anchored on the canonical append-only archive and
the archived ObservationEvidenceRecord.

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
  - `SecurityPropertyTests.test_duplicate_raw_json_key_is_rejected`
  - `SecurityPropertyTests.test_nan_and_infinity_are_rejected`
  - `SecurityPropertyTests.test_provider_and_path_injection_are_rejected`
  - `SecurityPropertyTests.test_schema_mismatch_is_rejected`
  - `SecurityPropertyTests.test_hash_mismatch_is_rejected`
- [ ] Feed `test_duplicate_raw_json_key_is_rejected` a decompressed byte sequence containing two occurrences of the same object key and assert `strict_json_loads_bytes` rejects it before envelope validation. Do not create the fixture by constructing a Python dict.
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

Task 9 updates operational documentation and final checks only where they
refer to the records-only sidecar and archived source binding. It does not
restore the superseded snapshots_by_symbol builder contract or claim resolver
authority to an unarchived sidecar.

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

## Full-Suite Gates

Full-suite execution is deferred to the future implementation session and is not run after each task. Every gate uses exactly `.\.venv\Scripts\python.exe -m unittest discover -s tests`, the same repository virtual-environment interpreter used by the targeted tests. At each gate, only the two historical `test_nightly_auth_dry_run` failures may be classified separately as `PASS_WITH_BASELINE_KNOWN_FAILURES`; the baseline tests are not modified. Any additional failure is a blocker and must stop the freeze.

- [ ] Gate A — immediately after Task 6, when core Python implementation is complete:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests`
- [ ] Gate B — immediately after Task 7, when workflow integration is complete:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests`
- [ ] Gate C — in Task 9, before the implementation freeze/review:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests`

## Plan Self-Review Checklist

Before committing this plan, verify the following cross-task invariants:

- [ ] Every normative design-spec requirement maps to at least one task, test, or gate.
- [ ] Shared interface names and signatures are identical wherever they are defined, imported, or used by a RED test; `strict_json_loads_bytes` receives raw bytes, and both sidecar helpers are owned by/imported from `advisor.evidence_schema`.
- [ ] `ArchiveStatus`, `HorizonStatus`, corporate-action reason codes, split/crypto policies, and proof fields remain separate; `verified_none` is a status and `verified_no_split_in_signal_horizon_v1` is a policy value.
- [ ] Basis qualification always comes from the explicit versioned `PriceBasisClaim` propagated in `DataFetchMetadata`; provider name, payload shape, fallback label, and diagnostic payload hash never qualify it.
- [ ] Collector input is typed by `CollectionAsset`; oldest canonical provider evidence is read before collection; corporate revision conflict ownership remains in `EvidenceArchive`, and conflict consumption remains in `EvidenceMaterializer`.
- [ ] One observation/maturation cycle makes at most one frozen evaluator call over the largest continuously eligible prefix; blocked horizons cannot be sent to or accepted from 3B.2.
- [ ] The second archive is durably confirmed before any new operational SQLite outcome row; archive failure leaves the operational outcome count unchanged.
- [ ] Bootstrap/archive Git operations are isolated from the caller main worktree, and the local isolation test checks `HEAD`, index, worktree bytes, and unrelated files.
- [ ] The local `us_equities_session_v1` authority covers the seven session properties plus the four DST-boundary and two regular-close tests, with no external timezone database or network calendar requirement.
- [ ] Observation construction returns the valid in-memory list before SQLite persistence; an unavailable/failed SQLite save still emits that list's sidecar, while construction failure emits no sidecar.
- [ ] A cache-hit basis claim requires the exact qualified source/parser route and request identity; provider name, namespace, fallback label, and an unproven route never qualify it.
- [ ] Every listed raw-OHLCV source contract has a provider-native fixture proof with no adjustment/transformation, and an unproven route remains claimless.
- [ ] All three named full-suite gates use the repository virtual-environment interpreter and preserve the baseline-known-failure classification rule.
- [ ] No protected module appears under any `Files: Modify` line, no excluded phase is scheduled, and the required forbidden-placeholder scan returns zero matches.
- [ ] Task 3 constructs one immutable ObservationEvidenceRecord containing O+B from the exact decision-time AssetSnapshot; the superseded builder never accepts snapshots_by_symbol or performs a late symbol rejoin.
- [ ] snapshot_projection_v1 enumerates all 34 current AssetSnapshot fields plus every nested field, includes no field implicitly, uses explicit sequence order and null keys, and derives snapshot_sha256_v1 only through canonical_json_bytes.
- [ ] Test E uses a literal expected projection and the closed-world test kills omitted/include/excluded-field, claim, sequence-order, and future-field mutations.
- [ ] Task 5 reads only the canonical archived O+B record and consumes its captured digest/claim; it never rebuilds historical binding from a current provider, snapshot, market-data lookup, or SQLite.
- [ ] Task 6 owns first-archive authority transition and the exact different-source-binding conflict test J; local sidecar/SQLite writes do not make evidence durable.
- [ ] Task 4 does not execute provider assignment for ObservationSourceBinding, and Task 8 tests transport mutations without elevating transport to authority.

## Regression Checkpoint Matrix
The implementation worker runs the smallest relevant checkpoint after each task and does not run the full suite after every task.

| Checkpoint | Exact command | Purpose |
|---|---|---|
| Canonical schema | `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.CanonicalSerializationTests tests.test_evidence_accumulation.CanonicalIdempotencyTests` | Canonical bytes, gzip, hashes, duplicate/conflict semantics. |
| Archive | `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ArchiveGitTests` | Orphan branch, atomic batch, retry, conflicts, push safety. |
| Evidence suite | `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation` | All new properties, recovery, security, workflow, and mutation guards. |
| Frozen observation/outcome | `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome` | Protected contract regression and append-only operational sink. |
| Existing report data path | `.\.venv\Scripts\python.exe -m unittest tests.test_live_loader tests.test_data_sources tests.test_cache_config_cli` | Confirm existing live fallback/report behavior remains unchanged. |
| Workflow/automation | `.\.venv\Scripts\python.exe -m unittest tests.test_github_actions_workflow tests.test_automation_scripts` | Existing report/nightly contracts plus automation documentation. |
| Frozen predictive subsets | `.\.venv\Scripts\python.exe -m unittest tests.test_predictive_evaluation tests.test_predictive_statistics` | Confirm no predictive contract drift while evidence gates are added. |
| Full-suite gates A/B/C | `.\.venv\Scripts\python.exe -m unittest discover -s tests` | Execute only at the three named gates above, never after every task. |

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
