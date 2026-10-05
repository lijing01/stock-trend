#!/usr/bin/env python3
"""Detached lifecycle for today's post-report research stages."""
import argparse
import copy
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[4]
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))

from analysis import evolution_job as evolution
from core.recommendation_snapshot import canonical_json, content_sha256

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_ROOT = Path(evolution.CACHE_DIR) / "evolution" / "background"
REVIEW_HTML_ROOT_NAME = "review_html"
TERMINAL = {"completed", "partial", "failed", "timed_out", "interrupted", "launch_failed"}
RESEARCH_DONE = {"completed", "continue_accumulating", "skipped", "scope_unverified",
                 "baseline_mismatch", "input_incomplete", "insufficient_data"}
CORE_DONE = {"completed", "insufficient_data", "healthy", "review_required",
             "fallback_required"}


class _StageDeadline(BaseException):
    """Private deadline signal which ordinary business exception handlers cannot swallow."""


def _budgets(manifest):
    schema = manifest.get("schema_version")
    if schema in {"daily-review-html-background/v1", "daily-review-reports-background/v2"}:
        return {"html": 300}
    if schema not in {None, "today-recommendation-background/v1",
                      "today-recommendation-background/v2"}:
        raise ValueError("unsupported_schema")
    def positive(value):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("invalid_budget_config")
        return value
    if schema == "today-recommendation-background/v2":
        if "budget_seconds" in manifest:
            positive(manifest["budget_seconds"])
        try:
            budgets = {name: positive(manifest[name + "_budget_seconds"])
                       for name in ("lock_wait", "close", "weekly", "monitor")}
        except KeyError as exc:
            raise ValueError("invalid_budget_config") from exc
    else:
        core = positive(manifest.get("budget_seconds", 300))
        budgets = {"lock_wait": min(30, core), "close": core, "weekly": core, "monitor": core}
    budgets["factor_daily"] = positive(manifest.get("factor_daily_budget_seconds", 10))
    budgets["factor_evaluation"] = positive(manifest.get("factor_evaluation_budget_seconds", 20))
    return budgets


def _task_dir(task_id, root):
    return Path(root) / task_id


def _write(path, payload):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json(payload) + b"\n")
    os.replace(temporary, path)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _now():
    return datetime.now(SHANGHAI).isoformat()


def ensure_task(manifest, root=DEFAULT_ROOT):
    try:
        _budgets(manifest)
    except ValueError as exc:
        return {"status": "launch_failed", "reason": str(exc)}
    root = Path(root)
    identity = {key: value for key, value in manifest.items() if key not in {"requested_at"}}
    task_id = content_sha256(identity)[:16]
    directory = _task_dir(task_id, root)
    manifest_path = directory / "manifest.json"
    status_path = directory / "status.json"
    if not manifest_path.exists():
        _write(manifest_path, {**copy.deepcopy(manifest), "task_id": task_id})
    if status_path.exists():
        # Rehydrate checkpoints/result metadata so repeated foreground calls
        # expose the same per-stage state as an explicit --status query.
        return read_status(task_id, root=root)
    status = {"status": "queued", "task_id": task_id, "stage": "queued",
              "created_at": _now(), "heartbeat_at": _now()}
    _write(status_path, status)
    return status


def read_status(task_id, root=DEFAULT_ROOT):
    directory = _task_dir(task_id, root)
    try:
        status = _read(directory / "status.json")
        manifest = _read(directory / "manifest.json")
        try:
            budgets = _budgets(manifest)
        except ValueError as exc:
            return {**status, "task_id": task_id, "status": "failed", "reason": str(exc)}
        checkpoint_path = directory / "checkpoints.json"
        if checkpoint_path.is_file():
            status = {**status, "stages": {
                name: {**item.get("result", {}), "input_sha256": item.get("input_sha256")}
                for name, item in _read(checkpoint_path).get("stages", {}).items()}}
        result_path = status.get("result_path") or str(directory / "result.json")
        if Path(result_path).is_file():
            result = _read(result_path)
            fresh_result = (status.get("status") != "running" or
                            str(result.get("finished_at") or "") >= str(status.get("started_at") or ""))
            if fresh_result and result.get("status") in {
                    "completed", "partial", "failed", "timed_out", "interrupted"}:
                status = {**status, "status": result["status"], "stage": "done",
                          "finished_at": result.get("finished_at"), "result_path": result_path}
                _write(directory / "status.json", status)
                return {"task_id": task_id, **status}
        if status.get("status") == "running":
            pid = status.get("pid")
            if pid and not _pid_alive(pid):
                # A detached worker may briefly disappear from the caller's
                # process namespace.  Do not declare it interrupted before
                # the task budget has elapsed; the worker may still finish.
                try:
                    manifest = _read(directory / "manifest.json")
                    started = datetime.fromisoformat(status.get("started_at") or status.get("created_at"))
                    budget = sum(budgets.values())
                    stale = (datetime.now(SHANGHAI) - started).total_seconds() > budget + 5
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    stale = False
                if stale:
                    status = {**status, "status": "interrupted", "reason": "worker_not_running"}
                    _write(directory / "status.json", status)
        return {"task_id": task_id, **status}
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {"task_id": task_id, "status": "missing"}


def update_status(task_id, root=DEFAULT_ROOT, **changes):
    directory = _task_dir(task_id, root)
    current = read_status(task_id, root=root)
    current.update(changes)
    current["task_id"] = task_id
    current["heartbeat_at"] = _now()
    _write(directory / "status.json", current)
    return current


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def launch_task(task_id, root=DEFAULT_ROOT):
    root = Path(root); directory = _task_dir(task_id, root)
    directory.mkdir(parents=True, exist_ok=True)
    status = read_status(task_id, root=root)
    if status.get("reason") in {"invalid_budget_config", "unsupported_schema"}:
        return status
    if status.get("status") == "running" and _pid_alive(status.get("pid")):
        return status
    log = (directory / "worker.log").open("ab")
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--run", task_id, "--root", str(root)],
            cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
            env={**os.environ, "TZ": "Asia/Shanghai"},
        )
    finally:
        log.close()
    return update_status(task_id, root=root, status="running", stage="starting", pid=process.pid,
                         started_at=_now())


def _review_html_root(root=DEFAULT_ROOT):
    return Path(root) / REVIEW_HTML_ROOT_NAME


def ensure_review_html_task(html_path, markdown_path=None, *, data_date=None, artifact_path=None,
                            yaml_path=None, config_sha256=None, root=DEFAULT_ROOT):
    """Create an independent task for replacing both daily-review blocks."""
    path = str(Path(html_path).resolve())
    markdown_path = Path(markdown_path) if markdown_path else Path(html_path).with_suffix(".md")
    manifest = {
        "schema_version": "daily-review-reports-background/v2",
        "kind": "daily-review-reports",
        "html_path": path,
        "markdown_path": str(markdown_path.resolve()),
        "data_date": data_date,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "yaml_path": str(yaml_path) if yaml_path else None,
        "config_sha256": config_sha256,
    }
    return ensure_task(manifest, root=_review_html_root(root))


def read_review_html_status(task_id, root=DEFAULT_ROOT):
    return read_status(task_id, root=_review_html_root(root))


def launch_review_html_task(task_id, root=DEFAULT_ROOT):
    """Launch the tiny detached report updater and return its task status."""
    review_root = _review_html_root(root)
    directory = _task_dir(task_id, review_root)
    directory.mkdir(parents=True, exist_ok=True)
    status = read_status(task_id, root=review_root)
    if status.get("status") == "completed":
        return status
    if status.get("status") == "running" and _pid_alive(status.get("pid")):
        return status
    # Mark the task before spawning.  The worker is intentionally tiny and
    # can finish before Popen returns; checking the status again below then
    # preserves its completed result instead of overwriting it with running.
    started_at = _now()
    update_status(task_id, root=review_root, status="running", stage="starting",
                  pid=None, started_at=started_at)
    log = (directory / "worker.log").open("ab")
    try:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__)), "--update-html", task_id,
             "--root", str(review_root)],
            cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
            env={**os.environ, "TZ": "Asia/Shanghai"},
        )
    finally:
        log.close()
    status = read_status(task_id, root=review_root)
    if status.get("status") in TERMINAL:
        return status
    return update_status(task_id, root=review_root, status="running", stage="starting",
                         pid=process.pid, started_at=started_at)


def run_review_html_task(task_id, root=DEFAULT_ROOT):
    """Replace the marked observation block in both reports and persist state."""
    review_root = _review_html_root(root)
    directory = _task_dir(task_id, review_root)
    manifest = _read(directory / "manifest.json")
    update_status(task_id, root=review_root, status="running", stage="analyzing",
                  pid=os.getpid())
    try:
        from analysis import market_regime
        from analysis.observation_list_analysis import analyze_observation_list
        from reporting.observation_report_update import update_observation_reports
        yaml_path = Path(manifest.get("yaml_path") or market_regime.OBSERVATION_LIST_FILE)
        analysis_failure = None
        try:
            expected_hash = manifest.get("config_sha256")
            if (expected_hash is not None
                    and hashlib.sha256(yaml_path.read_bytes()).hexdigest() != expected_hash):
                raise ValueError("observation_yaml_changed")
            analyze_observation_list(
                manifest["data_date"], yaml_path=yaml_path,
                artifact_path=manifest.get("artifact_path"))
        except Exception as exc:
            failure_reason = (str(exc) if isinstance(exc, ValueError) and str(exc)
                              else type(exc).__name__)
            analysis_failure = {
                "status": "unavailable", "items": [],
                "reason": f"YAML 观察分析失败: {failure_reason}",
            }
        update_status(task_id, root=review_root, status="running", stage="updating",
                      pid=os.getpid())
        if analysis_failure:
            result = update_observation_reports(
                manifest["html_path"], manifest.get("markdown_path"),
                state=analysis_failure, pending=False,
                render_html=market_regime.render_observation_list_html,
                render_markdown=market_regime.render_observation_list_markdown,
                block_start=market_regime.OBSERVATION_BLOCK_START,
                block_end=market_regime.OBSERVATION_BLOCK_END)
            result["analysis"] = {
                "status": "failed", "reason": analysis_failure["reason"]}
        else:
            result = market_regime.update_observation_list_reports(
                manifest["html_path"], manifest.get("markdown_path"),
                data_date=manifest.get("data_date"),
                artifact_path=manifest.get("artifact_path"), yaml_path=yaml_path)
        result.update({"task_id": task_id, "finished_at": _now()})
        task_status = "partial" if (
            analysis_failure or result.get("status") == "degraded") else "completed"
        result["status"] = task_status
        _write(directory / "result.json", result)
        update_status(task_id, root=review_root, status=task_status, stage="done",
                      result_path=str(directory / "result.json"),
                      files=result.get("files"),
                      partial_failures=result.get("partial_failures"),
                      observation_status=result.get("observation_status"),
                      analysis=result.get("analysis"),
                      finished_at=result["finished_at"])
        return result
    except Exception as exc:
        result = {"task_id": task_id, "status": "failed",
                  "reason": type(exc).__name__, "finished_at": _now()}
        _write(directory / "result.json", result)
        update_status(task_id, root=review_root, status="failed", stage="done",
                      reason=type(exc).__name__, result_path=str(directory / "result.json"),
                      finished_at=result["finished_at"])
        return result


def resume_review_html_task(task_id, root=DEFAULT_ROOT):
    status = read_review_html_status(task_id, root=root)
    if status.get("status") == "missing":
        return status
    if status.get("status") == "completed":
        return status
    if status.get("status") == "running" and _pid_alive(status.get("pid")):
        return status
    return launch_review_html_task(task_id, root=root)


def _lock(root, deadline):
    handle = open(Path(root) / "postprocess.lock", "a+")
    if fcntl is None:
        handle.close()
        raise ValueError("postprocess_lock_unsupported")
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError("postprocess_lock_timeout")
            time.sleep(min(.25, max(.01, deadline - time.monotonic())))


@contextmanager
def _stage_timeout(seconds):
    if not hasattr(signal, "setitimer"):
        raise RuntimeError("background_deadline_unsupported")
    def timeout(_signum, _frame):
        raise _StageDeadline("background_stage_timeout")
    old_handler = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _stage_failure_package(name, *, as_of, task_id, attempt_id, attempt_started_at, reason):
    return evolution._package(name, {"status": "failed", "failure_class": "runtime",
        "reason": reason, "as_of": as_of, "background_task_id": task_id,
        "attempt_id": attempt_id, "attempt_started_at": attempt_started_at})


def _stage(task_id, root, name, callback, job_root, budget_seconds, *,
           as_of, attempt_id, attempt_started_at):
    update_status(task_id, root=root, status="running", stage=name)
    persistence_started = False
    try:
        with _stage_timeout(budget_seconds):
            run = callback()
            run = evolution._package(name, {**run["content"], "as_of": as_of,
                "background_task_id": task_id, "attempt_id": attempt_id,
                "attempt_started_at": attempt_started_at})
            persistence_started = True
            persistence = evolution._save(run, job_root)
        return {**run["content"], "persistence": persistence}
    except (_StageDeadline, TimeoutError):
        status, reason = "timed_out", "background_stage_timeout"
    except Exception as exc:
        status, reason = "failed", (str(exc) if str(exc) == "background_deadline_unsupported"
                                   else type(exc).__name__)
    if persistence_started:
        # Even a timeout during a write leaves the current close unverified.
        return {"status": "failed", "reason": "persistence_failed"}
    run = _stage_failure_package(name, as_of=as_of, task_id=task_id,
        attempt_id=attempt_id, attempt_started_at=attempt_started_at, reason=reason)
    try:
        persistence = evolution._save(run, job_root)
    except Exception:
        return {"status": "failed", "reason": "persistence_failed"}
    return {**run["content"], "status": status, "persistence": persistence}


def _research_stage(task_id, root, name, callback, seconds):
    update_status(task_id, root=root, status="running", stage=name)
    return run_research_stage(callback, seconds)


def run_research_stage(callback, seconds):
    """Bound an optional research callback without failing the formal pipeline."""
    try:
        with _stage_timeout(seconds):
            result = callback()
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            return {"status": "failed", "reason": "invalid_research_result"}
        return result
    except (_StageDeadline, TimeoutError):
        return {"status": "timed_out", "reason": "background_stage_timeout"}
    except Exception as exc:
        reason = str(exc) if str(exc) == "background_deadline_unsupported" else type(exc).__name__
        return {"status": "failed", "reason": reason}


def _factor_daily(as_of, state_root):
    from analysis import factor_ablation
    return factor_ablation.run_daily(as_of, state_root=state_root)


def _factor_evaluation(as_of, state_root):
    from analysis import factor_ablation
    return factor_ablation.run_evaluation(as_of, state_root=state_root)


def _stage_input(manifest, stage, result):
    inputs = {"manifest": {key: item for key, item in manifest.items() if key != "requested_at"},
              "stage": stage,
              "close": (result.get("stages", {}).get("close")
                        if stage == "factor_ablation_evaluation" else None)}
    if stage in {"weekly", "monitor"}:
        inputs.update({"dependency_version": 2, "close_execution_job_id":
            (result.get("stages", {}).get("close", {}).get("persistence") or {}).get("job_id")})
    return inputs


def _verified_persistence(value):
    try:
        reference = value["persistence"]
        run = _read(reference["path"])
        digest = content_sha256(run["content"])
        return (run["job_id"] == reference["job_id"] == digest[:16]
                and run["content_sha256"] == digest)
    except (KeyError, OSError, ValueError, TypeError):
        return False


def _core_done(stage, value):
    if stage == "weekly" and value.get("reason") == "already_completed_this_week":
        return value.get("status") == "skipped" and _verified_persistence(value)
    return value.get("status") in CORE_DONE


def _checkpoint(directory, manifest, stage, value, result):
    inputs = _stage_input(manifest, stage, result)
    digest = content_sha256(inputs)
    path = directory / "checkpoints.json"
    try:
        current = _read(path)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        current = {"stages": {}}
    current["stages"][stage] = {"input_sha256": digest, "input": inputs, "result": value}
    _write(path, current)
    return digest


def _cached_stage(directory, manifest, stage, result):
    try:
        saved = _read(directory / "checkpoints.json")["stages"][stage]
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    digest = content_sha256(_stage_input(manifest, stage, result))
    value = saved.get("result")
    if saved.get("input_sha256") != digest or not isinstance(value, dict):
        return None
    if stage.startswith("factor_ablation_"):
        return value if value.get("status") in RESEARCH_DONE else None
    if not _core_done(stage, value) or not _verified_persistence(value):
        return None
    return value


def _run_or_reuse(directory, manifest, result, stage, callback):
    value = _cached_stage(directory, manifest, stage, result)
    if value is None:
        value = callback()
        _checkpoint(directory, manifest, stage, value, result)
    result["stages"][stage] = value
    return value


def run_task(task_id, root=DEFAULT_ROOT):
    root = Path(root); directory = _task_dir(task_id, root)
    manifest = _read(directory / "manifest.json")
    update_status(task_id, root=root, status="running", stage="starting", pid=os.getpid(),
                  started_at=_now(), reason=None)
    result = {"task_id": task_id, "as_of": manifest.get("as_of"), "stages": {}}
    lock = None
    try:
        budgets = _budgets(manifest)
        if not hasattr(signal, "setitimer"):
            raise ValueError("background_deadline_unsupported")
        attempt_id, attempt_started_at = uuid.uuid4().hex, _now()
        job_root = Path(manifest["job_root"])
        state_root = Path(manifest.get("state_root") or job_root.parent)
        as_of = manifest["as_of"]
        sessions = manifest.get("trading_sessions") or []
        _run_or_reuse(directory, manifest, result, "factor_ablation_daily", lambda:
            _research_stage(task_id, root, "factor_ablation_daily",
                            lambda: _factor_daily(as_of, state_root), budgets["factor_daily"])
            if manifest.get("research_eligible", False) else
            {"status": "skipped", "reason": "not_post_close_final"})
        update_status(task_id, root=root, status="running", stage="waiting_for_lock")
        lock = _lock(root, time.monotonic() + budgets["lock_wait"])
        def core_stage(name, callback):
            return _stage(task_id, root, name, callback, job_root, budgets[name],
                as_of=as_of, attempt_id=attempt_id, attempt_started_at=attempt_started_at)
        close = _run_or_reuse(directory, manifest, result, "close", lambda:
            core_stage("close", lambda: evolution.run_close(as_of)))
        if not _verified_persistence(close):
            for name in ("weekly", "monitor"):
                value = {"status": "skipped", "reason": "current_close_unavailable"}
                result["stages"][name] = value
                _checkpoint(directory, manifest, name, value, result)
            result["stages"]["factor_ablation_evaluation"] = {"status": "deferred_core_incomplete"}
            _checkpoint(directory, manifest, "factor_ablation_evaluation",
                        result["stages"]["factor_ablation_evaluation"], result)
            raise ValueError("current_close_persistence_failed")
        if close.get("status") == "completed":
            def weekly():
                prior = evolution.weekly_completed_run(job_root, as_of)
                if prior:
                    return {"status": "skipped", "reason": "already_completed_this_week",
                            "source_content_sha256": prior["content_sha256"],
                            "persistence": prior["persistence"]}
                return core_stage("weekly", lambda: evolution.run_weekly(as_of))
            _run_or_reuse(directory, manifest, result, "weekly", weekly)
        else:
            result["stages"]["weekly"] = {"status": "skipped", "reason": "close_not_complete"}
            _checkpoint(directory, manifest, "weekly", result["stages"]["weekly"], result)
        _run_or_reuse(directory, manifest, result, "monitor", lambda:
            core_stage("monitor", lambda:
                evolution.monitoring_snapshot(job_root=job_root, expected_trading_days=sessions,
                                              as_of=as_of)))
        if (not manifest.get("research_eligible", False)):
            result["stages"]["factor_ablation_evaluation"] = {
                "status": "skipped", "reason": "not_post_close_final"}
            _checkpoint(directory, manifest, "factor_ablation_evaluation",
                        result["stages"]["factor_ablation_evaluation"], result)
        elif (result["stages"]["close"].get("status") == "completed"
                and result["stages"]["monitor"].get("status") in CORE_DONE):
            _run_or_reuse(directory, manifest, result, "factor_ablation_evaluation", lambda:
                _research_stage(task_id, root, "factor_ablation_evaluation",
                                lambda: _factor_evaluation(as_of, state_root), budgets["factor_evaluation"]))
        else:
            result["stages"]["factor_ablation_evaluation"] = {
                "status": "deferred_core_incomplete"}
            _checkpoint(directory, manifest, "factor_ablation_evaluation",
                        result["stages"]["factor_ablation_evaluation"], result)
        core_complete = all(_core_done(stage, result["stages"][stage])
                            for stage in ("close", "weekly", "monitor"))
        research_statuses = [result["stages"][stage].get("status") for stage in
                             ("factor_ablation_daily", "factor_ablation_evaluation")]
        final = "completed" if (core_complete
                                and all(status in RESEARCH_DONE for status in research_statuses)) else "partial"
        result.update({"status": final, "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status=final, stage="done", result_path=str(directory / "result.json"),
                      finished_at=result["finished_at"])
        return result
    except TimeoutError as exc:
        result["stages"]["monitor"] = {"status": "skipped", "reason": "postprocess_lock_unavailable"}
        _checkpoint(directory, manifest, "monitor", result["stages"]["monitor"], result)
        result.update({"status": "timed_out", "reason": str(exc), "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status="timed_out", stage="done", reason=str(exc),
                      result_path=str(directory / "result.json"), finished_at=result["finished_at"])
        return result
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "current_close_persistence_failed", "invalid_budget_config", "unsupported_schema",
            "background_deadline_unsupported", "postprocess_lock_unsupported"} else type(exc).__name__
        result.update({"status": "failed", "reason": reason, "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status="failed", stage="done", reason=reason,
                      result_path=str(directory / "result.json"), finished_at=result["finished_at"])
        return result
    finally:
        if lock is not None:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def resume_task(task_id, root=DEFAULT_ROOT):
    status = read_status(task_id, root=root)
    if status.get("status") == "missing":
        return status
    if status.get("status") == "running" and _pid_alive(status.get("pid")):
        return status
    if status.get("status") == "completed":
        return status
    return launch_task(task_id, root=root)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run")
    parser.add_argument("--update-html")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    args = parser.parse_args(argv)
    if args.run:
        result = run_task(args.run, root=args.root)
        return 0 if result.get("status") in {"completed", "partial"} else 1
    if args.update_html:
        result = run_review_html_task(args.update_html, root=Path(args.root).parent)
        return 0 if result.get("status") == "completed" else 1
    parser.error("--run TASK_ID is required")


if __name__ == "__main__":
    raise SystemExit(main())
