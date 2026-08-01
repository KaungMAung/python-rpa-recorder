"""Best-effort external delivery of the canonical local run-history record."""
from __future__ import annotations

import getpass
import platform
import re
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .models import ProjectSettings
from .scheduler import RunHistoryEntry
from .webhook import post_json_webhook


def build_run_log_payload(
    flow_name: str,
    entry: RunHistoryEntry,
    step_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Serialize one existing history entry using stable webhook field names."""
    steps = [item for item in (step_results or []) if isinstance(item, dict)]
    statuses = [str(item.get("status") or "").strip().lower() for item in steps]
    return {
        "run_id": entry.run_id or "",
        "flow_name": flow_name,
        "status": entry.status,
        "started_at": entry.started_at,
        "finished_at": entry.finished_at or "",
        "duration_seconds": entry.duration_seconds,
        "machine_name": platform.node() or "",
        "user_name": _user_name(),
        "source": entry.source or "",
        "error": entry.error or "",
        "failed_step": entry.failed_step,
        "attempts": entry.attempts,
        "retry_count": entry.retry_count,
        "fallback_executed": entry.fallback_executed,
        "step_count": len(steps),
        "completed_step_count": sum(status in {"completed", "success", "passed"} for status in statuses),
        "failed_step_count": sum("fail" in status for status in statuses),
        "skipped_step_count": sum("skip" in status for status in statuses),
    }


def send_external_run_log(
    settings: ProjectSettings,
    flow_name: str,
    entry: RunHistoryEntry,
    step_results: list[dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Post a run-history entry without allowing delivery to alter the run result."""
    url = str(settings.run_log_webhook_url or "").strip()
    if not settings.send_run_log_to_sharepoint or not url:
        return False
    try:
        payload = build_run_log_payload(flow_name, entry, step_results)
        post_json_webhook(url, payload, settings.run_log_timeout_seconds)
    except Exception as exc:
        _write_log(log, f"External run log warning: {_redact_urls(str(exc), url)}")
        return False
    _write_log(log, f"External run log sent ({entry.status}).")
    return True


def redact_webhook_url(url: str) -> str:
    try:
        parts = urlsplit(str(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "[redacted]" if parts.query else "", ""))
    except ValueError:
        return "[redacted webhook URL]"


def _redact_urls(message: str, configured_url: str) -> str:
    safe = str(message).replace(configured_url, redact_webhook_url(configured_url))
    return re.sub(
        r"https?://[^\s'\"<>]+",
        lambda match: redact_webhook_url(match.group(0)),
        safe,
        flags=re.IGNORECASE,
    )


def _user_name() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


def _write_log(log: Callable[[str], None] | None, message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass
