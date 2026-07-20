"""Source-grounded Phase 3A.2 forensic artifacts.

This module is deliberately evidence-first.  It never manufactures an
occurrence set, never runs a parallel scoring model, and never changes the
advisor's scoring implementation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


CODE_PATH = "advisor/scoring.py"
CODE_REF = "6d0c1f705032606d6b449f79b8c151b941e1c037"
REASONS = [
    "synthetic_suspected_occurrence_set",
    "news_status_scope_collapsed",
    "counterfactual_parallel_simulation",
    "partially_tautological_tests",
]
DECISION_RANK = {
    "tradeable": 0,
    "watch_buy": 1,
    "watch_only": 2,
    "technical_unvalidated": 3,
    "wait": 4,
    "avoid": 5,
    "blocked": 6,
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _field_lines(text: str) -> list[tuple[str, str, int]]:
    result = []
    for line_no, line in enumerate(text.splitlines(), 1):
        match = re.match(r"^- ([^:]+):\s*(.*)$", line)
        if match:
            result.append((match.group(1), match.group(2).strip().strip("`") , line_no))
    return result


def asset_sections(report_text: str) -> list[dict[str, Any]]:
    lines = report_text.splitlines()
    headings = [(i + 1, m.group(1)) for i, line in enumerate(lines) if (m := re.fullmatch(r"## ([A-Z0-9.-]+)", line.strip()))]
    sections = []
    for index, (start, symbol) in enumerate(headings):
        end = headings[index + 1][0] - 1 if index + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end])
        fields: dict[str, list[dict[str, Any]]] = {}
        for name, raw, line_no in _field_lines(body):
            fields.setdefault(name, []).append({"raw": raw, "line": line_no + start})
        if fields.get("Ativo"):
            sections.append({"symbol": symbol, "start_line": start, "end_line": end, "text": body, "fields": fields})
    return sections


def _first(section: dict[str, Any], name: str) -> dict[str, Any] | None:
    values = section["fields"].get(name, [])
    return values[0] if values else None


def _last(section: dict[str, Any], name: str) -> dict[str, Any] | None:
    values = section["fields"].get(name, [])
    return values[-1] if values else None


def _observed(value: Any, raw: Any, report: str, symbol: str, section: str, line: int) -> dict[str, Any]:
    return {
        "value": value,
        "raw_value": raw,
        "provenance": "observed",
        "source": {"relative_path": report, "section": symbol, "subsection": section, "line": line},
    }


def _unavailable(scope: str, reason: str = "not_exposed_in_source_report") -> dict[str, Any]:
    return {
        "value": None,
        "raw_value": None,
        "provenance": "unavailable",
        "source": None,
        "reason": reason,
        "scope": scope,
    }


def _status_wrapper(field: dict[str, Any] | None, scope: str, report: str, symbol: str) -> dict[str, Any]:
    if field is None:
        return _unavailable(scope)
    return {**_observed(field["raw"], field["raw"], report, symbol, scope, field["line"]), "scope": scope}


def _semantic_field_name(line: str) -> str | None:
    match = re.match(r"^- ([^:]+):", line.strip())
    return match.group(1) if match else None


_DECISION_MARKERS = {
    "decision_label", "Decisao", "reason_codes", "Investment Quality Score", "Swing Trade Score",
}
_CAPABILITY_MARKERS = {
    "Data source", "provider", "quote_status", "guidance_status", "macro_status",
    "sec_filings_status", "sector_benchmark_status",
}
_CAPABILITY_SIGNATURE_POLICY = {
    "required_markers": ["news_status", "Data source", "provider"],
    "required_marker_groups": {
        "provider_identity": ["Data source", "provider"],
        "capability_context": ["quote_status", "guidance_status", "macro_status", "sec_filings_status", "sector_benchmark_status"],
    },
    "minimum_group_coverage": 2,
}
_COLLECTION_MARKERS = {"News/catalyst summary"}
# These are real report fields that start the next structural subsection.  They
# are deliberately not news/capability markers, so a status inside one of
# these blocks remains unclassified and cannot be a scope candidate.
_UNCLASSIFIED_MARKERS = {
    "Entrada ideal", "Entrada alternativa", "Stop/invalidation", "Alvo 2R", "Alvo 3R",
    "Risco por trade", "Tamanho maximo da posicao", "Relacao risco/retorno",
    "Leverage risk gate", "Leverage risk gate reasons", "Principais alertas",
    "Dados ausentes ou limitacoes", "short_setup_score", "squeeze_risk", "gap_risk",
    "borrow_data_available", "short_status",
}


def _marker_kind(name: str | None) -> str | None:
    if name in _DECISION_MARKERS:
        return "decision"
    if name in _CAPABILITY_MARKERS:
        return "capability"
    if name in _COLLECTION_MARKERS:
        return "collection"
    if name in _UNCLASSIFIED_MARKERS:
        return "unclassified"
    return None


def is_complete_capability_block(markers: list[dict[str, Any]] | set[str], fields: set[str] | list[str]) -> tuple[bool, list[str]]:
    """Return whether a capability block has the complete semantic signature."""
    marker_names = {marker["name"] if isinstance(marker, dict) else marker for marker in markers}
    field_names = set(fields)
    missing: list[str] = []
    for required in _CAPABILITY_SIGNATURE_POLICY["required_markers"]:
        if required not in marker_names and required not in field_names:
            missing.append(required)
    provider_identity = set(_CAPABILITY_SIGNATURE_POLICY["required_marker_groups"]["provider_identity"])
    context = set(_CAPABILITY_SIGNATURE_POLICY["required_marker_groups"]["capability_context"])
    if not provider_identity.issubset(marker_names):
        missing.append("provider_identity_group")
    if not marker_names.intersection(context):
        missing.append("capability_context_group")
    return not missing, list(dict.fromkeys(missing))


def segment_asset_blocks(asset_text: str) -> list[dict[str, Any]]:
    """Segment an asset using structural field markers, never global position."""
    lines = asset_text.splitlines()
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def close(end_index: int) -> None:
        nonlocal current
        if current is None:
            return
        current["end_line"] = end_index
        current["raw_lines"] = lines[current["start_line"] - 1:end_index]
        blocks.append(current)
        current = None

    for index, line in enumerate(lines, 1):
        name = _semantic_field_name(line)
        kind = _marker_kind(name)
        # Collection is a semantic sub-block of the decision narrative in the
        # real report.  Keep it in the parent block; its exact summary/status
        # range is materialized separately by parse_asset_semantic_blocks.
        if kind == "collection":
            if current is not None:
                current["markers"].append({"name": name, "line": index, "kind": kind})
            continue
        if kind is None:
            continue
        if current is None:
            current = {
                "block_id": f"block-{len(blocks) + 1}",
                "start_line": index,
                "end_line": index,
                "raw_lines": [],
                "markers": [],
                "candidate_types": [kind],
            }
        elif kind != current["candidate_types"][0] or (
            kind == "capability" and name == "Data source"
            and "news_status" in {_semantic_field_name(raw) for raw in lines[current["start_line"] - 1:index]}
        ):
            close(index - 1)
            current = {
                "block_id": f"block-{len(blocks) + 1}",
                "start_line": index,
                "end_line": index,
                "raw_lines": [],
                "markers": [],
                "candidate_types": [kind],
            }
        current["markers"].append({"name": name, "line": index, "kind": kind})
    close(len(lines))

    for block in blocks:
        block["candidate_types"] = list(dict.fromkeys(block["candidate_types"]))
        block["text"] = "\n".join(block["raw_lines"])
    return blocks


def classify_semantic_block(block: dict[str, Any]) -> dict[str, Any]:
    candidates = list(dict.fromkeys(block.get("candidate_types", [])))
    fields = {_semantic_field_name(line) for line in block.get("raw_lines", []) if _semantic_field_name(line)}
    block["fields"] = sorted(fields)
    if len(candidates) == 1 and candidates[0] in {"decision", "capability", "collection", "unclassified"}:
        block["type"] = candidates[0]
        if block["type"] == "capability":
            complete, missing = is_complete_capability_block(block.get("markers", []), fields)
            block["signature_complete"] = complete
            block["missing_signature_evidence"] = missing
            if not complete:
                block["type"] = "unclassified"
                block["partial_candidate_types"] = ["capability"]
    elif len(candidates) > 1:
        block["type"] = "ambiguous"
    else:
        block["type"] = "unclassified"
    block.setdefault("signature_complete", block["type"] in {"decision", "collection"})
    block.setdefault("missing_signature_evidence", [])
    block.setdefault("partial_candidate_types", [])
    block["marker_evidence"] = [dict(marker) for marker in block.get("markers", [])]
    return block


def _collection_subblock(lines: list[str], parent: dict[str, Any]) -> dict[str, Any] | None:
    summaries = [index for index in range(parent["start_line"], parent["end_line"] + 1)
                 if _semantic_field_name(lines[index - 1]) == "News/catalyst summary"]
    if len(summaries) != 1:
        return None
    start = summaries[0]
    end = next((index for index in range(start + 1, parent["end_line"] + 1)
                if _semantic_field_name(lines[index - 1]) == "news_status"), start)
    return {
        "block_id": f"{parent['block_id']}-collection",
        "start_line": start,
        "end_line": end,
        "raw_lines": lines[start - 1:end],
        "text": "\n".join(lines[start - 1:end]),
        "markers": [{"name": "News/catalyst summary", "line": start, "kind": "collection"}],
        "marker_evidence": [{"name": "News/catalyst summary", "line": start, "kind": "collection"}],
        "candidate_types": ["collection"],
        "type": "collection",
    }


def parse_asset_semantic_blocks(asset_text: str) -> dict[str, Any]:
    """Segment first, then classify; unclassified blocks never become candidates."""
    lines = asset_text.splitlines()
    semantic_blocks = [classify_semantic_block(block) for block in segment_asset_blocks(asset_text)]
    decision_candidates = [block for block in semantic_blocks if block["type"] == "decision"]
    capability_candidates = [block for block in semantic_blocks if block["type"] == "capability"]
    decision = decision_candidates[0] if len(decision_candidates) == 1 else None
    capability = capability_candidates[0] if len(capability_candidates) == 1 else None
    status = "identified" if len(decision_candidates) == 1 and len(capability_candidates) == 1 else "semantic_scope_not_identifiable"
    if len(decision_candidates) > 1 or len(capability_candidates) > 1:
        status = "semantic_scope_ambiguous"
    collection = _collection_subblock(lines, decision) if decision is not None else None
    if collection is not None:
        semantic_blocks = semantic_blocks + [collection]
    marker_evidence = {
        "decision": [marker["line"] for block in decision_candidates for marker in block["markers"] if marker["kind"] == "decision"],
        "capability": [marker["line"] for block in capability_candidates for marker in block["markers"] if marker["kind"] == "capability"],
        "collection": [marker["line"] for block in semantic_blocks for marker in block["markers"] if marker["kind"] == "collection"],
    }
    result: dict[str, Any] = {"status": status, "marker_evidence": marker_evidence, "semantic_blocks": semantic_blocks}
    if decision is not None:
        result["decision_block"] = decision
    if capability is not None:
        result["capability_block"] = capability
    if collection is not None:
        result["collection_attempt_block"] = collection
    if status == "semantic_scope_ambiguous":
        result["candidates"] = {
            "decision": [block["block_id"] for block in decision_candidates],
            "capability": [block["block_id"] for block in capability_candidates],
        }
    return result


def _block_news_wrapper(block: dict[str, Any], scope: str, report: str, symbol: str, line_offset: int) -> dict[str, Any]:
    lines = block["text"].splitlines()
    fields = [{"raw": match.group(1).strip().strip("`"), "line": line_offset + number} for number, line in enumerate(lines, 1) if (match := re.match(r"^- news_status:\s*`?([^`]+)`?", line.strip()))]
    if len(fields) != 1:
        return _unavailable(scope, "semantic_scope_ambiguous" if len(fields) > 1 else "semantic_scope_not_identifiable")
    return _status_wrapper(fields[0], scope, report, symbol)


def parse_decision_status_block(block: dict[str, Any], report: str = "", symbol: str = "", line_offset: int = 0) -> dict[str, Any]:
    return _block_news_wrapper(block, "decision", report, symbol, line_offset)


def parse_capability_status_block(block: dict[str, Any], report: str = "", symbol: str = "", line_offset: int = 0) -> dict[str, Any]:
    return _block_news_wrapper(block, "capability", report, symbol, line_offset)


def parse_collection_attempt_block(block: dict[str, Any], report: str, symbol: str, line_offset: int = 0) -> dict[str, Any]:
    lines = block["text"].splitlines()
    field = next((
        {"raw": match.group(1).strip(), "line": line_offset + number}
        for number, line in enumerate(lines, 1)
        if (match := re.match(r"^- News/catalyst summary:\s*(.*)$", line.strip()))
    ), None)
    return _status_wrapper(field, "collection_attempt", report, symbol)


def parse_decision_news_status(block: dict[str, Any], report: str = "", symbol: str = "", line_offset: int = 0) -> dict[str, Any]:
    """Extract news status from an already delimited decision block."""
    return parse_decision_status_block(block, report, symbol, line_offset)


def parse_capability_news_status(block: dict[str, Any], report: str = "", symbol: str = "", line_offset: int = 0) -> dict[str, Any]:
    """Extract news status from an already delimited capability block."""
    return parse_capability_status_block(block, report, symbol, line_offset)


def parse_collection_news_status(block: dict[str, Any], report: str, symbol: str, line_offset: int = 0) -> dict[str, Any]:
    """Extract collection-attempt evidence from its semantic block."""
    return parse_collection_attempt_block(block, report, symbol, line_offset)


def parse_news_status(text: str, symbol: str, report: str) -> dict[str, Any]:
    """Parse news fields by semantic labels in the real report format.

    The scheduled report has no headings around these fields.  The decision
    block is identified by its event/macro/thesis labels; the capability block
    by guidance/sec-filings/sector labels; collection by its unique summary
    label.  No first/last or quote-relative fallback is used.
    """
    section = next((s for s in asset_sections(text) if s["symbol"] == symbol), None)
    if section is None:
        raise ValueError(f"missing asset section: {symbol}")
    blocks = parse_asset_semantic_blocks(section["text"])
    absolute_start = section["start_line"]
    ambiguous = blocks.get("candidates", {})
    decision_block = blocks.get("decision_block")
    capability_block = blocks.get("capability_block")
    collection_block = blocks.get("collection_attempt_block")
    decision_wrapper = (
        parse_decision_news_status(decision_block, report, symbol, absolute_start + decision_block["start_line"] - 1)
        if decision_block is not None else _unavailable("decision", "semantic_scope_ambiguous" if ambiguous.get("decision") else "semantic_scope_not_identifiable")
    )
    capability_wrapper = (
        parse_capability_news_status(capability_block, report, symbol, absolute_start + capability_block["start_line"] - 1)
        if capability_block is not None else _unavailable("capability", "semantic_scope_ambiguous" if ambiguous.get("capability") else "semantic_scope_not_identifiable")
    )
    collection_wrapper = (
        parse_collection_news_status(collection_block, report, symbol, absolute_start + collection_block["start_line"] - 1)
        if collection_block is not None else _unavailable("collection_attempt", "semantic_scope_ambiguous" if ambiguous.get("collection") else "semantic_scope_not_identifiable")
    )
    marker_evidence = blocks.get("marker_evidence", {})
    for wrapper, scope in ((decision_wrapper, "decision"), (capability_wrapper, "capability"), (collection_wrapper, "collection_attempt")):
        wrapper["asset"] = symbol
        wrapper["semantic_block_type"] = scope
        wrapper["locator"] = wrapper.get("source")
        wrapper["relative_path"] = wrapper.get("source", {}).get("relative_path") if wrapper.get("source") else None
        marker_key = "collection" if scope == "collection_attempt" else scope
        wrapper["marker_evidence"] = marker_evidence.get(marker_key, [])
        if blocks.get("status") == "semantic_scope_ambiguous":
            wrapper["candidates"] = blocks.get("candidates", {})
    decision_value = decision_wrapper["value"]
    capability_value = capability_wrapper["value"]
    conflict = decision_value is not None and capability_value is not None and decision_value != capability_value
    conflict_type = "decision_vs_capability" if conflict else None
    return {
        "decision_status": decision_wrapper,
        "capability_status": capability_wrapper,
        "collection_attempt_status": collection_wrapper,
        "semantic_blocks": blocks.get("semantic_blocks", []),
        "conflict": conflict,
        "conflict_type": conflict_type,
        "semantic_note": {
            "decision": "status emitted in the decision block",
            "capability": "provider/capability status emitted in the capability or provenance block",
            "collection_attempt": "whether collection was attempted or yielded news/catalyst content",
        },
    }


def _decision_weaker(before: Any, after: Any) -> bool:
    if isinstance(before, str) and isinstance(after, str) and before in DECISION_RANK and after in DECISION_RANK:
        return DECISION_RANK[after] > DECISION_RANK[before]
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return after < before
    return before != after


def detect_shadowed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark a changed event shadowed only by a later changed event on its axis."""
    ordered = sorted(events, key=lambda event: int(event.get("sequence", 0)))
    result = [dict(event, later_shadowed_by=event.get("later_shadowed_by"), shadow_status="not_shadowed") for event in ordered]
    for i, event in enumerate(result):
        if not event.get("changed"):
            continue
        for later in result[i + 1:]:
            if later.get("axis") != event.get("axis") or not later.get("changed"):
                continue
            if _decision_weaker(event.get("value_after"), later.get("value_after")):
                event["later_shadowed_by"] = later.get("trace_event_id")
                event["shadow_status"] = "fully_shadowed"
                break
            if later.get("value_after") != event.get("value_after"):
                event["later_shadowed_by"] = later.get("trace_event_id")
                event["shadow_status"] = "partially_shadowed"
                break
    return result


def classify_occurrence(occurrence: dict[str, Any], events: list[dict[str, Any]], suspected_evidence_key: str | None = None) -> str:
    """Classify only from linked events; absence of linkage is indeterminate."""
    linked_ids = set(occurrence.get("linked_trace_event_ids", []))
    linked = [event for event in events if event.get("trace_event_id") in linked_ids]
    if not linked:
        return "unable_to_determine"
    if any(event.get("effect_type") == "explicit_override" for event in linked):
        return "explicit_override"
    changed = [event for event in linked if event.get("changed")]
    if not changed:
        return "no_op"
    if any(event.get("later_shadowed_by") for event in changed):
        return "shadowed"
    by_key_axis: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for event in changed:
        by_key_axis.setdefault((event.get("evidence_key"), event.get("axis")), []).append(event)
    if any(len(matches) >= 2 and not any(item.get("intentional_multi_effect") for item in matches) for matches in by_key_axis.values()):
        return "confirmed_duplicate_penalty"
    if suspected_evidence_key is not None and any(event.get("evidence_key") != suspected_evidence_key for event in changed):
        return "independent_gate"
    return "independent_gate" if len({event.get("evidence_key") for event in changed}) > 1 else "unable_to_determine"


def occurrence_ids_from_source(occurrences: list[dict[str, Any]]) -> list[str]:
    ids = []
    for item in occurrences:
        payload = {
            "symbol": item.get("symbol"),
            "rule": item.get("original_rule_or_reason"),
            "before": item.get("original_decision_before"),
            "after": item.get("original_decision_after"),
            "locator": item.get("original_source_locator"),
            "payload": item.get("original_payload"),
        }
        ids.append(hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:24])
    return ids


def recover_original_occurrences(root: Path) -> dict[str, Any]:
    candidates = []
    for path in sorted(root.rglob("*.json")):
        name = path.name.lower()
        if any(token in name for token in ("occurrence", "suspected", "penalt")):
            candidates.append(_rel(path, root))
    usable = []
    for relative in candidates:
        if relative.endswith("phase3-suspected-penalties-reclassification.json"):
            continue
        try:
            payload = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("occurrences"), list) and payload.get("source_kind") == "original_runtime_artifact":
            usable.append(payload)
    if usable:
        payload = usable[0]
        occurrences = payload["occurrences"]
        ids = occurrence_ids_from_source(occurrences)
        normalized = []
        for sequence, (item, occurrence_id) in enumerate(zip(occurrences, ids), 1):
            normalized.append({
                "original_occurrence_id": occurrence_id,
                "original_sequence": sequence,
                "symbol": item["symbol"],
                "original_rule_or_reason": item["original_rule_or_reason"],
                "original_decision_before": item.get("original_decision_before"),
                "original_decision_after": item.get("original_decision_after"),
                "original_source_locator": item.get("original_source_locator"),
                "original_payload": item.get("original_payload", {}),
                "linked_trace_event_ids": [],
                "classification": None,
            })
        return {
            "status": "recovered",
            "original_occurrence_set_status": "recovered",
            "source_search_status": "original_runtime_artifact_found",
            "recovered_occurrence_count": len(normalized),
            "reconciled": True,
            "confirmed_duplicates": 0,
            "source": payload.get("source"),
            "occurrences": normalized,
        }
    return {
        "status": "original_occurrence_set_unavailable",
        "original_occurrence_set_status": "unavailable",
        "source_search_status": "completed_no_original_runtime_artifact",
        "recovered_occurrence_count": None,
        "reconciled": False,
        "confirmed_duplicates": 0,
        "reason": "original_occurrence_artifact_not_found",
        "searched_artifacts": candidates,
        "occurrences": [],
    }


def write_original_occurrence_artifact(root: Path) -> dict[str, Any]:
    """Write the explicit unavailable contract (or a recovered source contract)."""
    payload = recover_original_occurrences(root)
    target = root / "reports/audit/phase3-original-suspected-occurrences.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_json(payload), encoding="utf-8")
    return payload


def derive_hims_trace_from_raw(raw_text: str) -> dict[str, Any]:
    """Derive HIMS from raw labels using the real scoring decision helpers."""
    from types import SimpleNamespace
    from advisor.scoring import _apply_cap, _base_decision, _weaker_cap

    lines = raw_text.splitlines()
    def field(name: str) -> str | None:
        prefix = f"- {name}:"
        return next((line.split(":", 1)[1].strip().strip("`") for line in lines if line.startswith(prefix)), None)

    investment_score = float(field("Investment Quality Score") or 0)
    swing_score = float(field("Swing Trade Score") or 0)
    scored_like = SimpleNamespace(investment_quality_score=investment_score, swing_trade_score=swing_score)
    base = _base_decision(scored_like)
    severity = next((line.split(":", 1)[1].strip().strip("`") for line in lines if line.startswith("- missing_data_severity:")), None)
    current_cap = "tradeable"
    current_cap = _weaker_cap(current_cap, "technical_unvalidated") if severity == "high" else current_cap
    intermediate = _apply_cap(base, current_cap)
    reason_codes = field("reason_codes") or ""
    has_market_cap_alert = "below_minimum_market_cap" in reason_codes
    override_rule = "classify_asset.below_minimum_market_cap_override" if has_market_cap_alert else None
    decision_before = intermediate if override_rule else None
    decision_after = "avoid" if override_rule else intermediate
    observed_final = field("Decisao")
    return {
        "base_decision": base,
        "severity_intermediate_decision": intermediate,
        "override_rule_id": override_rule,
        "override_decision_before": decision_before,
        "override_decision_after": decision_after,
        "effect_type": "explicit_override" if override_rule else None,
        "observed_final_decision": observed_final,
        "final_decision": decision_after,
        "replay_divergence": decision_after != observed_final,
        "source_code_locator": {
            "path": CODE_PATH,
            "function": "classify_asset",
            "branch_signature": 'if "below_minimum_market_cap" in alerts',
        },
    }


def rule_catalog(scoring_path: Path) -> list[dict[str, Any]]:
    source = scoring_path.read_text(encoding="utf-8")
    signatures = [
        ("classify_asset.initial_confidence_limiting_cap", "if _has_confidence_limiting_data_gap(limitations)"),
        ("classify_asset.high_missing_data_severity_cap", "if _missing_data_severity(limitations) == \"high\":"),
        ("classify_asset.below_minimum_market_cap_override", "if \"below_minimum_market_cap\" in alerts:"),
        ("classify_asset.decision_confidence_cap", "if decision_confidence_score < 65:"),
        ("classify_asset.technical_unvalidated_predicate", "if _is_technical_unvalidated("),
        ("classify_asset.win_rate_branch", "if not has_low_sample and backtest_stats and backtest_stats.win_rate_2r is not None:"),
        ("classify_asset.uncollected_context_limits", "_apply_uncollected_context_limits(scored, backtest_stats, limitations)"),
    ]
    lines = source.splitlines()
    result = []
    for rule_id, signature in signatures:
        if signature not in source:
            continue
        line = next(i for i, value in enumerate(lines, 1) if signature in value)
        result.append({
            "rule_id": rule_id,
            "source_code_locator": {
                "path": CODE_PATH,
                "function": "classify_asset",
                "branch_signature": signature,
                "line_range": str(line),
            },
        })
    return result


def _trace_for_asset(section: dict[str, Any], report: str) -> list[dict[str, Any]]:
    decision = _first(section, "Decisao")
    event_id = hashlib.sha256(f"{section['symbol']}|decision|{decision['line'] if decision else 0}".encode()).hexdigest()[:20]
    observed = decision["raw"] if decision else None
    events = [{
        "trace_event_id": event_id,
        "sequence": 1,
        "rule_id": "classify_asset.reported_decision",
        "source_code_locator": {"path": report, "function": "report_decision_block", "line_range": str(decision["line"] if decision else section["start_line"])},
        "evidence_key": f"report:{section['symbol']}:decision",
        "axis": "decision",
        "value_before": None,
        "candidate_value": observed,
        "value_after": observed,
        "changed": False,
        "later_shadowed_by": None,
        "trace_provenance": "observed_report_field_not_runtime_gate_trace",
    }]
    return detect_shadowed(events)


def replay_capability(root: Path, report: Path, code_ref: str) -> dict[str, Any]:
    sections = [s for s in asset_sections(report.read_text(encoding="utf-8")) if any(f["raw"] == "primary_watchlist" for f in s["fields"].get("universe_origin", []))]
    machine_files = [
        _rel(path, root) for path in sorted((root / ".tmp/nightly-review/2026-07-15/run-29429941131-main").rglob("*"))
        if path.is_file() and path.suffix.lower() in {".json", ".db"}
    ]
    assets = []
    for section in sections:
        decision = (_first(section, "Decisao") or {"raw": None})["raw"]
        missing = ["ScoredAsset", "AssetSnapshot", "BacktestStats", "risk_plan", "event", "alerts", "limitations", "scores"]
        assets.append({
            "symbol": section["symbol"],
            "original_replay": {"expected_decision": decision, "actual_decision": None, "parity": False, "reason": "exact_classify_asset_inputs_not_recoverable"},
            "counterfactuals": [{
                "name": name,
                "input_transformations": [],
                "removed_evidence_keys": [],
                "counterfactual_decision": None,
                "status": "unavailable",
                "reason": "exact_classify_asset_inputs_not_recoverable",
            } for name in ("without_optional_news_context", "without_backtest_low_sample_effect", "only_real_risk_gates")],
            "inputs": {"observed": [], "reconstructed_losslessly": [], "unavailable": missing},
        })
    return {
        "schema_version": "phase3-counterfactual-replay-capability-v1",
        "source": {"report": _rel(report, root), "code_ref": code_ref, "network_used": False, "machine_readable_artifacts": machine_files},
        "manifest": {"observed": [], "reconstructed_losslessly": [], "unavailable": ["ScoredAsset", "AssetSnapshot", "BacktestStats", "risk_plan", "event", "alerts", "limitations", "scores"]},
        "summary": {"asset_count": len(assets), "parity_complete_count": 0, "counterfactual_calculated_count": 0, "counterfactual_unavailable_count": len(assets) * 3},
        "assets": assets,
    }


def _asset_baseline(section: dict[str, Any], report: str, full_report_text: str | None = None) -> dict[str, Any]:
    def raw(name: str, default: Any = None) -> Any:
        field = _first(section, name)
        return field["raw"] if field else default
    alerts = [item.strip() for item in raw("Principais alertas", "").split(",") if item.strip()]
    limitations = [item.strip() for item in raw("Dados ausentes ou limitacoes", "").split(",") if item.strip()]
    decision = raw("Decisao")
    sample_field = _first(section, "Qualidade da amostra")
    sample_raw = sample_field["raw"] if sample_field else None
    sample = {"baixa": "low", "média": "medium", "media": "medium", "alta": "high"}.get(sample_raw.lower(), sample_raw.lower() if sample_raw else None)
    sample_quality = _observed(sample, sample_raw, report, section["symbol"], "sample_quality", sample_field["line"]) if sample_field else _unavailable("sample_quality", "sample_quality_not_exposed")
    crypto_flow = None
    if raw("Tipo") == "crypto":
        crypto_flow = {
            name: {"value": None, "raw_value": None, "provenance": "unavailable", "source": None, "reason": "not_exposed_in_source_report"}
            for name in ("funding", "open_interest_current", "open_interest_change", "cvd", "premium", "liquidations")
        }
    return {
        "symbol": section["symbol"],
        "asset_type": raw("Tipo"),
        "source_decision": decision,
        "scores": {"investment_quality_score": raw("Investment Quality Score"), "swing_trade_score": raw("Swing Trade Score")},
        "data_quality_score": raw("data_quality_score"),
        "decision_confidence_score": raw("decision_confidence_score"),
        "missing_data_severity": raw("missing_data_severity"),
        "alerts": alerts,
        "limitations": limitations,
        "backtest": {"sample_size": raw("sample_size"), "expected_value_r": raw("expected_value_r"), "win_rate_2r": None, "win_rate_2r_status": "not_exposed_by_scheduled_artifact", "sample_quality": sample_quality},
        "crypto_flow": crypto_flow,
        "news_status": parse_news_status(full_report_text, section["symbol"], report) if full_report_text is not None else parse_news_status("\n".join([f"## {section['symbol']}", section["text"]]), section["symbol"], report),
        "gate_trace": _trace_for_asset(section, report),
        "original_replay": {"expected_decision": decision, "actual_decision": None, "parity": False, "reason": "exact_classify_asset_inputs_not_recoverable"},
        "counterfactuals": [],
    }


def _mutation_results(root: Path) -> dict[str, Any]:
    return run_real_mutation_subprocesses(root)


def _workspace_manifest(workspace: Path) -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(workspace).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(item for item in workspace.rglob("*") if item.is_file())
    ]


def _manifest_hash(manifest: list[dict[str, str]]) -> str:
    return hashlib.sha256(_json(manifest).encode("utf-8")).hexdigest()


def _normalise_output(value: str, workspace: Path) -> str:
    text = (value or "").replace(str(workspace), "<temporary_workspace>")
    text = re.sub(r"[A-Za-z]:[\\/][^\r\n ]+", "<path>", text)
    text = re.sub(r"Ran (\d+) tests in [0-9.]+s", r"Ran \1 tests in <duration>s", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "<memory_address>", text)
    text = re.sub(r"pid[= ]\d+", "pid=<process_id>", text, flags=re.IGNORECASE)
    return text.replace("\\", "/").replace("\r\n", "\n").strip()


def _copy_mutation_workspace(root: Path, workspace: Path, report: Path) -> None:
    for relative in ("scripts/phase3a2_forensics.py", "tests/phase3_mutation_assertions.py"):
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)
    ignore_generated = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(root / "advisor", workspace / "advisor", ignore=ignore_generated)
    shutil.copytree(root / "tests/fixtures", workspace / "tests/fixtures", ignore=ignore_generated)
    source_report = workspace / "source/main-report.md"
    source_report.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(report, source_report)


def _run_mutation_generation(root: Path, report: Path) -> dict[str, Any]:
    specs = [
        ("M1", "replace raw report sample quality baixa with media", "source/main-report.md", "replace Qualidade da amostra: baixa -> média", "test_M1_sample_quality_provenance", lambda path: path.write_text(path.read_text(encoding="utf-8").replace("Qualidade da amostra: baixa", "Qualidade da amostra: média"), encoding="utf-8")),
        ("M2", "mutate the real HIMS trace derivation override", "scripts/phase3a2_forensics.py", "remove real override application", "test_M2_hims_override", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('decision_after = "avoid" if override_rule else intermediate', 'decision_after = intermediate if override_rule else intermediate', 1), encoding="utf-8")),
        ("M3", "weaken the real capability signature", "scripts/phase3a2_forensics.py", "accept any capability marker as complete", "test_M3_news_scopes", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('    return not missing, list(dict.fromkeys(missing))', '    return bool(marker_names & _CAPABILITY_MARKERS), []', 1), encoding="utf-8")),
        ("M4", "insert synthetic occurrence into the source fixture", "tests/fixtures/phase3/occurrence_source_fixture.json", "append immutable record", "test_M4_synthetic_occurrence_rejected", lambda path: path.write_text(_json({**json.loads(path.read_text(encoding="utf-8")), "immutable_records": json.loads(path.read_text(encoding="utf-8"))["immutable_records"] + [{"original_occurrence_id": "synthetic", "symbol": "FAKE"}]}), encoding="utf-8")),
        ("M5", "remove one occurrence from the source fixture", "tests/fixtures/phase3/occurrence_source_fixture.json", "remove final immutable record", "test_M5_removed_occurrence_rejected", lambda path: path.write_text(_json({**json.loads(path.read_text(encoding="utf-8")), "immutable_records": json.loads(path.read_text(encoding="utf-8"))["immutable_records"][:-1]}), encoding="utf-8")),
        ("M6", "replace unavailable win_rate null with zero", "tests/fixtures/phase3/unavailable_contracts.json", "null -> 0", "test_M6_unavailable_win_rate", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('"value": null', '"value": 0', 1), encoding="utf-8")),
        ("M7", "replace unavailable counterfactual null with decision", "tests/fixtures/phase3/unavailable_contracts.json", "null -> avoid", "test_M7_unavailable_counterfactual", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('"counterfactual_decision": null', '"counterfactual_decision": "avoid"'), encoding="utf-8")),
        ("M8", "mutate the real shadow detector implementation", "scripts/phase3a2_forensics.py", "remove later_shadowed_by assignment", "test_M8_shadow_detector_contract", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('event["later_shadowed_by"] = later.get("trace_event_id")', 'event["later_shadowed_by"] = None', 1), encoding="utf-8")),
        ("M9", "force unavailable occurrence contract to recovered 49", "scripts/phase3a2_forensics.py", "None -> 49 and false -> true in contract", "test_M9_no_forced_total", lambda path: path.write_text(path.read_text(encoding="utf-8").replace('"recovered_occurrence_count": None', '"recovered_occurrence_count": 49', 1).replace('"reconciled": False', '"reconciled": True', 1), encoding="utf-8")),
    ]
    command = [sys.executable, "-m", "unittest", "tests.phase3_mutation_assertions"]
    import os
    env_base = {key: os.environ[key] for key in ("PATH", "SystemRoot", "SYSTEMROOT", "LANG", "PYTHONIOENCODING") if key in os.environ}
    env_base["PYTHONPATH"] = "."
    env_base["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory() as base_temp:
        base_workspace = Path(base_temp) / "workspace_base"
        base_workspace.mkdir()
        _copy_mutation_workspace(root, base_workspace, report)
        initial_base_manifest = _workspace_manifest(base_workspace)
        initial_base_manifest_hash = _manifest_hash(initial_base_manifest)
        control_workspace = Path(base_temp) / "clone_control"
        shutil.copytree(base_workspace, control_workspace)
        control_manifest_before = _workspace_manifest(control_workspace)
        mutation_workspaces: dict[str, Path] = {}
        mutation_manifests: dict[str, list[dict[str, str]]] = {}
        for mutation_id, *_ in specs:
            clone = Path(base_temp) / f"clone_{mutation_id}"
            shutil.copytree(base_workspace, clone)
            mutation_workspaces[mutation_id] = clone
            mutation_manifests[mutation_id] = _workspace_manifest(clone)
            if mutation_manifests[mutation_id] != initial_base_manifest:
                raise RuntimeError(f"workspace manifest mismatch before {mutation_id}")
        control_env = {**env_base, "PHASE3_MUTATION_ID": "control", "PHASE3_REPORT": "source/main-report.md"}
        control = subprocess.run(command, cwd=control_workspace, env=control_env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        control_output = (control.stdout or "") + "\n" + (control.stderr or "")
        control_executed_count = int(re.search(r"Ran (\d+) tests", control_output).group(1)) if re.search(r"Ran (\d+) tests", control_output) else None
        control_failed_tests = sorted(set(re.findall(r"FAIL: (test_[A-Za-z0-9_]+)", control_output)))
        control_error_tests = sorted(set(re.findall(r"ERROR: (test_[A-Za-z0-9_]+)", control_output)))
        control_manifest_after = _workspace_manifest(control_workspace)
        base_manifest_after_control = _workspace_manifest(base_workspace)
        if base_manifest_after_control != initial_base_manifest:
            raise RuntimeError("workspace_base changed during control execution")
        base_manifest_hash = initial_base_manifest_hash
        control_run = {"command": ["python", "-m", "unittest", "tests.phase3_mutation_assertions"], "cwd_relative": ".", "pythonpath": ".", "non_secret_environment": ["LANG", "PATH", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "SystemRoot", "SYSTEMROOT"], "exit_code": control.returncode, "status": "passed" if control.returncode == 0 else "failed", "executed_test_count": control_executed_count, "selected_test_suite": "tests.phase3_mutation_assertions", "expected_failed_test": None, "observed_failed_tests": control_failed_tests, "unexpected_failed_tests": control_error_tests, "source_base_manifest": initial_base_manifest, "initial_base_manifest_sha256": initial_base_manifest_hash, "workspace_manifest_sha256": initial_base_manifest_hash, "clone_manifest_before_mutation": control_manifest_before, "clone_manifest_before_test": control_manifest_before, "clone_manifest_after_mutation": control_manifest_before, "clone_manifest_after_test": control_manifest_after, "normalized_output": {"exit_code": control.returncode, "status": "passed" if control.returncode == 0 else "failed"}}
        if control.returncode != 0:
            raise RuntimeError("mutation control_run failed; no mutation artifact written")
        results = []
        for mutation_id, description, target_file, operation, expected_test, mutate in specs:
            clone = mutation_workspaces[mutation_id]
            with tempfile.TemporaryDirectory():
                clone_manifest_before = mutation_manifests[mutation_id]
                if clone_manifest_before != initial_base_manifest:
                    raise RuntimeError(f"workspace manifest mismatch before {mutation_id}")
                target = clone / target_file
                target_hash_before = hashlib.sha256(target.read_bytes()).hexdigest()
                mutate(target)
                target_hash_after = hashlib.sha256(target.read_bytes()).hexdigest()
                clone_manifest_after_mutation = _workspace_manifest(clone)
                env = {**env_base, "PHASE3_MUTATION_ID": mutation_id, "PHASE3_REPORT": "source/main-report.md"}
                completed = subprocess.run(command, cwd=clone, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
                combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
                failed_tests = sorted(set(re.findall(r"FAIL: (test_[A-Za-z0-9_]+)", combined)))
                error_tests = sorted(set(re.findall(r"ERROR: (test_[A-Za-z0-9_]+)", combined)))
                executed_count = int(re.search(r"Ran (\d+) tests", combined).group(1)) if re.search(r"Ran (\d+) tests", combined) else None
                unexpected_tests = sorted(set(failed_tests + error_tests) - {expected_test})
                expected_failure_observed = completed.returncode != 0 and executed_count == 9 and expected_test in failed_tests and not unexpected_tests
                clone_manifest_after = _workspace_manifest(clone)
                results.append({"mutation_id": mutation_id, "description": description, "target_file": target_file, "operation": operation, "expected_test_ids": [expected_test], "command": ["python", "-m", "unittest", "tests.phase3_mutation_assertions"], "exit_code": completed.returncode, "expected_failure_observed": expected_failure_observed, "observed_failed_tests": failed_tests, "normalized_output": {"exit_code": completed.returncode, "observed_failed_test_ids": failed_tests, "assertion_category": "expected assertion failed" if expected_failure_observed else "unexpected subprocess/setup result"}, "base_workspace_manifest_sha256": base_manifest_hash, "clone_workspace_manifest_sha256_before": _manifest_hash(clone_manifest_before), "target_hash_before": target_hash_before, "target_hash_after": target_hash_after, "mutation_changed_target": target_hash_before != target_hash_after, "temporary_workspace": True, "mutation_detected": expected_failure_observed and target_hash_before != target_hash_after, "failure_reason": "expected assertion failed" if expected_failure_observed else "unexpected subprocess/setup result"})
                results[-1].update({"source_base_manifest": initial_base_manifest, "cwd_relative": ".", "pythonpath": ".", "non_secret_environment": ["LANG", "PATH", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE", "SystemRoot", "SYSTEMROOT"], "clone_manifest_before_mutation": clone_manifest_before, "clone_manifest_before_test": clone_manifest_before, "clone_manifest_after_mutation": clone_manifest_after_mutation, "clone_manifest_after_test": clone_manifest_after, "executed_test_count": executed_count, "selected_test_suite": "tests.phase3_mutation_assertions", "expected_failed_test": expected_test, "unexpected_failed_tests": unexpected_tests, "observed_failed_tests": failed_tests})
        results.sort(key=lambda item: item["mutation_id"])
        detected_count = sum(item["mutation_detected"] for item in results)
        if detected_count != len(specs):
            details = [(item["mutation_id"], item["exit_code"], item["executed_test_count"], item["observed_failed_tests"], item["unexpected_failed_tests"], item["failure_reason"]) for item in results]
            raise RuntimeError(f"only {detected_count}/{len(specs)} mutations detected; refusing artifact: {details}")
        base_manifest_final = _workspace_manifest(base_workspace)
        if base_manifest_final != initial_base_manifest:
            raise RuntimeError("workspace_base changed during mutation execution")
        return {"schema_version": "phase3-mutation-test-results-v4", "control_run": control_run, "workspace_base": {"initial_manifest": initial_base_manifest, "initial_manifest_sha256": initial_base_manifest_hash, "after_control_manifest": base_manifest_after_control, "after_control_manifest_sha256": _manifest_hash(base_manifest_after_control), "final_manifest": base_manifest_final, "final_manifest_sha256": _manifest_hash(base_manifest_final), "unchanged": base_manifest_final == initial_base_manifest, "pre_execution_clone_manifests": {mutation_id: mutation_manifests[mutation_id] for mutation_id, *_ in specs}}, "mutations": results, "all_mutations_detected": True, "detected_count": detected_count, "total_count": len(specs), "summary": {"mutation_count": len(specs), "failed_as_expected": detected_count, "unexpected_passes": 0}}


def run_real_mutation_subprocesses(root: Path) -> dict[str, Any]:
    first = _run_mutation_generation(root, root / ".tmp/nightly-review/2026-07-15/run-29429941131-main/history/2026-07-15-main.md")
    second = _run_mutation_generation(root, root / ".tmp/nightly-review/2026-07-15/run-29429941131-main/history/2026-07-15-main.md")
    first_hash = hashlib.sha256(_json(first).encode("utf-8")).hexdigest()
    second_hash = hashlib.sha256(_json(second).encode("utf-8")).hexdigest()
    if first_hash != second_hash:
        raise RuntimeError("mutation artifact is not deterministic")
    second["determinism"] = {"verified": True, "generation_hashes": [first_hash, second_hash]}
    return second


def freeze_previous_artifacts(root: Path) -> list[str]:
    paths = [
        root / "reports/audit/phase3-real-cycle-baseline.json",
        root / "reports/audit/phase3-baseline-reconciliation.json",
        root / "reports/audit/phase3-suspected-penalties-reclassification.json",
        root / "docs/PHASE3_BASELINE_METHODOLOGY.md",
    ]
    saved = []
    for path in paths:
        if not path.exists():
            continue
        suffix = ".superseded-needs-revision" + path.suffix
        target = path.with_name(path.stem + suffix)
        if path.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["baseline_status"] = "superseded_needs_revision"
                payload["superseded_reason_codes"] = REASONS
                target.write_text(_json(payload), encoding="utf-8")
                path.write_text(_json(payload), encoding="utf-8")
            except json.JSONDecodeError:
                shutil.copy2(path, target)
        else:
            shutil.copy2(path, target)
        saved.append(_rel(target, root))
    return saved


def build_all(root: Path, report: Path, code_ref: str = CODE_REF, *, run_mutations: bool = True) -> dict[str, Any]:
    report = report.resolve()
    report_rel = _rel(report, root)
    text = report.read_text(encoding="utf-8")
    sections = [s for s in asset_sections(text) if any(f["raw"] == "primary_watchlist" for f in s["fields"].get("universe_origin", []))]
    if len(sections) != 15:
        raise ValueError(f"expected 15 primary assets, found {len(sections)}")
    frozen = freeze_previous_artifacts(root)
    original = recover_original_occurrences(root)
    trace = {
        "schema_version": "phase3-trace-linkage-v1",
        "source": {"report": report_rel, "runtime_trace_artifact": None, "status": "runtime_trace_unavailable"},
        "summary": {"status": "not_reconcilable_original_source_missing", "original_occurrence_count": None, "linked_occurrence_count": None, "unable_to_determine_count": None, "real_trace_event_count": 0, "confirmed_duplicate_penalty_count": 0, "shadowed_count": None},
        "occurrences": original["occurrences"],
        "trace_events": [],
        "rule_catalog": rule_catalog(root / CODE_PATH),
        "classification_method": "mechanical_from_linked_events_only",
        "shadow_detector": {"name": "same_axis_later_weaker_or_override", "implemented": True, "applied_to_real_occurrence_set": False, "reason": "original occurrence set unavailable"},
    }
    news_rows = []
    for section in sections:
        status = parse_news_status(text, section["symbol"], report_rel)
        semantic = parse_asset_semantic_blocks(section["text"])
        semantic_blocks = [
            {
                "block_id": block["block_id"],
                "type": block["type"],
                "start_line": block["start_line"],
                "end_line": block["end_line"],
                "marker_evidence": block["marker_evidence"],
                "news_status_present": any(_semantic_field_name(line) == "news_status" for line in block["raw_lines"]),
                "used_for_scope": block["type"] in {"decision", "capability", "collection"},
                "signature_complete": block.get("signature_complete", False),
                "partial_candidate_types": block.get("partial_candidate_types", []),
                "missing_signature_evidence": block.get("missing_signature_evidence", []),
            }
            for block in semantic.get("semantic_blocks", [])
        ]
        news_rows.append({"symbol": section["symbol"], **status, "semantic_blocks": semantic_blocks, "raw_occurrence_count": len(section["fields"].get("news_status", [])), "conflict": status["conflict"], "interpretation": "decision and capability are separate scopes"})
    replay = replay_capability(root, report, code_ref)
    baseline = {
        "schema_version": "phase3-baseline-v3",
        "baseline_status": "source_grounded_needs_review",
        "source": {"report": report_rel, "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(), "code_ref": code_ref, "primary_universe_only": True},
        "methodology": {"occurrence_source": original["status"], "trace": "no runtime gate trace emitted in package", "counterfactuals": "only real classify_asset replay; unavailable when exact inputs are absent", "news_status": "scoped decision/capability/collection fields"},
        "assets": [_asset_baseline(section, report_rel, text) for section in sections],
        "summary": {"asset_count": len(sections), "original_occurrence_set_status": original["original_occurrence_set_status"], "confirmed_duplicate_penalty_count": 0, "counterfactual_parity_complete": replay["summary"]["parity_complete_count"]},
    }
    reconciliation = {
        "schema_version": "phase3-baseline-reconciliation-v3",
        "source": {"report": report_rel, "code_ref": code_ref},
        "sample_quality": [{"symbol": section["symbol"], "raw_value": _first(section, "Qualidade da amostra")["raw"] if _first(section, "Qualidade da amostra") else None, "parsed_value": _asset_baseline(section, report_rel, text)["backtest"]["sample_quality"], "provenance": "observed"} for section in sections],
        "original_occurrence_set": original["status"],
    }
    methodology = """# Phase 3A.2.3 Fechamento dos Bloqueadores do Framework Forense

- Ocorrências: `original_occurrence_set_unavailable`; o pacote não contém o artefato runtime original. Nenhuma ocorrência sintética é promovida.
- Trace: `runtime_trace_unavailable`; o detector `same_axis_later_weaker_or_override` existe, ordena por `sequence` e é aplicado apenas às fixtures controladas.
- News: decision, capability e collection são resolvidos por marcadores semânticos dos blocos reais do relatório; `quote_status` não define escopo. Locators são 1-based e apontam para a linha bruta.
- Crypto: `funding`, `open_interest_current`, `open_interest_change`, `cvd`, `premium` e `liquidations` aparecem sempre para ETH, SOL, HYPE e BTC; ausência é `null/unavailable` com `source=null`.
- Replay: paridade com `classify_asset` é 0/15; os 45 contrafactuais são `null/unavailable` porque os inputs equivalentes não existem. Nenhum simulador paralelo é usado.
- Mutações: um único `workspace_base` é criado e manifestado antes do control run; o control run e cada mutação usam clones idênticos desse mesmo estado inicial, com o mesmo comando, interpretador e `PYTHONPATH`. O artefato só é escrito quando 9/9 falham pelo teste esperado, os hashes dos alvos mudam e os manifests pré-mutação coincidem.
- Determinismo: duas gerações isoladas são normalizadas para dados estruturados e exigem hashes byte a byte idênticos; duração, caminhos temporários, endereços de memória, PIDs e line endings não são persistidos.
- Sample quality: o valor é um wrapper observado (`value`, `raw_value`, `provenance`, `source`), validado por snippets brutos independentes para AMD, ETH, HIMS, MSFT e USAR. HIMS é derivado de `hims_source_raw.md`, sem conclusões pré-preenchidas.
- HIMS bruto: `tests/fixtures/phase3/hims_source_raw.md` é exatamente o trecho das linhas 839–847 do relatório original, comparado linha a linha no teste independente.
- Contrato de ocorrências: `phase3-original-suspected-occurrences.json` declara diretamente status indisponível, contagem `null`, reconciliação falsa e duplicidades zero; não há recuperação artificial.
- Integridade fixa: `advisor/scoring.py` SHA-256 `16B7A0A4C93ECD0E633B7DF560C585F10398426EDA8861D7D06C38E3449BAFAD`; relatório SHA-256 `410DCADEA7EB22DCB71C53A45B05E1C70484B038D87565847C966351805D4E8A`.
- Rede: não utilizada. Scoring não alterado.
"""
    mutation = _mutation_results(root) if run_mutations else {"status": "deferred_to_artifact_generation"}
    capability_matrix = []
    for row in news_rows:
        for block in row["semantic_blocks"]:
            if block["type"] == "capability":
                capability_matrix.append({"symbol": row["symbol"], "markers": [marker["name"] for marker in block["marker_evidence"]], "required_signature_matched": block["signature_complete"], "optional_markers": [marker["name"] for marker in block["marker_evidence"] if marker["name"] not in {"Data source", "provider", "news_status"}], "classification": block["type"], "start_line": block["start_line"], "end_line": block["end_line"], "news_status_present": block["news_status_present"]})
    artifacts = {"baseline": baseline, "reconciliation": reconciliation, "original": original, "trace": trace, "news": {"schema_version": "phase3-news-status-reconciliation-v2", "source": {"report": report_rel}, "parser_strategy": "semantic_block_segmentation_and_classification", "capability_signature_strategy": "required_semantic_marker_groups", "partial_capability_blocks_ignored": True, "positional_fallback_used": False, "unclassified_blocks_ignored": True, "capability_signature_policy": _CAPABILITY_SIGNATURE_POLICY, "capability_signature_matrix": capability_matrix, "assets": news_rows, "multi_status_assets": [row["symbol"] for row in news_rows if row["raw_occurrence_count"] > 1]}, "replay": replay, "mutation": mutation, "methodology": methodology, "frozen": frozen}
    return artifacts


def write_artifacts(root: Path, artifacts: dict[str, Any], outputs: dict[str, Path] | None = None) -> None:
    outputs = outputs or {
        "baseline": root / "reports/audit/phase3-baseline-v3.json",
        "original": root / "reports/audit/phase3-original-suspected-occurrences.json",
        "trace": root / "reports/audit/phase3-trace-linkage.json",
        "news": root / "reports/audit/phase3-news-status-reconciliation.json",
        "replay": root / "reports/audit/phase3-counterfactual-replay-capability.json",
        "mutation": root / "reports/audit/phase3-mutation-test-results.json",
        "reconciliation": root / "reports/audit/phase3-baseline-v3-reconciliation.json",
        "methodology": root / "docs/PHASE3_BASELINE_METHODOLOGY.md",
    }
    for key, path in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        value = artifacts[key]
        path.write_text(value if key == "methodology" else _json(value), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--code-ref", default=CODE_REF)
    args = parser.parse_args()
    root = args.root.resolve()
    report = args.report if args.report.is_absolute() else root / args.report
    artifacts = build_all(root, report, args.code_ref)
    write_artifacts(root, artifacts)
    print(json.dumps({"original_occurrence_status": artifacts["original"]["status"], "asset_count": len(artifacts["baseline"]["assets"]), "frozen": artifacts["frozen"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
