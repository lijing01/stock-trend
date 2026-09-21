#!/usr/bin/env python3
"""Detached lifecycle for today's post-report research stages."""
import argparse
import copy
import json
import os
import signal
import subprocess
import sys
import time
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
TERMINAL = {"completed", "partial", "failed", "timed_out", "interrupted", "launch_failed"}
RESEARCH_DONE = {"completed", "continue_accumulating", "skipped", "scope_unverified",
                 "baseline_mismatch", "input_incomplete", "insufficient_data"}
CORE_DONE = {"completed", "insufficient_data", "healthy", "review_required",
             "fallback_required"}


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
                    budget = (max(1, int(manifest.get("budget_seconds", 300)))
                              + max(1, int(manifest.get("factor_daily_budget_seconds", 10)))
                              + max(1, int(manifest.get("factor_evaluation_budget_seconds", 20))))
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


def _lock(root, deadline):
    handle = open(Path(root) / "postprocess.lock", "a+")
    if fcntl is None:
        return handle
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
    if seconds <= 0 or not hasattr(signal, "setitimer"):
        yield
        return
    def timeout(_signum, _frame):
        raise TimeoutError("background_stage_timeout")
    old_handler = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _stage(task_id, root, name, callback, job_root, deadline):
    update_status(task_id, root=root, status="running", stage=name)
    remaining = max(0.1, deadline - time.monotonic())
    try:
        with _stage_timeout(remaining):
            run = callback()
            persistence = evolution._save(run, job_root)
        return {**run["content"], "persistence": persistence}
    except TimeoutError:
        raise
    except Exception as exc:
        return {"status": "failed", "reason": type(exc).__name__}


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
    except TimeoutError:
        return {"status": "timed_out", "reason": "background_stage_timeout"}
    except Exception as exc:
        return {"status": "failed", "reason": type(exc).__name__}


def _factor_daily(as_of, state_root):
    from analysis import factor_ablation
    return factor_ablation.run_daily(as_of, state_root=state_root)


def _factor_evaluation(as_of, state_root):
    from analysis import factor_ablation
    return factor_ablation.run_evaluation(as_of, state_root=state_root)


def _checkpoint(directory, manifest, stage, value, result):
    digest = content_sha256({"manifest": {key: item for key, item in manifest.items()
                                            if key != "requested_at"},
                             "stage": stage,
                             "close": (result.get("stages", {}).get("close")
                                       if stage == "factor_ablation_evaluation" else None)})
    path = directory / "checkpoints.json"
    try:
        current = _read(path)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        current = {"stages": {}}
    current["stages"][stage] = {"input_sha256": digest, "result": value}
    _write(path, current)
    return digest


def _cached_stage(directory, manifest, stage, result):
    try:
        saved = _read(directory / "checkpoints.json")["stages"][stage]
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    digest = content_sha256({"manifest": {key: item for key, item in manifest.items()
                                            if key != "requested_at"},
                             "stage": stage,
                             "close": (result.get("stages", {}).get("close")
                                       if stage == "factor_ablation_evaluation" else None)})
    value = saved.get("result")
    done = RESEARCH_DONE if stage.startswith("factor_ablation_") else CORE_DONE
    return value if saved.get("input_sha256") == digest and isinstance(value, dict) \
        and value.get("status") in done else None


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
    budget = max(1, int(manifest.get("budget_seconds", 300)))
    daily_budget = max(1, int(manifest.get("factor_daily_budget_seconds", 10)))
    evaluation_budget = max(1, int(manifest.get("factor_evaluation_budget_seconds", 20)))
    update_status(task_id, root=root, status="running", stage="starting", pid=os.getpid())
    result = {"task_id": task_id, "as_of": manifest.get("as_of"), "stages": {}}
    lock = None
    try:
        job_root = Path(manifest["job_root"])
        state_root = Path(manifest.get("state_root") or job_root.parent)
        as_of = manifest["as_of"]
        sessions = manifest.get("trading_sessions") or []
        _run_or_reuse(directory, manifest, result, "factor_ablation_daily", lambda:
            _research_stage(task_id, root, "factor_ablation_daily",
                            lambda: _factor_daily(as_of, state_root), daily_budget)
            if manifest.get("research_eligible", False) else
            {"status": "skipped", "reason": "not_post_close_final"})
        # Research has its own limit; the existing post-processing stages keep
        # their entire budget even if daily shadow ranking used all 10 seconds.
        deadline = time.monotonic() + budget
        update_status(task_id, root=root, status="running", stage="waiting_for_lock")
        lock = _lock(root, deadline)
        _run_or_reuse(directory, manifest, result, "close", lambda:
            _stage(task_id, root, "close", lambda: evolution.run_close(as_of), job_root, deadline))
        if time.monotonic() >= deadline:
            raise TimeoutError("background_budget_exhausted")
        if result["stages"]["close"].get("status") == "completed":
            # The weekly job still owns its own same-week deduplication in the
            # foreground compatibility path; this task has a frozen input.
            _run_or_reuse(directory, manifest, result, "weekly", lambda:
                _stage(task_id, root, "weekly", lambda: evolution.run_weekly(as_of), job_root, deadline))
        else:
            result["stages"]["weekly"] = {"status": "skipped", "reason": "close_not_complete"}
            _checkpoint(directory, manifest, "weekly", result["stages"]["weekly"], result)
        if time.monotonic() >= deadline:
            raise TimeoutError("background_budget_exhausted")
        _run_or_reuse(directory, manifest, result, "monitor", lambda:
            _stage(task_id, root, "monitor", lambda:
                evolution.monitoring_snapshot(job_root=job_root, expected_trading_days=sessions,
                                              as_of=as_of), job_root, deadline))
        if (not manifest.get("research_eligible", False)):
            result["stages"]["factor_ablation_evaluation"] = {
                "status": "skipped", "reason": "not_post_close_final"}
            _checkpoint(directory, manifest, "factor_ablation_evaluation",
                        result["stages"]["factor_ablation_evaluation"], result)
        elif (result["stages"]["close"].get("status") == "completed"
                and result["stages"]["monitor"].get("status") in CORE_DONE):
            _run_or_reuse(directory, manifest, result, "factor_ablation_evaluation", lambda:
                _research_stage(task_id, root, "factor_ablation_evaluation",
                                lambda: _factor_evaluation(as_of, state_root), evaluation_budget))
        else:
            result["stages"]["factor_ablation_evaluation"] = {
                "status": "deferred_core_incomplete"}
            _checkpoint(directory, manifest, "factor_ablation_evaluation",
                        result["stages"]["factor_ablation_evaluation"], result)
        core_statuses = [result["stages"][stage].get("status") for stage in ("close", "weekly", "monitor")]
        research_statuses = [result["stages"][stage].get("status") for stage in
                             ("factor_ablation_daily", "factor_ablation_evaluation")]
        final = "completed" if (all(status in CORE_DONE for status in core_statuses)
                                and all(status in RESEARCH_DONE for status in research_statuses)) else "partial"
        result.update({"status": final, "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status=final, stage="done", result_path=str(directory / "result.json"),
                      finished_at=result["finished_at"])
        return result
    except TimeoutError as exc:
        result.update({"status": "timed_out", "reason": str(exc), "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status="timed_out", stage="done", reason=str(exc),
                      result_path=str(directory / "result.json"), finished_at=result["finished_at"])
        return result
    except Exception as exc:
        result.update({"status": "failed", "reason": type(exc).__name__, "finished_at": _now()})
        _write(directory / "result.json", result)
        update_status(task_id, root=root, status="failed", stage="done", reason=type(exc).__name__,
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
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    args = parser.parse_args(argv)
    if args.run:
        result = run_task(args.run, root=args.root)
        return 0 if result.get("status") in {"completed", "partial"} else 1
    parser.error("--run TASK_ID is required")


if __name__ == "__main__":
    raise SystemExit(main())
