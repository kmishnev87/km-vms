from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.sanitization import redact_text
from app.db.session import engine
from app.services.migration_maintenance import (
    MigrationMaintenanceBlocked,
    apply_migration_maintenance,
    dry_run_migration_maintenance,
)


def _bounded_text(value: Any, limit: int = 180) -> str:
    return redact_text(str(value or "")).replace("\n", " ").strip()[:limit]


def _safe_summary(result: dict[str, Any]) -> dict[str, Any]:
    report = result.get("report") if isinstance(result.get("report"), dict) else {}
    backup = report.get("backup") if isinstance(report.get("backup"), dict) else {}
    return {
        "status": _bounded_text(result.get("status") or "unknown", 40),
        "reason": _bounded_text(result.get("reason"), 180),
        "current_version": result.get("current_version"),
        "target_version": result.get("target_version"),
        "pending_count": int(result.get("pending_count") or 0),
        "applied": bool(result.get("applied")),
        "idempotent": bool(result.get("idempotent")),
        "migration_executed": bool(result.get("migration_executed")),
        "backup": {
            "status": _bounded_text(
                result.get("backup_status") or backup.get("status") or "not_created",
                60,
            ),
            "root_status": _bounded_text(
                result.get("backup_root_status") or backup.get("backup_root_status"),
                60,
            ),
            "persistent": result.get("backup_root_persistent", backup.get("backup_root_persistent")),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="km-vms-migration-maintenance")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("dry-run")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--confirm", action="store_true", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with Session(engine) as db:
            if args.mode == "dry-run":
                result = dry_run_migration_maintenance(db)
            else:
                result = apply_migration_maintenance(db, confirm=bool(args.confirm))
        summary = _safe_summary(result)
        print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
        return 0 if summary["status"] in {"pending", "current", "applied"} else 2
    except MigrationMaintenanceBlocked as exc:
        diagnostics = exc.diagnostics if isinstance(exc.diagnostics, dict) else {}
        result = {
            "status": diagnostics.get("status") or "blocked",
            "reason": diagnostics.get("reason") or exc.status,
            "migration_executed": bool(diagnostics.get("migration_executed")),
            "report": diagnostics.get("report") if isinstance(diagnostics.get("report"), dict) else {},
        }
        print(json.dumps(_safe_summary(result), sort_keys=True, ensure_ascii=True))
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": _bounded_text(exc.__class__.__name__, 80),
                    "migration_executed": False,
                },
                sort_keys=True,
                ensure_ascii=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
