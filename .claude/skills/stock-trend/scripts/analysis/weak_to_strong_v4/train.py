#!/usr/bin/env python3
"""Train one immutable monthly V4 research bundle from frozen records."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import tempfile
import math
from typing import Iterable, Mapping, Sequence

from .v4_shadow_scoring import fit_v4_shadow_bundle, validate_v4_shadow_bundle


PIPELINE_SCHEMA_VERSION = "v4-monthly-training/v1"
FROZEN_COST_PER_SIDE = 0.001


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso_date(value: object, field: str) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _calendar(path: Path) -> tuple[list[str], str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    values = payload.get("trading_dates") if isinstance(payload, Mapping) else payload
    if not isinstance(values, list):
        raise ValueError("calendar must be an array or contain trading_dates")
    days = [_iso_date(value, "calendar entry") for value in values]
    if not days or days != sorted(set(days)):
        raise ValueError("calendar must contain ordered unique trading dates")
    return days, _sha256(path)


def _files(inputs: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for source in inputs:
        if source.is_file():
            files.append(source)
            continue
        root = source / "shards" if (source / "shards").is_dir() else source
        files.extend(sorted(root.glob("*.jsonl")))
    if not files:
        raise ValueError("no training JSON/JSONL files found")
    return files


def _rows(path: Path) -> Iterable[dict[str, object]]:
    if path.suffix == ".jsonl":
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, Mapping):
                        raise ValueError(f"{path} contains a non-object record")
                    yield dict(value)
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, Mapping):
        value = next((value[key] for key in ("training_records", "records")
                      if isinstance(value.get(key), list)), value)
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a record array")
    for row in value:
        if not isinstance(row, Mapping):
            raise ValueError(f"{path} contains a non-object record")
        yield dict(row)


def _training_record(row: Mapping[str, object]) -> dict[str, object] | None:
    pattern = str(row.get("probe_pattern") or row.get("signal_type") or "").upper()
    if pattern not in {"T1", "T2"}:
        return None
    result = dict(row)
    execution = row.get("execution")
    if result.get("exit_date") is None and isinstance(execution, Mapping):
        exit_data = execution.get("exit")
        gross = execution.get("gross_return")
        if (execution.get("status") == "CLOSED" and isinstance(exit_data, Mapping)
                and exit_data.get("date") and isinstance(gross, (int, float))
                and not isinstance(gross, bool)):
            result["exit_date"] = str(exit_data["date"])
            result["net_return"] = (
                (1 + float(gross)) * (1 - FROZEN_COST_PER_SIDE)
                / (1 + FROZEN_COST_PER_SIDE) - 1
            )
    net_return = result.get("net_return")
    if (result.get("exit_date") is None or isinstance(net_return, bool)
            or not isinstance(net_return, (int, float))
            or not math.isfinite(float(net_return))):
        return None
    result["won_net"] = float(net_return) > 0
    return result


def _boundaries(calendar: Sequence[str], validation_start: str) -> dict[str, str]:
    start = _iso_date(validation_start, "validation_start")
    if start not in calendar:
        raise ValueError("validation_start must be a trading date")
    index = calendar.index(start)
    if index < 6:
        raise ValueError("calendar requires six sessions before validation_start")
    month = start[:7]
    if any(day.startswith(month) for day in calendar[:index]):
        raise ValueError("validation_start must be the first trading date of its month")
    valid = [day for day in calendar[index:] if day.startswith(month)]
    if not valid:
        raise ValueError("calendar does not cover the validation month")
    return {"validation_start": start, "training_cutoff": calendar[index - 1],
            "purge_cutoff": calendar[index - 6], "valid_until": valid[-1]}


def _eligible_record(record: Mapping[str, object], boundaries: Mapping[str, str]) -> bool:
    signal_date = _iso_date(
        record.get("signal_date") or record.get("trade_date") or record.get("date"),
        "training signal date",
    )
    exit_date = _iso_date(record.get("exit_date"), "training exit date")
    if (signal_date > boundaries["purge_cutoff"]
            or exit_date > boundaries["training_cutoff"]):
        return False
    if not str(record.get("event_id") or "").strip():
        raise ValueError("eligible training record requires a stable event_id")
    feature_block = record.get("features")
    feature_asof = (feature_block.get("feature_asof")
                    if isinstance(feature_block, Mapping) else None)
    if feature_asof is None and isinstance(feature_block, Mapping):
        nested = feature_block.get("features")
        feature_asof = (nested.get("feature_asof")
                        if isinstance(nested, Mapping) else None)
    feature_asof = feature_asof or record.get("feature_asof")
    if _iso_date(feature_asof, "training feature_asof") != signal_date:
        raise ValueError("training feature_asof must equal signal date")
    return True


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode()
    if path.exists():
        if path.read_bytes() == encoded:
            return
        raise ValueError(f"immutable model already exists with different content: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def train(inputs: Sequence[Path], calendar_path: Path, validation_start: str,
          output: Path) -> dict[str, object]:
    calendar, calendar_hash = _calendar(calendar_path)
    boundaries = _boundaries(calendar, validation_start)
    files = _files(inputs)
    seen: set[tuple[object, ...]] = set()
    records: list[dict[str, object]] = []
    source_hashes = []
    source_seen = 0
    for path in files:
        source_hashes.append({"path": str(path), "sha256": _sha256(path)})
        for row in _rows(path):
            source_seen += 1
            key = tuple(row.get("key") or ())
            if key and key in seen:
                raise ValueError(f"duplicate event key: {key}")
            seen.add(key)
            record = _training_record(row)
            if record is None:
                continue
            if _eligible_record(record, boundaries):
                records.append(record)
    if not records:
        raise ValueError("no mature records remain after cutoff and purge")
    identity = {"schema_version": PIPELINE_SCHEMA_VERSION,
                "sources": source_hashes, "calendar_sha256": calendar_hash,
                "boundaries": boundaries, "cost_per_side": FROZEN_COST_PER_SIDE}
    data_version = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    model_version = f"V4-SHADOW-{validation_start[:7].replace('-', '')}-{data_version[:12]}"
    bundle = fit_v4_shadow_bundle(
        records, market_calendar=calendar, model_version=model_version,
        data_version=data_version, outcome_field="net_return", win_field="won_net",
        minimum_training_signal_days=120, **boundaries,
    )
    bundle.update(
        pipeline_schema_version=PIPELINE_SCHEMA_VERSION,
        source_records_seen=source_seen,
        eligible_training_records=len(records),
        training_cost_per_side=FROZEN_COST_PER_SIDE,
        training_source_identity=identity,
        limitations=[
            "historical research sample; not independent forward evidence",
            "historical membership, point-in-time adjustment and fill feasibility are unverified",
            "V4 remains research-only and does not change production recommendation eligibility",
        ],
    )
    bundle["bundle_hash"] = hashlib.sha256(json.dumps(
        {key: value for key, value in bundle.items() if key != "bundle_hash"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    validate_v4_shadow_bundle(bundle)
    for signal_type, model in bundle["models"].items():
        if int(model.get("training_signal_days", 0)) < 120:
            raise ValueError(f"{signal_type} training window has fewer than 120 signal days")
    _atomic_write(output, bundle)
    manifest_path = output.with_suffix(".manifest.json")
    manifest = {
        "schema_version": "v4-monthly-training-manifest/v1",
        "model_path": str(output), "model_sha256": _sha256(output),
        "model_version": bundle["model_version"], "bundle_hash": bundle["bundle_hash"],
        "source_records_seen": source_seen, "eligible_training_records": len(records),
        "training_cutoff": bundle["training_cutoff"],
        "purge_cutoff": bundle["purge_cutoff"], "valid_until": bundle["valid_until"],
        "training_cost_per_side": FROZEN_COST_PER_SIDE,
        "actual_max_signal_date": max(str(record.get("signal_date") or
                                          record.get("trade_date") or record.get("date"))
                                      for record in records),
        "actual_max_exit_date": max(str(record["exit_date"]) for record in records),
        "models": {key: {"training_records": value["training_records"],
                          "training_signal_days": value["training_signal_days"]}
                   for key, value in bundle["models"].items()},
        "limitations": bundle["limitations"],
    }
    _atomic_write(manifest_path, manifest)
    return {"status": "trained", "model_path": str(output),
            "manifest_path": str(manifest_path),
            "model_version": bundle["model_version"], "bundle_hash": bundle["bundle_hash"],
            "source_records_seen": source_seen, "eligible_training_records": len(records),
            "training_cutoff": bundle["training_cutoff"],
            "purge_cutoff": bundle["purge_cutoff"], "valid_until": bundle["valid_until"],
            "models": {key: {"training_records": value["training_records"],
                              "training_signal_days": value["training_signal_days"]}
                       for key, value in bundle["models"].items()}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training", action="append", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--validation-start", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(train(args.training, args.calendar, args.validation_start, args.output),
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
