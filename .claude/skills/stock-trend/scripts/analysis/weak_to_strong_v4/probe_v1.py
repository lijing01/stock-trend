"""Causal T1/T2/T3 daily probe detector used by weak-to-strong v3."""

from __future__ import annotations

import math
import statistics

from .weak_to_strong import _canonical_digest, _normalise_date, _normalise_rows

SCHEMA_VERSION = "daily-probe/v1"
DETECTOR_VERSION = "1.0.0-research.1"
DEFAULT_CONFIG = {
    "atr_window": 14, "level_window": 20, "volume_window": 5,
    "t1_lower_min": .35, "t1_close_min": .60, "t1_range_atr_min": .60,
    "level_near_pct": .035, "level_near_atr": .60,
    "t2_gap_min": 2, "t2_gap_max": 10, "t2_anchor_vol_min": 1.15,
    "t2_anchor_body_min": .30, "t2_anchor_range_atr_min": .80,
    "t2_low_min": .975, "t2_low_max": 1.035, "t2_volume_max": .80,
    "t2_range_max": .95, "t2_close_min": .30,
    "t12_confirm_days": 5, "t12_confirm_high": 1.002,
    "t12_confirm_vol": 1.15, "t12_failure_low": .99,
    "t3_upper_min": .37, "t3_range_atr_min": .85,
    "t3_resistance_min": .992, "t3_volume20_min": 1.45,
    "t3_prior_close_min": .97, "t3_absorb_days": 3,
    "t3_absorb_max_days": 8, "t3_absorb_volume_max": .85,
    "t3_expiry_days": 20,
    "t3_breakout_high": 1.002, "t3_breakout_close": 1.02,
    "t3_breakout_vol": 1.20,
}


def _config(value):
    config = dict(DEFAULT_CONFIG)
    if value is not None:
        if not isinstance(value, dict) or set(value) - set(config):
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        config.update(value)
    integers = {"atr_window", "level_window", "volume_window", "t2_gap_min",
                "t2_gap_max", "t12_confirm_days", "t3_absorb_days",
                "t3_absorb_max_days", "t3_expiry_days"}
    for key, number in config.items():
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or number <= 0
                or (key in integers and int(number) != number)):
            return dict(DEFAULT_CONFIG), ["config_invalid"]
        if key in integers:
            config[key] = int(number)
    if (config["t2_gap_min"] >= config["t2_gap_max"]
            or config["t3_absorb_days"] > config["t3_absorb_max_days"]
            or config["t3_absorb_max_days"] >= config["t3_expiry_days"]):
        return dict(DEFAULT_CONFIG), ["config_invalid"]
    return config, []


def _tr(rows, index):
    row = rows[index]
    previous = rows[index - 1]["close"] if index else row["close"]
    return max(row["high"] - row["low"], abs(row["high"] - previous),
               abs(row["low"] - previous))


def _atr(rows, index, window):
    if index + 1 < window:
        return None
    return sum(_tr(rows, i) for i in range(index + 1 - window, index + 1)) / window


def _prior(rows, index, field, window, *, median=False):
    if index < window:
        return None
    values = [row[field] for row in rows[index - window:index]]
    return statistics.median(values) if median else sum(values) / window


def _features(rows, index, config):
    row = rows[index]
    spread = row["high"] - row["low"]
    if spread <= 0:
        return {}
    atr = _atr(rows, index, config["atr_window"])
    vol5 = _prior(rows, index, "volume", config["volume_window"])
    vol20 = _prior(rows, index, "volume", config["level_window"], median=True)
    prior = rows[index - config["level_window"]:index] if index >= config["level_window"] else []
    return {
        "range": spread, "body_ratio": abs(row["close"] - row["open"]) / spread,
        "upper_ratio": (row["high"] - max(row["open"], row["close"])) / spread,
        "lower_ratio": (min(row["open"], row["close"]) - row["low"]) / spread,
        "close_pos": (row["close"] - row["low"]) / spread, "atr14": atr,
        "support20": min((item["low"] for item in prior), default=None),
        "resistance20": max((item["high"] for item in prior), default=None),
        "prior_ma20": sum((item["close"] for item in prior), 0) / len(prior) if prior else None,
        "volume_ratio5": row["volume"] / vol5 if vol5 else None,
        "volume_ratio20_median": row["volume"] / vol20 if vol20 else None,
    }


def _new(pattern, rows, index, features, config, anchor=None):
    row = rows[index]
    anchor_date = rows[anchor]["date"] if anchor is not None else ""
    candidate_id = _canonical_digest({"pattern": pattern, "date": row["date"],
                                      "anchor": anchor_date,
                                      "parameter_hash": _canonical_digest(config)})
    return {"candidate_id": candidate_id, "pattern": pattern,
            "candidate_date": row["date"], "known_at": row["date"],
            "anchor_date": anchor_date, "_index": index, "low": row["low"],
            "high": row["high"], "close": row["close"], "volume": row["volume"],
            "atr14": features["atr14"], "features": features,
            "status": "candidate", "status_date": row["date"],
            "status_events": [{"status": "candidate", "date": row["date"]}]}


def _detect(rows, index, config):
    f = _features(rows, index, config)
    required = ("range", "atr14", "support20", "resistance20", "prior_ma20",
                "volume_ratio5", "volume_ratio20_median")
    if any(f.get(key) is None for key in required):
        return []
    row = rows[index]
    found = []
    def near(level):
        return abs(row["low"] - level) <= max(config["level_near_pct"] * level,
                                               config["level_near_atr"] * f["atr14"])
    if (f["lower_ratio"] >= config["t1_lower_min"]
            and f["close_pos"] >= config["t1_close_min"]
            and f["range"] >= config["t1_range_atr_min"] * f["atr14"]
            and (near(f["support20"]) or near(f["prior_ma20"]))):
        found.append(_new("T1_LOWER_WICK", rows, index, f, config))
    matches = []
    for anchor in range(max(0, index - config["t2_gap_max"]),
                        index - config["t2_gap_min"] + 1):
        a, af = rows[anchor], _features(rows, anchor, config)
        if not af.get("range") or not af.get("atr14"):
            continue
        anchor_ok = (a["close"] < a["open"]
                     and (af.get("volume_ratio5") or 0) >= config["t2_anchor_vol_min"]
                     and af["body_ratio"] >= config["t2_anchor_body_min"]
                     and af["range"] >= config["t2_anchor_range_atr_min"] * af["atr14"])
        retest_ok = (config["t2_low_min"] * a["low"] <= row["low"]
                     <= config["t2_low_max"] * a["low"]
                     and row["volume"] <= config["t2_volume_max"] * a["volume"]
                     and f["range"] <= config["t2_range_max"] * af["range"]
                     and f["close_pos"] >= config["t2_close_min"]
                     and row["close"] >= a["low"])
        if anchor_ok and retest_ok:
            matches.append(anchor)
    if matches:
        found.append(_new("T2_LOW_VOLUME_RETEST", rows, index, f, config,
                          anchor=max(matches)))
    if (f["upper_ratio"] >= config["t3_upper_min"]
            and f["range"] >= config["t3_range_atr_min"] * f["atr14"]
            and row["high"] >= config["t3_resistance_min"] * f["resistance20"]
            and f["volume_ratio20_median"] >= config["t3_volume20_min"]
            and index and row["close"] >= config["t3_prior_close_min"] * rows[index - 1]["close"]):
        found.append(_new("T3_UPPER_WICK", rows, index, f, config))
    return found


def _set(candidate, status, date):
    candidate["status"], candidate["status_date"] = status, date
    candidate["status_events"].append({"status": status, "date": date})


def _advance_t12(candidate, rows, config):
    origin = candidate["_index"]
    for index in range(origin + 1, len(rows)):
        age, row = index - origin, rows[index]
        if row["low"] < config["t12_failure_low"] * candidate["low"]:
            _set(candidate, "invalidated", row["date"])
            return
        if candidate["status"] == "candidate" and age <= config["t12_confirm_days"]:
            vol5 = _prior(rows, index, "volume", config["volume_window"])
            if (row["close"] > config["t12_confirm_high"] * candidate["high"]
                    and vol5 and row["volume"] / vol5 >= config["t12_confirm_vol"]):
                _set(candidate, "confirmed", row["date"])
        if candidate["status"] == "candidate" and age >= config["t12_confirm_days"]:
            _set(candidate, "expired", row["date"])
            return


def _advance_t3(candidate, rows, config):
    origin, absorbed = candidate["_index"], False
    for index in range(origin + 1, len(rows)):
        age, row = index - origin, rows[index]
        if (row["low"] < candidate["low"] - candidate["atr14"]
                or row["close"] < candidate["close"] - 1.5 * candidate["atr14"]):
            _set(candidate, "invalidated", row["date"])
            return
        if (config["t3_absorb_days"] <= age <= config["t3_absorb_max_days"]
                and not absorbed):
            recent = rows[index + 1 - config["t3_absorb_days"]:index + 1]
            if statistics.median(item["volume"] for item in recent) <= config["t3_absorb_volume_max"] * candidate["volume"]:
                absorbed = True
                _set(candidate, "absorbed", row["date"])
        if absorbed and candidate["status"] != "confirmed":
            prior_high = max(item["high"] for item in rows[index - 5:index])
            vol5 = _prior(rows, index, "volume", config["volume_window"])
            if (row["close"] > config["t3_breakout_high"] * prior_high
                    and row["close"] > config["t3_breakout_close"] * candidate["close"]
                    and vol5 and row["volume"] / vol5 >= config["t3_breakout_vol"]):
                _set(candidate, "confirmed", row["date"])
        if age >= config["t3_expiry_days"] and candidate["status"] != "confirmed":
            _set(candidate, "expired_no_breakout" if absorbed else "expired_no_absorption",
                 row["date"])
            return


def analyze_daily_probes(payload, *, as_of_date, min_candidate_date=None, config=None):
    """Detect candidates and advance each state only through ``as_of_date``."""
    target, (resolved, config_errors) = _normalise_date(as_of_date), _config(config)
    result = {"schema_version": SCHEMA_VERSION, "detector_version": DETECTOR_VERSION,
              "parameter_hash": _canonical_digest(resolved), "as_of_date": target,
              "evidence_status": "ready", "reason_codes": [], "candidates": []}
    if not target:
        result.update(evidence_status="data_insufficient", reason_codes=["as_of_date_invalid"])
        return result
    rows, row_errors, duplicates = _normalise_rows(payload, target)
    reasons = list(dict.fromkeys(config_errors + row_errors))
    if config_errors or len(rows) < resolved["level_window"] + 1:
        reasons.append("history_insufficient")
        result.update(evidence_status="data_insufficient",
                      reason_codes=list(dict.fromkeys(reasons)))
        return result
    if reasons or duplicates:
        if duplicates:
            reasons.append("kline_duplicate")
        result.update(evidence_status="data_insufficient",
                      reason_codes=list(dict.fromkeys(reasons)),
                      duplicate_dates=duplicates)
        return result
    result["reason_codes"], result["duplicate_dates"] = [], []
    minimum = _normalise_date(min_candidate_date) if min_candidate_date else ""
    candidates = []
    for index, row in enumerate(rows):
        if not minimum or row["date"] >= minimum:
            candidates.extend(_detect(rows, index, resolved))
    for candidate in candidates:
        (_advance_t3 if candidate["pattern"] == "T3_UPPER_WICK" else _advance_t12)(
            candidate, rows, resolved)
        candidate.pop("_index")
    result["candidates"] = candidates
    return result


def select_probe(candidates, rows, structure_start=""):
    """Select one causally eligible probe and calculate its v3 bonus.

    ``rows`` are normalized bars with ISO ``date`` values and end on the score
    date.  The function does not inspect bars beyond that final row.
    """
    if not rows or not isinstance(candidates, list):
        return None
    dates = [row.get("date") for row in rows]
    position = {date: index for index, date in enumerate(dates) if date}
    start = _normalise_date(structure_start) if structure_start else ""
    as_of = dates[-1]
    ranked = []
    preference = {"T2_LOW_VOLUME_RETEST": 2, "T1_LOWER_WICK": 1}
    for candidate in candidates:
        date = candidate.get("candidate_date")
        if date not in position or (start and date < start):
            continue
        age = len(rows) - 1 - position[date]
        if age < 0 or age > 20 or candidate.get("status") in {
                "invalidated", "expired", "expired_no_breakout",
                "expired_no_absorption"}:
            continue
        pattern, status = candidate.get("pattern"), candidate.get("status")
        base = 0
        if pattern in {"T1_LOWER_WICK", "T2_LOW_VOLUME_RETEST"}:
            if status == "candidate" and age <= 5:
                base = 2
            elif status == "confirmed":
                confirmed = next((event["date"] for event in candidate.get("status_events", [])
                                  if event.get("status") == "confirmed"), "")
                if confirmed in position and len(rows) - 1 - position[confirmed] <= 10:
                    base = 6
        if not base:
            continue
        bonus = base + (1 if age <= 5 else 0)
        since = rows[position[date]:]
        holds = (all(row["low"] >= candidate["low"] for row in since)
                 and rows[-1]["close"] > candidate["close"])
        bonus = min(8, bonus + (1 if holds else 0))
        selected = dict(candidate)
        selected.update(bonus=bonus, base_bonus=base, candidate_age_sessions=age,
                        recent_bonus=1 if age <= 5 else 0,
                        holds_low_bonus=1 if holds else 0, selected_as_of=as_of)
        ranked.append((bonus, position[date], preference.get(pattern, 0), selected))
    return max(ranked, key=lambda item: item[:3])[3] if ranked else None
