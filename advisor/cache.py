from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from advisor.models import AssetDecision, Candle


class SQLiteCache:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def set_json(self, namespace: str, key: str, payload: Any, fetched_at: str | None = None) -> None:
        fetched_at = fetched_at or _now_iso()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    insert into cache(namespace, key, payload, fetched_at)
                    values (?, ?, ?, ?)
                    on conflict(namespace, key) do update set
                        payload = excluded.payload,
                        fetched_at = excluded.fetched_at
                    """,
                    (namespace, key, json.dumps(payload, sort_keys=True), fetched_at),
                )

    def get_json(
        self,
        namespace: str,
        key: str,
        *,
        max_age_seconds: int,
        now: str | None = None,
    ) -> Any | None:
        now_dt = _parse_iso(now or _now_iso())
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "select payload, fetched_at from cache where namespace = ? and key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        payload, fetched_at = row
        age = (now_dt - _parse_iso(fetched_at)).total_seconds()
        if age > max_age_seconds:
            return None
        return json.loads(payload)

    def save_latest_report(self, markdown: str, html: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    "insert into reports(created_at, markdown, html) values (?, ?, ?)",
                    (_now_iso(), markdown, html),
                )

    def load_latest_report(self) -> tuple[str, str] | None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "select markdown, html from reports order by id desc limit 1",
            ).fetchone()
        return tuple(row) if row else None

    def save_signal_journal(self, decisions: list[AssetDecision], *, report_file: str) -> None:
        generated_at = _now_iso()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                for decision in decisions:
                    connection.execute(
                        """
                        insert into signal_journal(
                            created_at, market_session, asset, asset_type, decision_label, bucket,
                            investment_quality_score, swing_trade_score, expected_value_r,
                            win_rate_2r, win_rate_3r, sample_size, confidence_quality,
                            data_quality, missing_data_severity, entry, alternative_entry,
                            stop, target_2r, target_3r, risk_per_trade, max_position_size,
                            reason_codes, data_source, data_timestamp, report_file, status
                        )
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _signal_row(generated_at, decision, report_file),
                    )

    def load_signal_journal(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("select * from signal_journal order by id").fetchall()
        return [dict(row) for row in rows]

    def update_signal_results(self, candles_by_asset: dict[str, list[Candle]]) -> int:
        updated = 0
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                rows = connection.execute(
                    """
                    select id, asset, entry, stop, target_2r, target_3r
                    from signal_journal
                    where result_final is null or result_final = 'open'
                    """
                ).fetchall()
                for row in rows:
                    candles = candles_by_asset.get(row["asset"])
                    if not candles:
                        continue
                    result = _track_signal_result(
                        candles,
                        entry=float(row["entry"]),
                        stop=float(row["stop"]),
                        target_2r=float(row["target_2r"]),
                        target_3r=float(row["target_3r"]),
                    )
                    connection.execute(
                        """
                        update signal_journal set
                            return_5d = ?,
                            return_10d = ?,
                            return_20d = ?,
                            return_40d = ?,
                            hit_stop = ?,
                            hit_2r = ?,
                            hit_3r = ?,
                            days_to_2r = ?,
                            days_to_stop = ?,
                            result_final = ?,
                            updated_at = ?
                        where id = ?
                        """,
                        (
                            result["return_5d"],
                            result["return_10d"],
                            result["return_20d"],
                            result["return_40d"],
                            int(result["hit_stop"]),
                            int(result["hit_2r"]),
                            int(result["hit_3r"]),
                            result["days_to_2r"],
                            result["days_to_stop"],
                            result["result_final"],
                            _now_iso(),
                            row["id"],
                        ),
                    )
                    updated += 1
        return updated

    def _ensure_schema(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute(
                    """
                    create table if not exists cache(
                        namespace text not null,
                        key text not null,
                        payload text not null,
                        fetched_at text not null,
                        primary key(namespace, key)
                    )
                    """
                )
                connection.execute(
                    """
                    create table if not exists api_usage(
                        provider text not null,
                        day text not null,
                        count integer not null,
                        primary key(provider, day)
                    )
                    """
                )
                connection.execute(
                    """
                    create table if not exists reports(
                        id integer primary key autoincrement,
                        created_at text not null,
                        markdown text not null,
                        html text not null
                    )
                    """
                )
                connection.execute(
                    """
                    create table if not exists signal_journal(
                        id integer primary key autoincrement,
                        created_at text not null,
                        market_session text not null,
                        asset text not null,
                        asset_type text not null,
                        decision_label text not null,
                        bucket text not null,
                        investment_quality_score real not null,
                        swing_trade_score real not null,
                        expected_value_r real,
                        win_rate_2r real,
                        win_rate_3r real,
                        sample_size integer not null,
                        confidence_quality text,
                        data_quality text not null,
                        missing_data_severity text not null,
                        entry real not null,
                        alternative_entry real,
                        stop real not null,
                        target_2r real not null,
                        target_3r real not null,
                        risk_per_trade real not null,
                        max_position_size text not null,
                        reason_codes text not null,
                        data_source text not null,
                        data_timestamp text,
                        report_file text not null,
                        status text not null,
                        return_5d real,
                        return_10d real,
                        return_20d real,
                        return_40d real,
                        hit_stop integer,
                        hit_2r integer,
                        hit_3r integer,
                        days_to_2r integer,
                        days_to_stop integer,
                        result_final text,
                        updated_at text
                    )
                    """
                )


class ApiLimiter:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        SQLiteCache(self.db_path)

    def allow(self, provider: str, *, limit: int, day: str | None = None) -> bool:
        day = day or datetime.now(timezone.utc).date().isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                row = connection.execute(
                    "select count from api_usage where provider = ? and day = ?",
                    (provider, day),
                ).fetchone()
                current = row[0] if row else 0
                if current >= limit:
                    return False
                connection.execute(
                    """
                    insert into api_usage(provider, day, count)
                    values (?, ?, ?)
                    on conflict(provider, day) do update set count = excluded.count
                    """,
                    (provider, day, current + 1),
                )
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _signal_row(generated_at: str, decision: AssetDecision, report_file: str) -> tuple[Any, ...]:
    stats = decision.backtest_stats
    status = "blocked" if decision.decision == "blocked" else (
        "open_for_tracking" if decision.decision in {"tradeable", "watch_buy"} else "not_tradeable"
    )
    return (
        generated_at,
        decision.market_session,
        decision.symbol,
        decision.asset_type,
        decision.decision,
        decision.decision if decision.bucket in {"", "unknown"} else decision.bucket,
        decision.investment_quality_score,
        decision.swing_trade_score,
        stats.expected_value_r if stats else None,
        stats.win_rate_2r if stats else None,
        stats.win_rate_3r if stats else None,
        stats.sample_size if stats else 0,
        decision.sample_quality,
        decision.data_quality,
        decision.missing_data_severity,
        decision.ideal_entry,
        decision.alternative_entry,
        decision.risk_plan.stop,
        decision.risk_plan.target_2r,
        decision.risk_plan.target_3r,
        decision.risk_plan.risk_amount,
        decision.risk_plan.position_size_display or str(decision.risk_plan.max_position_units),
        json.dumps(decision.reason_codes, sort_keys=True),
        decision.data_source,
        decision.data_timestamp,
        report_file,
        status,
    )


def _track_signal_result(candles: list[Candle], *, entry: float, stop: float, target_2r: float, target_3r: float) -> dict[str, Any]:
    hit_stop = False
    hit_2r = False
    hit_3r = False
    days_to_2r = None
    days_to_stop = None
    result_final = "open"
    for days, candle in enumerate(candles[1:41], start=1):
        if not hit_stop and candle.low <= stop:
            hit_stop = True
            days_to_stop = days
            result_final = "hit_stop"
            break
        if not hit_3r and candle.high >= target_3r:
            hit_2r = True
            hit_3r = True
            days_to_2r = days_to_2r or days
            result_final = "hit_3r"
            break
        if not hit_2r and candle.high >= target_2r:
            hit_2r = True
            days_to_2r = days
            result_final = "hit_2r"
            break
    if result_final == "open" and len(candles) > 40:
        result_final = "expired"
    return {
        "return_5d": _period_return(candles, entry, 5),
        "return_10d": _period_return(candles, entry, 10),
        "return_20d": _period_return(candles, entry, 20),
        "return_40d": _period_return(candles, entry, 40),
        "hit_stop": hit_stop,
        "hit_2r": hit_2r,
        "hit_3r": hit_3r,
        "days_to_2r": days_to_2r,
        "days_to_stop": days_to_stop,
        "result_final": result_final,
    }


def _period_return(candles: list[Candle], entry: float, days: int) -> float | None:
    if entry == 0 or len(candles) <= days:
        return None
    return round((candles[days].close - entry) / entry, 6)
