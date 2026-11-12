"""A local, owner-only record of AI responses that failed validation, for human review.

A rejected response is discarded from every artifact and never shown. With the audit
enabled, it is first written here so a developer can see *why* it failed. The audit is:

- **Local and gitignored.** The default directory is ``data/ai_rejections/``.
- **Owner-only where supported.** The directory is 0700 and each file 0600, created
  exclusively, so an existing file is never overwritten or followed.
- **Minimal.** It holds the response, the violations and provenance IDs. It never holds
  the API key, request headers or the prompt. Anything shaped like an Anthropic key is
  redacted anyway.
- **Write-only for the application.** Nothing in GrowthPulse reads it back. The dashboard
  builder reads only ``docs/samples/ai_insights/``, and every artifact there is
  re-validated, so an audited response can never bypass validation.
- **Pruned.** Files older than the retention period are deleted on each write.

It's enabled per ``AI_REJECTION_AUDIT``: ``auto`` (the default) turns it on only for
``APP_ENV=local`` outside CI, so CI logs and hosted runs never hold rejected output.
"""

import contextlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings

AUDIT_SCHEMA_VERSION = 1
MAX_TEXT_BYTES = 200_000
KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
SAFE_ID = re.compile(r"[^A-Za-z0-9_\-]")


def audit_enabled(settings: Settings, environ: dict[str, str] | None = None) -> bool:
    environ = os.environ if environ is None else environ
    if settings.ai_rejection_audit == "enabled":
        return True
    if settings.ai_rejection_audit == "disabled":
        return False
    return settings.app_env == "local" and not environ.get("CI")


def _redact(value):
    if isinstance(value, str):
        return KEY_PATTERN.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class AuditRecord:
    saved: bool
    audit_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {"saved": self.saved, "audit_id": self.audit_id, "reason": self.reason}


class RejectionAudit:
    def __init__(self, directory: Path, retention_days: int):
        self.directory = Path(directory)
        self.retention = timedelta(days=retention_days)

    @classmethod
    def from_settings(cls, settings: Settings) -> "RejectionAudit | None":
        if not audit_enabled(settings):
            return None
        return cls(settings.ai_rejection_audit_dir, settings.ai_rejection_audit_retention_days)

    def _prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # A filesystem without POSIX modes keeps its own permissions.
        with contextlib.suppress(OSError):
            os.chmod(self.directory, stat.S_IRWXU)

    def prune(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        removed = 0
        if not self.directory.exists():
            return 0
        for path in self.directory.glob("*.json"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if now - modified > self.retention:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def save(
        self,
        *,
        report_id: str,
        package_id: str,
        prompt_version: str,
        provider: dict,
        stop_reason: str | None,
        response_text: str,
        violations: list[dict],
        now: datetime | None = None,
    ) -> AuditRecord:
        """Write one rejected response. Never raises: failure to audit never blocks the
        fallback, and the reason is recorded instead."""
        now = now or datetime.now(UTC)
        message_id = SAFE_ID.sub("", str(provider.get("message_id") or ""))[:40]
        audit_id = f"{now:%Y%m%dT%H%M%SZ}-{message_id or uuid.uuid4().hex[:12]}"
        try:
            parsed = json.loads(response_text)
            body = {"response": parsed, "response_text": None}
        except json.JSONDecodeError:
            truncated = response_text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
            body = {"response": None, "response_text": truncated}
        record = _redact(
            {
                "audit_schema_version": AUDIT_SCHEMA_VERSION,
                "notice": "Rejected AI output. Failed validation. Never display or publish.",
                "audit_id": audit_id,
                "created_at": now.isoformat(timespec="seconds"),
                "report_id": report_id,
                "package_id": package_id,
                "prompt_version": prompt_version,
                "provider": {
                    key: provider.get(key)
                    for key in ("name", "mode", "requested_model", "response_model", "message_id")
                },
                "stop_reason": stop_reason,
                "violations": violations,
                **body,
            }
        )
        try:
            self._prepare()
            self.prune(now)
            path = self.directory / f"{audit_id}.json"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=1, sort_keys=True)
                handle.write("\n")
        except OSError as error:
            return AuditRecord(saved=False, reason=type(error).__name__)
        return AuditRecord(saved=True, audit_id=audit_id)
