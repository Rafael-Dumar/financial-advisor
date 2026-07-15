from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


BRT = timezone(timedelta(hours=-3))


class ArtifactSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactCandidate:
    run_id: int
    created_at: str
    event: str
    conclusion: str
    head_sha: str
    head_branch: str
    url: str
    artifact_name: str
    artifact_expired: bool
    artifact_created_at: str
    report_type: str
    report_brt_date: str
    report_generated_at: str
    report_path: str


@dataclass(frozen=True)
class ArtifactSelection:
    status: str
    operational_allowed: bool
    source_date: str
    artifact_age_seconds: int
    main: ArtifactCandidate
    close: ArtifactCandidate

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_artifact_pair(
    candidates: list[dict[str, object] | ArtifactCandidate],
    *,
    brt_date: str,
    expected_head_sha: str,
    allow_manual: bool = False,
    allow_stale_diagnostic: bool = False,
    selected_at: str | None = None,
) -> ArtifactSelection:
    selection_time = _timestamp(selected_at) if selected_at else datetime.now(timezone.utc)
    parsed = [_candidate(value) for value in candidates]
    eligible = [
        candidate
        for candidate in parsed
        if _metadata_is_valid(
            candidate,
            expected_head_sha=expected_head_sha,
            allow_manual=allow_manual,
        )
    ]
    current = [candidate for candidate in eligible if candidate.report_brt_date == brt_date]
    pair = _select_same_day_pair(current)
    if pair is not None:
        main, close = pair
        return ArtifactSelection(
            status="valid_current_day",
            operational_allowed=True,
            source_date=brt_date,
            artifact_age_seconds=max(
                _artifact_age_seconds(main, selection_time),
                _artifact_age_seconds(close, selection_time),
            ),
            main=main,
            close=close,
        )

    if allow_stale_diagnostic:
        prior_dates = sorted(
            {candidate.report_brt_date for candidate in eligible if candidate.report_brt_date < brt_date},
            reverse=True,
        )
        for source_date in prior_dates:
            pair = _select_same_day_pair(
                [candidate for candidate in eligible if candidate.report_brt_date == source_date]
            )
            if pair is not None:
                main, close = pair
                return ArtifactSelection(
                    status="stale_diagnostic",
                    operational_allowed=False,
                    source_date=source_date,
                    artifact_age_seconds=max(
                        _artifact_age_seconds(main, selection_time),
                        _artifact_age_seconds(close, selection_time),
                    ),
                    main=main,
                    close=close,
                )

    raise ArtifactSelectionError(
        f"no_valid_current_day_artifact_pair:brt_date={brt_date}:expected_head_sha={expected_head_sha}"
    )


def _candidate(value: dict[str, object] | ArtifactCandidate) -> ArtifactCandidate:
    if isinstance(value, ArtifactCandidate):
        return value
    try:
        return ArtifactCandidate(
            run_id=int(value["run_id"]),
            created_at=str(value["created_at"]),
            event=str(value["event"]),
            conclusion=str(value["conclusion"]),
            head_sha=str(value["head_sha"]),
            head_branch=str(value["head_branch"]),
            url=str(value["url"]),
            artifact_name=str(value["artifact_name"]),
            artifact_expired=bool(value["artifact_expired"]),
            artifact_created_at=str(value["artifact_created_at"]),
            report_type=str(value["report_type"]),
            report_brt_date=str(value["report_brt_date"]),
            report_generated_at=str(value["report_generated_at"]),
            report_path=str(value["report_path"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ArtifactSelectionError(f"invalid_artifact_candidate:{error}") from error


def _metadata_is_valid(
    candidate: ArtifactCandidate,
    *,
    expected_head_sha: str,
    allow_manual: bool,
) -> bool:
    allowed_events = {"schedule", "workflow_dispatch"} if allow_manual else {"schedule"}
    expected_name = f"financial-advisor-{candidate.report_type}-{candidate.run_id}"
    return (
        candidate.report_type in {"main", "close"}
        and candidate.conclusion == "success"
        and candidate.event in allowed_events
        and candidate.head_sha == expected_head_sha
        and candidate.head_branch == "main"
        and not candidate.artifact_expired
        and candidate.artifact_name == expected_name
        and _brt_date(candidate.report_generated_at) == candidate.report_brt_date
    )


def _select_same_day_pair(candidates: list[ArtifactCandidate]) -> tuple[ArtifactCandidate, ArtifactCandidate] | None:
    main_candidates = sorted(
        (candidate for candidate in candidates if candidate.report_type == "main"),
        key=lambda candidate: (_timestamp(candidate.created_at), candidate.run_id),
        reverse=True,
    )
    close_candidates = sorted(
        (candidate for candidate in candidates if candidate.report_type == "close"),
        key=lambda candidate: (_timestamp(candidate.created_at), candidate.run_id),
        reverse=True,
    )
    for main in main_candidates:
        for close in close_candidates:
            if (
                main.head_sha == close.head_sha
                and main.report_brt_date == close.report_brt_date
                and _timestamp(main.report_generated_at) <= _timestamp(close.report_generated_at)
            ):
                return main, close
    return None


def _artifact_age_seconds(candidate: ArtifactCandidate, selected_at: datetime) -> int:
    return max(0, int((selected_at - _timestamp(candidate.artifact_created_at)).total_seconds()))


def _brt_date(value: str) -> str:
    return _timestamp(value).astimezone(BRT).date().isoformat()


def _timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brt-date", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--allow-manual", action="store_true")
    parser.add_argument("--allow-stale-diagnostic", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        selection = select_artifact_pair(
            payload,
            brt_date=args.brt_date,
            expected_head_sha=args.expected_head_sha,
            allow_manual=args.allow_manual,
            allow_stale_diagnostic=args.allow_stale_diagnostic,
        )
    except (ArtifactSelectionError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(selection.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
