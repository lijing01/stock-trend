#!/usr/bin/env python3
"""今日推荐: collect, scan, evaluate, research and monitor on each invocation."""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPTS_DIR.parents[3]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from analysis import evolution_job as evolution
from core.cache_utils import CACHE_DIR
from core.evolution_registry import _atomic_write, load_active_policy
from core.recommendation_snapshot import content_sha256
from fetchers.sector_data import _load_authoritative_trading_dates

SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_STATE_ROOT = Path(CACHE_DIR) / "evolution"
DEFAULT_BACKGROUND_ROOT = DEFAULT_STATE_ROOT / "background"


def _run_script(script, arguments):
    """Keep progress on stderr and require a successful structured result."""
    print(f"今日推荐：{script}", file=sys.stderr)
    process = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *arguments, "--json"],
        cwd=PROJECT_ROOT, env={**os.environ, "TZ": "Asia/Shanghai"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=1200)
    if process.stderr:
        print(process.stderr.strip(), file=sys.stderr)
    # market_regime's legacy CLI surrounds its JSON with progress and timing.
    decoder = json.JSONDecoder()
    result = None
    offset = 0
    for line in process.stdout.splitlines(keepends=True):
        if line.startswith("{"):
            try:
                result, end = decoder.raw_decode(process.stdout, offset)
                progress = process.stdout[:offset] + process.stdout[end:]
                if progress.strip():
                    print(progress.strip(), file=sys.stderr)
                break
            except ValueError:
                pass
        offset += len(line)
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("upstream_output_invalid")
    paths = {}
    for line in (process.stderr or "").splitlines():
        match = re.search(r"(?:HTML|HTML report):\s*(\S+)", line)
        if match and Path(match.group(1)).is_file():
            paths["html"] = match.group(1)
        match = re.search(r"(?:MD|候选报告):\s*(\S+)", line)
        if match and Path(match.group(1)).is_file():
            paths["markdown"] = match.group(1)
    if paths:
        result["report_paths"] = paths
    if script == "analysis/market_regime.py":
        # The legacy writer suppresses disk errors; successful stdout alone
        # does not prove candidates will read this refresh rather than old data.
        saved = json.loads((Path(CACHE_DIR) / "market_regime.json").read_text(encoding="utf-8"))
        meta = result.get("meta") or {}
        if (not meta.get("generated_at") or saved.get("generated_at") != meta["generated_at"]
                or saved.get("data_date") != meta.get("data_date")
                or saved.get("regime") != result.get("regime")):
            raise ValueError("market_context_not_persisted")
    return result


def _completed_sessions(now):
    dates = sorted(date.fromisoformat(value).isoformat()
                   for value in _load_authoritative_trading_dates(now))
    today = now.date().isoformat()
    # The provider can return history only. Evaluate that known interval, but
    # expose its horizon rather than interpreting absent dates as holidays.
    return ([day for day in dates if day < today or
             (day == today and now.time() >= time(15, 10))], dates[-1] if dates else None)


def _should_use_post_close_final(now):
    """Use the immutable scan scope once the current session is closed."""
    return now.weekday() >= 5 or now.time() >= time(15, 10)


def _weekly_completed(job_root, as_of):
    week = date.fromisoformat(as_of).isocalendar()[:2]
    for path in (job_root / "weekly").glob("*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            content = run["content"]
            if not isinstance(content, dict) or not isinstance(content.get("input"), dict):
                continue
            prior = date.fromisoformat(content["as_of"])
            if (content.get("kind") == "weekly" and content.get("status") == "completed"
                    and prior.isoformat() <= as_of and prior.isocalendar()[:2] == week
                    and (content.get("input") or {}).get("research_snapshots", 0) > 0
                    and run.get("content_sha256") == content_sha256(content)):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False


def _job_stage(kind, callback, job_root):
    """Keep a failed auxiliary stage visible without suppressing candidates."""
    print(f"今日推荐：{kind}", file=sys.stderr)
    try:
        run = callback()
        persistence = evolution._save(run, job_root)
        return {**run["content"], "persistence": persistence}
    except Exception as exc:
        return {"status": "failed", "reason": type(exc).__name__}


def _report_digest(output):
    """Digest only the frozen recommendation evidence, not runtime metadata."""
    return content_sha256({key: output.get(key) for key in (
        "recommendations", "waiting_trigger", "observation", "meta", "policy")})


def _launch_background(output, *, now, as_of, sessions, state_root, postprocess_root):
    """Create/reuse a detached post-process task after the report is ready."""
    from bridge import today_background

    manifest = {
        "schema_version": "today-recommendation-background/v1",
        "requested_at": now.isoformat(),
        "as_of": as_of,
        "trading_sessions": list(sessions),
        "report_sha256": _report_digest(output),
        "state_root": str(Path(state_root).resolve()),
        "job_root": str((Path(state_root) / "jobs").resolve()),
        "budget_seconds": int(os.environ.get("STOCK_TREND_BACKGROUND_BUDGET", "300")),
    }
    task = today_background.ensure_task(manifest, root=postprocess_root)
    if task["status"] in {"completed", "partial", "failed", "timed_out", "interrupted"}:
        return task
    if task.get("status") == "running":
        return task
    try:
        launched = today_background.launch_task(task["task_id"], root=postprocess_root)
        return {**task, **launched}
    except (OSError, ValueError) as exc:
        today_background.update_status(task["task_id"], root=postprocess_root,
                                       status="launch_failed", reason=type(exc).__name__)
        return {**task, "status": "launch_failed", "reason": type(exc).__name__}


def _policy_notice(previous, current, source):
    if previous == current or (previous is None and current.get("status") == "baseline"):
        return None
    before = (previous or {}).get("priority_bonuses", {})
    after = current.get("priority_bonuses", {})
    changes = {key: {"before": before.get(key), "after": after.get(key)}
               for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}
    fallback = current.get("status") == "invalid_pointer_fallback"
    message = ("策略指针异常，已使用基线策略" if fallback else
               f"策略版本：{(previous or {}).get('experiment_id', '首次检测')} → {current.get('experiment_id')}")
    return {"kind": "policy_fallback" if fallback else "policy_changed", "source": source,
            "message": message, "previous": previous, "current": current,
            "parameter_changes": changes}


def run_today(candidate_args=None, *, now=None, state_root=DEFAULT_STATE_ROOT,
              dry_run=False, postprocess="sync", background_root=None):
    candidate_args = list(candidate_args or [])
    if postprocess not in {"sync", "background"}:
        raise ValueError("invalid_postprocess_mode")
    workflow = {"status": "dry_run" if dry_run else "running",
                "candidate_args": candidate_args,
                "steps": ["market", "candidates", "close", "weekly", "monitor"]}
    output = {"workflow": workflow, "notifications": []}
    if dry_run:
        return output
    now = now or datetime.now(SHANGHAI)
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    if (_should_use_post_close_final(now)
            and "--post-close-final" not in candidate_args):
        candidate_args.append("--post-close-final")
        workflow["candidate_args"] = candidate_args
    state_root = Path(state_root)
    job_root = state_root / "jobs"
    state_path = state_root / "today_state.json"
    previous = None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        previous = state["active_policy"]
        if not isinstance(previous, dict):
            raise ValueError("invalid_notification_state")
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError):
        output["notifications"].append({"kind": "notification_state_unavailable",
                                         "message": "上次策略记录不可用，本次重新检测当前版本。"})
    before = load_active_policy()
    notice = _policy_notice(previous, before, "since_previous_run")
    if notice:
        output["notifications"].append(notice)

    try:
        _run_script("analysis/market_regime.py", ["--no-html"])
        workflow["market"] = {"status": "completed"}
    except Exception as exc:
        workflow["market"] = {"status": "failed", "reason": type(exc).__name__}
    if workflow["market"]["status"] == "completed":
        try:
            candidates = _run_script("scans/daily_candidates.py", candidate_args)
            output.update(candidates)
            workflow["candidates"] = {"status": "completed"}
        except Exception as exc:
            workflow["candidates"] = {"status": "failed", "reason": type(exc).__name__}
    else:
        workflow["candidates"] = {"status": "skipped", "reason": "market_refresh_failed"}

    try:
        sessions, coverage_end = _completed_sessions(now)
        workflow["calendar"] = {
            "status": ("unavailable" if not sessions else
                       "historical_only" if coverage_end < now.date().isoformat() else "ready"),
            "coverage_end": coverage_end,
            "requested_date": now.date().isoformat(),
        }
    except Exception as exc:
        sessions = []
        workflow["calendar"] = {"status": "unavailable", "reason": type(exc).__name__}
    as_of = sessions[-1] if sessions else None
    workflow["as_of"] = as_of
    if as_of:
        workflow["report"] = {"status": "ready" if "recommendations" in output else "degraded"}
        if postprocess == "background" and "recommendations" in output:
            task = _launch_background(
                output, now=now, as_of=as_of, sessions=sessions,
                state_root=state_root,
                postprocess_root=Path(background_root or DEFAULT_BACKGROUND_ROOT),
            )
            workflow["postprocess"] = task
            for stage in ("close", "weekly", "monitor"):
                workflow[stage] = {"status": "background", "task_id": task.get("task_id")}
        else:
            workflow["close"] = _job_stage("close", lambda: evolution.run_close(as_of), job_root)
            if _weekly_completed(job_root, as_of):
                workflow["weekly"] = {"status": "skipped", "reason": "already_completed_this_week"}
            else:
                workflow["weekly"] = _job_stage("weekly", lambda: evolution.run_weekly(as_of), job_root)
            workflow["monitor"] = _job_stage("monitor", lambda: evolution.monitoring_snapshot(
                job_root=job_root, expected_trading_days=sessions, as_of=as_of), job_root)
    else:
        workflow["report"] = {"status": "degraded" if "recommendations" not in output else "ready"}
        workflow["postprocess"] = {"status": "skipped", "reason": "trading_calendar_unavailable"}
        for stage in ("close", "weekly", "monitor"):
            workflow[stage] = {"status": "skipped", "reason": "trading_calendar_unavailable"}

    after = load_active_policy()
    workflow["active_policy"] = after
    notice = _policy_notice(before, after, "during_run")
    if notice:
        notice["message"] += "；本次推荐仍按扫描时版本生成，新版本用于后续扫描。"
        notice["monitor_fallback"] = workflow.get("monitor", {}).get("fallback")
        output["notifications"].append(notice)
    if workflow.get("monitor", {}).get("status") == "fallback_required":
        output["notifications"].append({
            "kind": "policy_safety_alert",
            "message": "策略监控发现接口或契约异常，已执行安全检查；请复核异常，即使当前已是基线策略。",
            "fallback": workflow["monitor"].get("fallback"),
            "current": after,
        })
    try:
        _atomic_write(state_path, {"active_policy": after, "observed_at": now.isoformat()})
        workflow["notification_tracking"] = {"status": "saved"}
    except OSError as exc:
        workflow["notification_tracking"] = {"status": "failed", "reason": type(exc).__name__}
    degraded = any(workflow[stage]["status"] in ("failed", "upstream_gap", "fallback_required")
                   for stage in workflow["steps"])
    post_status = workflow.get("postprocess", {}).get("status")
    if postprocess == "background" and post_status in {"queued", "running", "background"}:
        workflow["status"] = "report_ready"
    elif postprocess == "background" and post_status in {
            "launch_failed", "failed", "timed_out", "interrupted"}:
        workflow["status"] = "partial"
    else:
        workflow["status"] = "partial" if (
            degraded or workflow["calendar"]["status"] != "ready"
            or workflow["notification_tracking"]["status"] == "failed"
        ) else "completed"
    return output


def render_summary(result):
    workflow = result["workflow"]
    labels = {"completed": "已完成", "partial": "部分完成", "report_ready": "报告已就绪，后台处理中",
              "dry_run": "仅预览",
              "failed": "失败", "skipped": "已跳过", "upstream_gap": "正式数据缺口",
              "insufficient_data": "样本不足", "healthy": "正常",
              "review_required": "需要复核", "fallback_required": "已触发安全恢复"}
    lines = []
    for notice in result["notifications"]:
        lines.append(notice["message"])
        for key, change in notice.get("parameter_changes", {}).items():
            lines.append(f"  {key}：{change['before']} → {change['after']}")
    lines.append(f"今日推荐：{labels.get(workflow['status'], workflow['status'])}；评价日期：{workflow.get('as_of') or '待确定'}")
    if workflow.get("postprocess", {}).get("task_id"):
        lines.append(f"后台任务：{workflow['postprocess']['task_id']}（可用 --status 查询）")
    if workflow.get("calendar", {}).get("status") == "historical_only":
        lines.append(f"交易日历仅覆盖至 {workflow['calendar']['coverage_end']}，后处理仅评价已知历史区间。")
    for stage, label in (("market", "市场刷新"), ("candidates", "候选扫描"),
                         ("close", "历史评价"), ("weekly", "每周研究"), ("monitor", "策略监控")):
        detail = workflow.get(stage, {})
        status = detail.get("status", "待执行")
        reason = detail.get("reason", "")
        reason = {"already_completed_this_week": "本周已完成", "trading_calendar_unavailable": "交易日历不可用",
                  "market_refresh_failed": "市场刷新失败"}.get(reason, reason)
        lines.append(f"{label}：{labels.get(status, status)} {reason}".rstrip())
    lines.append(f"可执行 {len(result.get('recommendations', []))}；等待触发 {len(result.get('waiting_trigger', []))}；观察 {len(result.get('observation', []))}")
    lines.append("本报告仅供学习参考，不构成任何投资建议。")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="今日推荐：自动扫描、评价、每周研究、监控与策略变更通知",
                                     epilog="支持 candidates 参数，例如 --top 30 --min-candidates 20 --no-html。")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只显示流程，不联网、不写入")
    parser.add_argument("--postprocess", choices=("background", "sync"), default="background")
    parser.add_argument("--status", metavar="TASK_ID", help="查询后台任务状态")
    parser.add_argument("--resume", metavar="TASK_ID", help="恢复一个未完成的后台任务")
    args, candidate_args = parser.parse_known_args(argv)
    if args.status or args.resume:
        from bridge import today_background
        if args.resume:
            result = today_background.resume_task(args.resume, root=DEFAULT_BACKGROUND_ROOT)
        else:
            result = today_background.read_status(args.status, root=DEFAULT_BACKGROUND_ROOT)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") not in {"missing", "failed", "timed_out", "interrupted"} else 1
    result = run_today(candidate_args, dry_run=args.dry_run, postprocess=args.postprocess)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_summary(result))
    return 1 if result["workflow"].get("candidates", {}).get("status") in ("failed", "skipped") else 0


if __name__ == "__main__":
    sys.exit(main())
