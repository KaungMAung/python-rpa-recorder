"""Timestamped execution evidence and bounded retention."""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunEvidenceSession:
    """Own one run folder, its log, and the final machine-readable summary."""

    def __init__(
        self,
        project_dir: Path,
        flow_name: str,
        source: str,
        retention_runs: int = 100,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.flow_name = flow_name
        self.source = source
        self.run_id = uuid4().hex
        self.started_at = utc_now_iso()
        self._started = datetime.fromisoformat(self.started_at)
        self.retention_runs = min(1000, max(1, int(retention_runs)))
        stamp = self._started.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        source_slug = re.sub(r"[^a-z0-9]+", "_", source.casefold()).strip("_") or "run"
        self.runs_root = self.project_dir / "runs"
        self.folder = self.runs_root / f"{stamp}_{source_slug}_{self.run_id[:8]}"
        self.folder.mkdir(parents=True, exist_ok=False)
        self.log_path = self.folder / "execution.log"
        self.summary_path = self.folder / "summary.json"
        self.validation_results: list[dict[str, Any]] = []
        self.runtime_inputs: dict[str, Any] = {}
        self.logger = logging.getLogger(f"python-rpa-recorder.evidence.{self.run_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.handlers.clear()
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(handler)
        self.logger.info("run evidence started: flow=%s source=%s run_id=%s", flow_name, source, self.run_id)

    @property
    def relative_folder(self) -> str:
        try:
            return self.folder.relative_to(self.project_dir).as_posix()
        except ValueError:
            return str(self.folder)

    def set_validation(self, issues: list[Any]) -> None:
        self.validation_results = [
            {
                "level": str(issue.level),
                "step_number": int(issue.step_number),
                "step_name": str(issue.step_name),
                "reason": str(issue.reason),
            }
            for issue in issues
        ]

    def set_runtime_inputs(self, values: dict[str, Any], sensitive_names: set[str]) -> None:
        self.runtime_inputs = {
            name: "[REDACTED]" if name in sensitive_names else value for name, value in values.items()
        }

    def finalize(
        self,
        status: str,
        step_results: list[dict[str, Any]] | None = None,
        attempts: int = 0,
        failed_step: int | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        ended = datetime.now(timezone.utc)
        normalized_steps = []
        for raw in step_results or []:
            step = dict(raw)
            if step.get("status") == "Running":
                step["status"] = "Stopped" if status == "Stopped" else "Failed"
                step.setdefault("error", error)
                step["ended_at"] = step.get("ended_at") or ended.isoformat()
                try:
                    started = datetime.fromisoformat(str(step.get("started_at")))
                    step["duration_seconds"] = max(0.0, (ended - started).total_seconds())
                except (TypeError, ValueError):
                    step.setdefault("duration_seconds", None)
            normalized_steps.append(step)
        screenshots = sorted(
            path.relative_to(self.folder).as_posix()
            for path in self.folder.rglob("*.png")
            if path.is_file()
        )
        summary = {
            "schema_version": 1,
            "run_id": self.run_id,
            "flow_name": self.flow_name,
            "source": self.source,
            "started_at": self.started_at,
            "ended_at": ended.isoformat(),
            "duration_seconds": max(0.0, (ended - self._started).total_seconds()),
            "status": status,
            "attempts": int(attempts),
            "failed_step": failed_step,
            "error": error,
            "validation_results": self.validation_results,
            "runtime_inputs": self.runtime_inputs,
            "step_results": normalized_steps,
            "screenshots": screenshots,
            "log": self.log_path.name,
        }
        self.logger.info(
            "run evidence completed: status=%s attempts=%s failed_step=%s error=%s",
            status, attempts, failed_step, error or "",
        )
        self.summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        self.cleanup_retention()
        self.close()
        return summary

    def close(self) -> None:
        for handler in list(self.logger.handlers):
            handler.flush()
            handler.close()
            self.logger.removeHandler(handler)

    def cleanup_retention(self) -> None:
        if not self.runs_root.exists():
            return
        folders = [path for path in self.runs_root.iterdir() if path.is_dir()]
        # Always retain the session being finalized even when two runs were
        # created inside the same millisecond and UUID ordering differs.
        folders = [self.folder] + sorted(
            (path for path in folders if path != self.folder),
            key=lambda path: path.name,
            reverse=True,
        )
        for old in folders[self.retention_runs:]:
            try:
                if old.resolve().parent == self.runs_root.resolve():
                    shutil.rmtree(old)
            except OSError:
                self.logger.warning("could not remove expired evidence folder: %s", old)


def load_run_summary(folder: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load a report without raising when evidence was deleted or corrupted."""
    folder = Path(folder)
    if not folder.exists():
        return None, "The run evidence folder has been deleted or moved."
    path = folder / "summary.json"
    if not path.exists():
        return None, "The run summary is missing from this evidence folder."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"The run summary could not be read: {exc}"
    if not isinstance(data, dict):
        return None, "The run summary has an unsupported format."
    return data, None
