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
| `advisor/models.py` | Add optional, end-appended `DataFetchMetadata` fields for an explicit `PriceBasisClaim`; do not change `Candle`, `AssetSnapshot` decision fields, or any scoring input shape. |
| `advisor/data_sources.py` | Expose versioned source/parser claims for only the explicitly qualified raw OHLCV routes and the Alpha Vantage `SPLITS` endpoint. The claim is a source-contract value, never derived from provider name alone. FMP light remains unqualified unless a separate provider-native contract is proven. |
| `advisor/data_pipeline.py` | Preserve an explicit basis claim through `_price_fetch_metadata` into `AssetSnapshot.data_fetch_metadata` without inferring it from provider, endpoint appearance, or candle values. |
| `advisor/live_loader.py` | Thread an optional explicit basis claim through `_fetch`/`_fetch_optional` and `_fetch_metadata`; pass claims only at qualified parser/source call sites while leaving existing report fallback behavior unchanged. |
| `advisor/cli.py` | Build and retain the valid observation list before attempting operational SQLite persistence, write the sidecar from that same in-memory list regardless of the SQLite result, and add thin `evidence collect`, `evidence archive`, `evidence materialize`, and `evidence mature` dispatch. Existing report behavior and the frozen `outcomes evaluate` interface remain unchanged. |
| `.github/workflows/financial-advisor-reports.yml` | Preserve the existing report job and provider behavior, upload the observation-sidecar transport with the report output, and add the read-only-to-writer handoff required by the archive job without giving provider secrets to the writer. |
| `docs/AUTOMATION_SETUP.md` | Document the evidence workflow, exact command boundaries, orphan-branch bootstrap operation, permissions, concurrency, fresh-read requirement, and recovery assumptions. |

### Read-only contract anchors

`advisor/cache.py`, `advisor/signal_observation.py`, `advisor/signal_outcome.py`, `.github/workflows/financial-advisor-nightly-review.yml`, and the four frozen Phase 3B contract documents are inspected inputs. The permitted metadata-only changes to `advisor/models.py`, `advisor/data_pipeline.py`, `advisor/live_loader.py`, and `advisor/data_sources.py` are described above; no candle, decision, scoring, risk, or frozen-contract behavior changes. The existing live report fallback chain remains a regression surface only; it is not reused as evidence authority.

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
from advisor.models import AssetSnapshot
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

def build_observation_sidecar(
    observations: Sequence[SignalObservation],
    *,
    snapshots_by_symbol: Mapping[str, AssetSnapshot],
    output_path: Path,
) -> Path:
    pass

def resolve_signal_price_basis_status(
    *, observation: SignalObservation,
    sidecar: Mapping[str, object],
) -> SignalBasisStatus:
    pass
```

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

`canonical_json_bytes` uses UTF-8, `ensure_ascii=False`, `sort_keys=True`, separators `(",", ":")`, `allow_nan=False`, rejects nonfinite/unsupported values, and emits no final newline. `strict_json_loads_bytes` is the only function that detects duplicate keys because it receives raw JSON bytes; it uses strict UTF-8, `object_pairs_hook`, and `parse_constant`. Semantic arrays are sorted by the identity-defined order before serialization. `deterministic_gzip` emits one DEFLATE-9 member with `MTIME=0`, `FLG=0`, `XFL=2`, and `OS=255`; decompression rejects trailing bytes, unused data, CRC/size mismatch, multiple members, and the configured uncompressed-size limit.

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
    def qualify_horizon(
        self, *, observation: SignalObservation,
        horizon: int, signal_price_basis_status: SignalBasisStatus,
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

Inspection of the current call graph found that `Candle` contains only date and numeric OHLCV fields, while `DataFetchMetadata` contains provider, endpoint, timestamps, freshness, fallback, granularity, and `market_data_kind`, but no price-basis assertion. The report path is:

```text
LiveDataLoader._fetch / _fetch_optional
  -> _fetch_metadata
  -> stock_snapshot_from_payloads / crypto_snapshot_from_payloads
  -> _price_fetch_metadata
  -> AssetSnapshot.data_fetch_metadata
  -> _scan decisions and snapshots_by_symbol
  -> build_signal_observation
  -> observation sidecar
```

The safe implementation path is therefore limited to non-protected metadata propagation. Append an optional `price_basis_claim: PriceBasisClaim | None` field to `DataFetchMetadata` at the end of the dataclass, preserve it through `_price_fetch_metadata`, and pass it explicitly through `_fetch` and `_fetch_optional`. Add source-contract factories in `data_sources.py` for the qualified parser routes only:

- FMP `historical-price-eod/full` parsed through the existing raw OHLCV `historical` fields;
- Binance `klines`;
- Hyperliquid `candleSnapshot`.

Each factory returns `PriceBasisClaim(price_basis="raw_ohlcv", price_basis_policy_version="price_basis_v1", source_contract=source_contract_id)` where `source_contract_id` is one of these exact values: `fmp.historical_price_eod.full.raw_ohlcv_v1`, `binance.futures_klines.raw_ohlcv_v1`, or `hyperliquid.candle_snapshot.raw_ohlcv_v1`. The claim is passed by the corresponding loader call site, not inferred by `_fetch_metadata`. A cache hit may reattach that claim only when the current call site is the same qualified route, its request identity is the exact cache key, and the same versioned parser will process the payload; the cached payload does not create the claim. FMP light, Yahoo, Stooq, Alpha Vantage adjusted daily history, cache entries without an exact qualified route/parser claim, and any route not in the qualified registry return no claim and therefore produce `signal_basis_unavailable`. The sidecar copies the claim from the snapshot metadata and binds it to `signal_id + observation_hash`; its resolver accepts `verified_raw_ohlcv` only when the claim fields and qualified source-contract ID validate together.

This path does not alter `Candle`, `AssetDecision`, `SignalObservation`, scoring, risk, or report rendering. RED tests compare decisions and rendered reports built from snapshots with and without the optional metadata, and spy that the scoring/risk call behavior is unchanged. If a source/parser cannot satisfy the explicit claim contract, the implementation must leave the claim absent and return `signal_basis_unavailable`; it must not infer raw basis from provider name or payload shape.

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

## Task 3 — Observation sidecar and signal-basis integration

**Reviewer boundary:** A reviewer can approve or reject sidecar binding and report integration without changing the `SignalObservation` schema or the report’s existing scoring/risk behavior.

**Files:** Modify `advisor/models.py`, `advisor/data_sources.py`, `advisor/data_pipeline.py`, `advisor/live_loader.py`, and `advisor/cli.py`; extend `tests/test_evidence_accumulation.py`. The basis helper definitions remain in `advisor/evidence_schema.py`, created in Task 1.

The frozen observation contract remains read-only.

**Interfaces to implement in `advisor/evidence_schema.py` (and imported from that same module by every caller and RED test):**

```python
def build_observation_sidecar(
    observations: Sequence[SignalObservation],
    *,
    snapshots_by_symbol: Mapping[str, AssetSnapshot],
    output_path: Path,
) -> Path:
    pass

def resolve_signal_price_basis_status(
    *, observation: SignalObservation,
    sidecar: Mapping[str, object],
) -> SignalBasisStatus:
    pass
```

```python
# advisor/cli.py
# uses the existing `SQLiteCache`, `AssetDecision`, `AssetSnapshot`,
# `SignalRunMetadata`, and `SignalObservation` types
def _build_signal_observations(
    decisions: Sequence[AssetDecision],
    *,
    snapshots_by_symbol: Mapping[str, AssetSnapshot],
    stock_regime: str,
    crypto_regime: str,
    run_metadata: SignalRunMetadata,
) -> list[SignalObservation]:
    pass

def _persist_signal_observations(
    cache: SQLiteCache,
    observations: Sequence[SignalObservation],
) -> str:
    pass
```

The sidecar key is exactly `(signal_id, observation_hash)`. It carries explicit sanitized semantic provenance, the price-basis claim, source snapshot identity, and the sidecar schema version. `verified_raw_ohlcv` is emitted only when `AssetSnapshot.data_fetch_metadata.price_basis_claim` contains `price_basis="raw_ohlcv"`, `price_basis_policy_version="price_basis_v1"`, and one of the three qualified source-contract IDs from the call-graph finding. Absence, partial fields, an unqualified source contract, or ambiguous provenance emits `signal_basis_unavailable`. No code infers raw basis from a provider name, a fallback label, `market_data_kind`, or a `payload_sha256`.

The non-protected metadata path is explicit and versioned:

```python
# advisor/data_sources.py
def qualified_fmp_full_price_basis_claim() -> PriceBasisClaim:
    pass
def qualified_binance_klines_basis_claim() -> PriceBasisClaim:
    pass
def qualified_hyperliquid_candle_snapshot_basis_claim() -> PriceBasisClaim:
    pass

# advisor/live_loader.py
def _fetch(
    self, provider: str, namespace: str, url: str, *,
    payload: dict[str, Any] | None = None,
    parent_call_id: str | None = None, attempt_number: int = 1,
    fallback_from: str | None = None, fallback_to: str | None = None,
    fallback_reason: str | None = None, symbol: str | None = None,
    price_basis_claim: PriceBasisClaim | None = None,
) -> Any:
    pass
def _fetch_optional(
    self, provider: str, namespace: str, url: str, *, default: Any,
    payload: dict[str, Any] | None = None,
    parent_call_id: str | None = None, attempt_number: int = 1,
    fallback_from: str | None = None, fallback_to: str | None = None,
    fallback_reason: str | None = None, symbol: str | None = None,
    price_basis_claim: PriceBasisClaim | None = None,
) -> Any:
    pass
```

`DataFetchMetadata.price_basis_claim` is optional and appended after existing fields. The loader passes a claim only at the FMP full, Binance klines, and Hyperliquid candle-snapshot call sites whose parser contracts are explicitly qualified. A qualified exact-route cache hit may receive the same call-site claim again only when its request identity equals the current cache key and the same parser-contract version will process the payload. An unqualified cache hit remains claimless even if its provider is FMP, its namespace is `prices`, or it has a fallback label. FMP light, Yahoo, Stooq, Alpha Vantage adjusted daily history, and cache records without an exact qualified route/parser claim remain unqualified. `_price_fetch_metadata` preserves a supplied claim; it never creates one. `AssetSnapshot`, `AssetDecision`, `Candle`, `SignalObservation`, scoring, risk, and report rendering keep their existing shapes and behavior.

**TDD sequence:**

- [ ] Write these exact failing tests:
  - `ObservationSidecarTests.test_sidecar_binds_basis_by_signal_id_and_observation_hash`
  - `ObservationSidecarTests.test_sidecar_rejects_unknown_observation_hash_binding`
  - `SignalBasisPropagationTests.test_explicit_raw_metadata_claim_qualifies`
  - `SignalBasisPropagationTests.test_provider_name_alone_does_not_qualify`
  - `SignalBasisPropagationTests.test_unqualified_fallback_source_is_signal_basis_unavailable`
  - `SignalBasisPropagationTests.test_basis_claim_propagates_through_live_loader_pipeline`
  - `SignalBasisPropagationTests.test_basis_metadata_does_not_change_asset_decision_report_scoring_or_risk`
  - `SignalBasisPropagationTests.test_qualified_route_cache_hit_preserves_basis_claim`
  - `SignalBasisPropagationTests.test_unqualified_cache_hit_does_not_gain_basis_claim`
  - `SignalBasisPropagationTests.test_provider_name_cache_hit_does_not_qualify`
  - `SourceContractQualificationTests.test_fmp_full_fixture_proves_provider_native_raw_ohlcv_without_adjustment`
  - `SourceContractQualificationTests.test_binance_klines_fixture_proves_provider_native_raw_ohlcv_without_adjustment`
  - `SourceContractQualificationTests.test_hyperliquid_candle_snapshot_fixture_proves_provider_native_raw_ohlcv_without_adjustment`
  - `SourceContractQualificationTests.test_fmp_light_without_proof_remains_unqualified`
  - `ObservationSidecarTests.test_report_sidecar_reuses_the_same_in_memory_observations`
  - `ObservationSidecarTests.test_signal_observation_schema_and_hash_are_unchanged`
  - `ObservationSidecarTests.test_sqlite_unavailable_does_not_prevent_valid_sidecar`
  - `ObservationSidecarTests.test_observation_construction_failure_emits_no_sidecar`
  - `ObservationSidecarTests.test_sqlite_failure_does_not_change_report_decision_or_observation_hash`
- [ ] Run RED:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ObservationSidecarTests.test_sidecar_binds_basis_by_signal_id_and_observation_hash`
  Expected failure: `ImportError: cannot import name 'build_observation_sidecar' from 'advisor.evidence_schema'` or the equivalent missing-module failure before the helper exists.
- [ ] Add the optional end-appended `DataFetchMetadata.price_basis_claim` field and the immutable `PriceBasisClaim` shape in `advisor/models.py`. Add the three explicit qualified source-contract factories and the Alpha Vantage `SPLITS` URL method in `advisor/data_sources.py`. The factories return only the fixed `price_basis_v1`/`raw_ohlcv` combinations; no factory accepts a provider name as proof by itself.
- [ ] Thread `price_basis_claim` through `_fetch`, `_fetch_optional`, `_fetch_metadata`, and `_price_fetch_metadata` in both fresh and cache-hit paths. A cache hit may reattach a claim only when the current call site supplies a registry entry whose exact route URL/request key and parser contract version match the cache key and payload parser. FMP full, Binance klines, and Hyperliquid candle-snapshot call sites are the only v1 qualified routes; FMP light, Yahoo, Stooq, Alpha Vantage adjusted-history, and all other fallback calls remain claimless. The existing report fallback chain and its output remain unchanged.
- [ ] Implement sidecar construction in `advisor/evidence_schema.py` from the list returned by the existing report observation-building path and `snapshots_by_symbol`. In `advisor/cli.py`, `_build_signal_observations` first constructs the complete list from decisions and snapshots and returns only after every `SignalObservation` is valid and serializable; a construction/serialization exception produces no list and no sidecar. The CLI then calls `_persist_signal_observations(cache, observations)` as an operational attempt and emits the sidecar from that same in-memory list independently of its returned `written`, `duplicate_same`, or `unavailable` result and independently of a caught SQLite exception. `_persist_signal_observations` normalizes storage exceptions to its unavailable result for status reporting; it is never a gate for sidecar emission. Do not reload observations from SQLite and do not alter `SignalObservation` fields, identity, or hash computation.
- [ ] Require the CLI to pass the same snapshot identity used by the report decision that produced `ideal_entry`, `stop`, and targets for each observation. The sidecar must bind that snapshot's explicit `DataFetchMetadata.price_basis_claim` to `(signal_id, observation_hash)` and reject a sidecar assembled from a different snapshot.
- [ ] Implement `resolve_signal_price_basis_status` in `advisor/evidence_schema.py` so it verifies the observation key, snapshot metadata claim, policy version, raw basis literal, and qualified source-contract allowlist together. A provider-only metadata fixture and an unqualified fallback fixture must resolve to `signal_basis_unavailable`.
- [ ] Prove each qualified route with a deterministic provider-native fixture: FMP full `historical` rows with native `date/open/high/low/close/volume`, Binance kline positional OHLCV fields, and Hyperliquid `t/o/h/l/c/v` fields. Each test must assert the parser copies raw OHLCV values without adjusted-close or corporate-action transformation before allowing its exact `*_raw_ohlcv_v1` claim. FMP light receives an explicit no-proof fixture and remains unqualified; no route is qualified merely because fields have generic OHLCV names. These contract tests use fixture payloads only and make no provider request.
- [ ] Test cache-hit semantics using the existing `_cache_key` route/request identity: a qualified exact route/parser cache hit reattaches the call-site claim; an unqualified route cache hit has `price_basis_claim=None`; provider name `fmp`, namespace `prices`, or a fallback label alone never qualifies. No change to `advisor/cache.py` is permitted.
- [ ] Implement the CLI observation sequence in `advisor/cli.py` only: validate the required run metadata, call `_build_signal_observations` before any cache result is considered, and keep the returned list in memory. If construction or observation serialization fails, report the existing observation-unavailable status and do not call `build_observation_sidecar`; no synthetic observation is invented. If construction succeeds, call `_persist_signal_observations`, catch/normalize `written`, `duplicate_same`, `unavailable`, and storage-error outcomes, and then call `build_observation_sidecar` with the same list regardless of that operational result. The report markdown/HTML, `AssetDecision` values, and observation hashes remain the existing authority and are not changed by either outcome.
- [ ] Make the three SQLite/sidecar tests exercise this exact ordering: `ObservationSidecarTests.test_sqlite_unavailable_does_not_prevent_valid_sidecar` builds a valid `SignalObservation`, makes `SQLiteCache.save_signal_observations` return `status="unavailable"` or raise, and asserts the emitted sidecar contains the identical `signal_id` and `observation_hash`; `ObservationSidecarTests.test_observation_construction_failure_emits_no_sidecar` makes `_build_signal_observations` fail and asserts no sidecar writer call and no sidecar file; `ObservationSidecarTests.test_sqlite_failure_does_not_change_report_decision_or_observation_hash` compares report decision and observation hash with successful and failing SQLite persistence while asserting the in-memory observation/sidecar payload is identical.
- [ ] Keep report authority independent: a sidecar/archive error must be surfaced to the evidence transport path and must not mutate a generated report or invoke a fallback provider. The existing report fallback behavior in `live_loader.py` remains unchanged.
- [ ] Run the targeted sidecar tests:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.ObservationSidecarTests`
- [ ] Run metadata, scoring, report, and frozen observation regressions:
  `.\.venv\Scripts\python.exe -m unittest tests.test_evidence_accumulation.SignalBasisPropagationTests tests.test_hardening tests.test_phase2_report_provenance`
  `.\.venv\Scripts\python.exe -m unittest tests.test_signal_observation tests.test_signal_outcome tests.test_live_loader tests.test_cache_config_cli`
- [ ] Commit the task result as `feat: persist observation evidence sidecars` after `git diff --check` and exact scope inspection.

**Acceptance:** The sidecar is derived from the report’s in-memory observations, is keyed by both immutable identifiers, copies an explicit versioned basis claim from snapshot metadata, cannot bind a different hash, never changes `SignalObservation`, and marks provider-only/unqualified stock/ETF paths as `signal_basis_unavailable`. Adding the metadata claim does not change `AssetDecision`, rendered report, scoring, or risk results.

## Task 4 — Deterministic market/provider and corporate-action collection

**Reviewer boundary:** A reviewer can approve or reject assigned-provider determinism, sticky recovery, no silent fallback, session coverage, and Alpha Vantage split normalization without reviewing maturation.

**Files:** Create/extend `advisor/evidence_collector.py` and `tests/test_evidence_accumulation.py`. The collector consumes the explicit source-contract methods from `advisor/data_sources.py` and does not modify loader/report behavior.

**Interfaces to implement:** `CollectionAsset`, `PRICE_PROVIDER_ASSIGNMENT_POLICY_VERSION`, `assigned_price_provider`, `EvidenceCollector.collect_market`, `EvidenceCollector.collect_corporate_actions`, and `normalize_split_factor`. The collector writes transport only and has no Git or SQLite authority. Before collection, the read-only collector job calls `oldest_canonical_provider_by_symbol(evidence_checkout=evidence_checkout, symbols=tuple(asset.symbol for asset in assets))` for the typed asset list and passes the returned mapping to `collect_market`; a missing symbol key means the deterministic v1 mapping applies.

**TDD sequence:**

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

**Interfaces to implement:** `EvidenceMaterializer`, `HorizonQualification`, `MaterializationResult`, `qualify_horizon`, and `resolve_signal_price_basis_status` imported from `advisor.evidence_schema`. `HorizonQualification.status` is a `HorizonStatus`; its `policy` is a separate `SplitPolicy | CryptoPolicy | None`; and its `reason_code` is a separate `CorporateActionReasonCode | None`. The materializer accepts only an evidence checkout and DB target; it has no API for a transport directory.

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

## Task 7 — GitHub Actions permission boundaries and CLI dispatch

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
