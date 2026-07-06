from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


THREAD_KEY_PREFIXES = ("local:", "remote:", "pending-worktree:")
MODEL_RESPONSE_ITEM_TYPES = {"reasoning", "function_call", "custom_tool_call"}


@dataclass
class RepairReport:
    codex_home: Path
    provider: str
    apply: bool
    checks: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    changed_files: list[Path] = field(default_factory=list)
    backup_dir: Path | None = None

    def add_check(self, text: str) -> None:
        self.checks.append(text)

    def add_action(self, text: str) -> None:
        self.actions.append(text)

    def add_warning(self, text: str) -> None:
        self.warnings.append(text)

    def mark_changed(self, path: Path) -> None:
        if path not in self.changed_files:
            self.changed_files.append(path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "codex_home": str(self.codex_home),
            "provider": self.provider,
            "apply": self.apply,
            "backup_dir": str(self.backup_dir) if self.backup_dir else None,
            "checks": self.checks,
            "actions": self.actions,
            "warnings": self.warnings,
            "changed_files": [str(path) for path in self.changed_files],
        }


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def normalize_win_path(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def current_model_provider(codex_home: Path) -> str:
    config = codex_home / "config.toml"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "openai"

    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            provider = data.get("model_provider")
            if isinstance(provider, str) and provider.strip():
                return provider.strip()
        except Exception:
            pass

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("model_provider"):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            return value
    return "openai"


def sqlite_paths(codex_home: Path) -> list[Path]:
    candidates = [
        codex_home / "state_5.sqlite",
        codex_home / "sqlite" / "state_5.sqlite",
    ]
    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if path.exists() and resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"pragma table_info({table})")}


def has_threads_table(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "select 1 from sqlite_master where type='table' and name='threads'"
    ).fetchone()
    return row is not None


def is_subagent(source: str | None, thread_source: str | None) -> bool:
    return thread_source == "subagent" or bool(source and source.startswith('{"subagent"'))


def local_thread_key(thread_id: str) -> str:
    if thread_id.startswith(THREAD_KEY_PREFIXES):
        return thread_id
    return f"local:{thread_id}"


def ensure_backup(report: RepairReport) -> Path:
    if report.backup_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        report.backup_dir = (
            report.codex_home / "backups_state" / "sidebar-repair" / stamp
        )
        if report.apply:
            report.backup_dir.mkdir(parents=True, exist_ok=True)
    return report.backup_dir


def backup_file(path: Path, report: RepairReport) -> None:
    if not report.apply or not path.exists():
        return
    backup_dir = ensure_backup(report)
    candidates = [path]
    if path.suffix in {".sqlite", ".db"}:
        candidates.extend(
            candidate
            for candidate in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
            if candidate.exists()
        )
    for candidate in candidates:
        destination = backup_dir / candidate.name
        suffix = 1
        while destination.exists():
            destination = backup_dir / f"{candidate.name}.{suffix}.bak"
            suffix += 1
        shutil.copy2(candidate, destination)


def is_model_response_item(payload: dict[str, Any]) -> bool:
    item_type = payload.get("type")
    if item_type in MODEL_RESPONSE_ITEM_TYPES:
        return True
    if item_type == "message" and payload.get("role") == "assistant":
        return True
    if payload.get("call_type") in {"function_call", "custom_tool_call"}:
        return True
    return False


def synthetic_response_item_id(
    thread_id: str | None, turn_id: str | None, line_no: int, payload: dict[str, Any]
) -> str:
    seed = json.dumps(
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "line_no": line_no,
            "type": payload.get("type"),
            "role": payload.get("role"),
            "name": payload.get("name"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"repair-{digest}"


def user_thread_filter_sql() -> str:
    return (
        "coalesce(thread_source, '') != 'subagent' "
        "and coalesce(source, '') not like '{\"subagent\"%'"
    )


def doctor_sqlite(path: Path, provider: str, report: RepairReport) -> None:
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        report.add_warning(f"{path}: cannot open sqlite: {exc}")
        return
    try:
        if not has_threads_table(con):
            report.add_warning(f"{path}: no threads table")
            return
        total = con.execute("select count(*) from threads").fetchone()[0]
        user_total = con.execute(
            f"select count(*) from threads where {user_thread_filter_sql()}"
        ).fetchone()[0]
        mismatched = con.execute(
            f"select count(*) from threads where {user_thread_filter_sql()} "
            "and model_provider != ?",
            (provider,),
        ).fetchone()[0]
        prefixed = 0
        for cwd, rollout_path in con.execute("select cwd, rollout_path from threads"):
            if cwd != normalize_win_path(cwd) or rollout_path != normalize_win_path(
                rollout_path
            ):
                prefixed += 1
        report.add_check(
            f"{path}: threads={total}, user_threads={user_total}, "
            f"provider_mismatch={mismatched}, path_prefix_rows={prefixed}"
        )
    finally:
        con.close()


def repair_sqlite(
    path: Path,
    provider: str,
    report: RepairReport,
    repair_user_flags: bool = False,
) -> list[dict[str, Any]]:
    rows_for_global_state: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("pragma busy_timeout=5000")
    except sqlite3.Error as exc:
        report.add_warning(f"{path}: cannot open sqlite: {exc}")
        return rows_for_global_state

    try:
        if not has_threads_table(con):
            report.add_warning(f"{path}: no threads table")
            return rows_for_global_state
        columns = table_columns(con, "threads")
        needed = {
            "id",
            "source",
            "thread_source",
            "archived",
            "model_provider",
            "cwd",
            "rollout_path",
        }
        missing = needed - columns
        if missing:
            report.add_warning(f"{path}: missing columns: {', '.join(sorted(missing))}")
            return rows_for_global_state

        select_cols = [
            "id",
            "source",
            "thread_source",
            "archived",
            "model_provider",
            "cwd",
            "rollout_path",
        ]
        for optional in ("has_user_event", "updated_at", "recency_at"):
            if optional in columns:
                select_cols.append(optional)

        rows = list(con.execute(f"select {', '.join(select_cols)} from threads"))
        sqlite_updates = 0
        sqlite_backed_up = False
        for row in rows:
            source = row["source"]
            thread_source = row["thread_source"]
            archived = int(row["archived"] or 0) if "archived" in row.keys() else 0
            subagent = is_subagent(source, thread_source)
            cwd = normalize_win_path(row["cwd"])
            rollout_path = normalize_win_path(row["rollout_path"])

            if not archived and not subagent:
                rows_for_global_state.append(
                    {
                        "id": row["id"],
                        "cwd": cwd,
                        "updated_at": row["updated_at"] if "updated_at" in row.keys() else 0,
                        "recency_at": row["recency_at"] if "recency_at" in row.keys() else 0,
                    }
                )

            updates: dict[str, Any] = {}
            if cwd != row["cwd"]:
                updates["cwd"] = cwd
            if rollout_path != row["rollout_path"]:
                updates["rollout_path"] = rollout_path
            if not subagent:
                if row["model_provider"] != provider:
                    updates["model_provider"] = provider
                if repair_user_flags and thread_source != "user":
                    updates["thread_source"] = "user"
                if (
                    repair_user_flags
                    and "has_user_event" in row.keys()
                    and row["has_user_event"] != 1
                ):
                    updates["has_user_event"] = 1

            if not updates:
                continue
            sqlite_updates += 1
            if report.apply:
                if not sqlite_backed_up:
                    backup_file(path, report)
                    sqlite_backed_up = True
                assignments = ", ".join(f"{name}=?" for name in updates)
                con.execute(
                    f"update threads set {assignments} where id=?",
                    [*updates.values(), row["id"]],
                )

        if sqlite_updates:
            report.add_action(f"{path}: sqlite row updates={sqlite_updates}")
            if report.apply:
                con.commit()
                con.execute("pragma wal_checkpoint(passive)")
                report.mark_changed(path)
        else:
            report.add_check(f"{path}: no sqlite row changes needed")
    except sqlite3.Error as exc:
        report.add_warning(f"{path}: sqlite repair failed: {exc}")
    finally:
        con.close()
    return rows_for_global_state


def update_rollout_first_line(path: Path, provider: str, cwd: str | None) -> bool:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, raw in enumerate(lines[:20]):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            return False
        changed = False
        if payload.get("model_provider") != provider:
            payload["model_provider"] = provider
            changed = True
        if cwd and payload.get("cwd") != cwd:
            payload["cwd"] = cwd
            changed = True
        if not changed:
            return False
        lines[index] = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return True
    return False


def repair_rollouts(rows: list[dict[str, Any]], provider: str, report: RepairReport) -> None:
    changed = 0
    unreadable = 0
    for row in rows:
        rollout_path = normalize_win_path(row.get("rollout_path"))
        if not rollout_path:
            continue
        path = Path(rollout_path)
        if not path.exists():
            continue
        try:
            would_change = False
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if isinstance(payload, dict):
                    cwd = normalize_win_path(row.get("cwd"))
                    would_change = (
                        payload.get("model_provider") != provider
                        or (bool(cwd) and payload.get("cwd") != cwd)
                    )
                break
            if not would_change:
                continue
            changed += 1
            if report.apply:
                backup_file(path, report)
                if update_rollout_first_line(path, provider, normalize_win_path(row.get("cwd"))):
                    report.mark_changed(path)
        except OSError:
            unreadable += 1
    if changed:
        report.add_action(f"rollout first-line metadata updates={changed}")
    else:
        report.add_check("rollout first-line metadata: no changes needed")
    if unreadable:
        report.add_warning(f"unreadable rollout files={unreadable}")


def repair_session_jsonl_compat(
    codex_home: Path, report: RepairReport, drop_invalid_lines: bool = False
) -> None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        report.add_check("session JSONL compatibility: no sessions directory")
        return

    files_seen = 0
    files_changed = 0
    invalid_files = 0
    invalid_lines_total = 0
    dropped_invalid_lines = 0
    skipped_invalid_files = 0
    turn_context_updates = 0
    response_id_updates = 0
    response_turn_id_updates = 0

    for path in sorted(sessions_root.rglob("*.jsonl")):
        files_seen += 1
        current_turn_id: str | None = None
        thread_id: str | None = None
        file_changed = False
        file_turn_context_updates = 0
        file_response_id_updates = 0
        file_response_turn_id_updates = 0
        invalid_lines: list[int] = []
        out_lines: list[str] = []

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            report.add_warning(f"{path}: cannot read session jsonl: {exc}")
            continue

        for line_no, raw in enumerate(lines, 1):
            if not raw.strip():
                out_lines.append(raw)
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                invalid_lines.append(line_no)
                if not drop_invalid_lines:
                    out_lines.append(raw)
                else:
                    file_changed = True
                continue

            payload = obj.get("payload")
            line_changed = False
            if obj.get("type") == "session_meta" and isinstance(payload, dict):
                if isinstance(payload.get("id"), str):
                    thread_id = payload["id"]
            elif obj.get("type") == "turn_context" and isinstance(payload, dict):
                if isinstance(payload.get("turn_id"), str):
                    current_turn_id = payload["turn_id"]
                if "multi_agent_version" not in payload:
                    payload["multi_agent_version"] = "v1"
                    file_turn_context_updates += 1
                    line_changed = True
            elif obj.get("type") == "response_item" and isinstance(payload, dict):
                if is_model_response_item(payload):
                    if "id" not in payload:
                        payload["id"] = synthetic_response_item_id(
                            thread_id, current_turn_id, line_no, payload
                        )
                        file_response_id_updates += 1
                        line_changed = True
                    if current_turn_id:
                        metadata = payload.get("metadata")
                        if not isinstance(metadata, dict):
                            metadata = {}
                            payload["metadata"] = metadata
                        if "turn_id" not in metadata:
                            metadata["turn_id"] = current_turn_id
                            file_response_turn_id_updates += 1
                            line_changed = True

            if line_changed:
                file_changed = True
                out_lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            else:
                out_lines.append(raw)

        if invalid_lines:
            invalid_files += 1
            invalid_lines_total += len(invalid_lines)
            if drop_invalid_lines:
                dropped_invalid_lines += len(invalid_lines)
            else:
                report.add_warning(
                    f"{path}: invalid JSONL lines={len(invalid_lines)}; "
                    "pass --drop-invalid-jsonl-lines with --apply to remove them"
                )
                skipped_invalid_files += 1
                continue

        if not file_changed:
            continue

        files_changed += 1
        turn_context_updates += file_turn_context_updates
        response_id_updates += file_response_id_updates
        response_turn_id_updates += file_response_turn_id_updates
        if report.apply:
            backup_file(path, report)
            path.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
            report.mark_changed(path)

    if files_changed:
        report.add_action(
            "session JSONL compatibility updates: "
            f"files={files_changed}, turn_context_v1={turn_context_updates}, "
            f"response_ids={response_id_updates}, "
            f"response_turn_ids={response_turn_id_updates}, "
            f"dropped_invalid_lines={dropped_invalid_lines}, "
            f"skipped_invalid_files={skipped_invalid_files}"
        )
    else:
        report.add_check("session JSONL compatibility: no changes needed")
    report.add_check(
        "session JSONL compatibility scan: "
        f"files={files_seen}, invalid_files={invalid_files}, "
        f"invalid_lines={invalid_lines_total}, "
        f"skipped_invalid_files={skipped_invalid_files}"
    )


def all_user_rows_for_rollouts(codex_home: Path) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sqlite_paths(codex_home):
        try:
            con = sqlite3.connect(path, timeout=5)
            con.row_factory = sqlite3.Row
            if not has_threads_table(con):
                continue
            for row in con.execute(
                "select id, source, thread_source, model_provider, cwd, rollout_path "
                f"from threads where {user_thread_filter_sql()}"
            ):
                rows[row["id"]] = {
                    "id": row["id"],
                    "cwd": normalize_win_path(row["cwd"]),
                    "rollout_path": normalize_win_path(row["rollout_path"]),
                }
        except sqlite3.Error:
            continue
        finally:
            try:
                con.close()
            except Exception:
                pass
    return list(rows.values())


def normalize_sidebar_key(value: str) -> str:
    if value.startswith(THREAD_KEY_PREFIXES):
        return value
    return local_thread_key(value)


def sync_global_state(rows: list[dict[str, Any]], report: RepairReport) -> None:
    global_state = report.codex_home / ".codex-global-state.json"
    if not global_state.exists():
        report.add_warning(f"{global_state}: missing global state file")
        return
    try:
        data = json.loads(global_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report.add_warning(f"{global_state}: cannot read json: {exc}")
        return

    roots = [
        (normalize_win_path(root) or root).rstrip("\\/")
        for root in data.get("electron-saved-workspace-roots", [])
        if isinstance(root, str) and root
    ]
    roots_by_len = sorted(set(roots), key=len, reverse=True)
    if not roots_by_len:
        report.add_check("global state: no saved workspace roots")
        return

    hints = dict(data.get("thread-workspace-root-hints", {}))
    assignments = dict(data.get("thread-project-assignments", {}))
    sidebar_orders = dict(data.get("sidebar-project-thread-orders", {}))
    project_order = [
        (normalize_win_path(project_id) or project_id).rstrip("\\/")
        for project_id in data.get("project-order", [])
        if isinstance(project_id, str) and project_id
    ]
    projectless = set(
        item for item in data.get("projectless-thread-ids", []) if isinstance(item, str)
    )
    project_threads: dict[str, list[tuple[int, str]]] = {}

    for row in rows:
        thread_id = row.get("id")
        cwd = normalize_win_path(row.get("cwd"))
        if not isinstance(thread_id, str) or not cwd:
            continue
        cwd_cmp = cwd.rstrip("\\/").lower()
        match = None
        for root in roots_by_len:
            root_cmp = root.lower()
            if cwd_cmp == root_cmp or cwd_cmp.startswith(root_cmp + "\\"):
                match = root
                break
        if not match:
            continue
        hints[thread_id] = match
        assignments[thread_id] = {
            "projectKind": "local",
            "projectId": match,
            "path": match,
            "pendingCoreUpdate": False,
        }
        try:
            recency = int(row.get("recency_at") or row.get("updated_at") or 0)
        except Exception:
            recency = 0
        project_threads.setdefault(match, []).append((recency, local_thread_key(thread_id)))
        projectless.discard(thread_id)

    if not project_threads:
        report.add_check("global state: no matching project rows")
        return

    for project_id, items in project_threads.items():
        thread_ids = [thread_id for _, thread_id in sorted(items, reverse=True)]
        existing = sidebar_orders.get(project_id)
        if isinstance(existing, dict):
            prior = [
                normalize_sidebar_key(item)
                for item in existing.get("threadIds", [])
                if isinstance(item, str)
            ]
            missing = [item for item in thread_ids if item not in set(prior)]
            if missing:
                sidebar_orders[project_id] = {**existing, "threadIds": missing + prior}
        else:
            sidebar_orders[project_id] = {"threadIds": thread_ids}

    new_project_order = project_order + [
        item for item in project_threads if item not in set(project_order)
    ]

    changed = (
        data.get("thread-workspace-root-hints") != hints
        or data.get("thread-project-assignments") != assignments
        or data.get("sidebar-project-thread-orders") != sidebar_orders
        or data.get("project-order") != new_project_order
        or set(data.get("projectless-thread-ids", [])) != projectless
    )
    if not changed:
        report.add_check("global state: no sidebar cache changes needed")
        return

    report.add_action(
        f"global state project mappings refreshed for {len(project_threads)} projects"
    )
    if report.apply:
        backup_file(global_state, report)
        data["thread-workspace-root-hints"] = hints
        data["thread-project-assignments"] = assignments
        data["sidebar-project-thread-orders"] = sidebar_orders
        data["project-order"] = new_project_order
        data["projectless-thread-ids"] = sorted(projectless)
        global_state.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        report.mark_changed(global_state)


def run_doctor(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser()
    provider = current_model_provider(codex_home)
    report = RepairReport(codex_home=codex_home, provider=provider, apply=False)
    report.add_check(f"target provider={provider}")

    dbs = sqlite_paths(codex_home)
    if not dbs:
        report.add_warning("no state_5.sqlite stores found")
    for path in dbs:
        doctor_sqlite(path, provider, report)

    global_state = codex_home / ".codex-global-state.json"
    if global_state.exists():
        try:
            data = json.loads(global_state.read_text(encoding="utf-8"))
            report.add_check(
                "global state: "
                f"workspace_roots={len(data.get('electron-saved-workspace-roots', []))}, "
                f"project_orders={len(data.get('sidebar-project-thread-orders', {}))}, "
                f"projectless={len(data.get('projectless-thread-ids', []))}"
            )
        except Exception as exc:
            report.add_warning(f"global state unreadable: {exc}")
    else:
        report.add_warning("global state file missing")

    print_report(report, as_json=args.json)
    return 0 if not report.warnings else 1


def run_repair(args: argparse.Namespace) -> int:
    codex_home = Path(args.codex_home).expanduser()
    provider = current_model_provider(codex_home)
    report = RepairReport(codex_home=codex_home, provider=provider, apply=args.apply)
    report.add_check(f"target provider={provider}")
    if not args.apply:
        report.add_check("dry-run only; pass --apply to write backups and changes")

    dbs = sqlite_paths(codex_home)
    if not dbs:
        report.add_warning("no state_5.sqlite stores found")
        print_report(report, as_json=args.json)
        return 1

    rows_for_global: dict[str, dict[str, Any]] = {}
    for path in dbs:
        for row in repair_sqlite(
            path, provider, report, repair_user_flags=args.repair_user_flags
        ):
            rows_for_global[row["id"]] = row
    if not args.no_rollouts:
        repair_rollouts(all_user_rows_for_rollouts(codex_home), provider, report)
    if args.repair_jsonl_compat:
        repair_session_jsonl_compat(
            codex_home, report, drop_invalid_lines=args.drop_invalid_jsonl_lines
        )
    if not args.no_global_state:
        sync_global_state(list(rows_for_global.values()), report)

    if report.apply and report.backup_dir:
        report.add_check(f"backup_dir={report.backup_dir}")
    print_report(report, as_json=args.json)
    return 0 if not report.warnings else 1


def print_report(report: RepairReport, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    print(f"codex_home: {report.codex_home}")
    print(f"target_provider: {report.provider}")
    print(f"mode: {'apply' if report.apply else 'dry-run'}")
    if report.backup_dir:
        print(f"backup_dir: {report.backup_dir}")
    if report.checks:
        print("checks:")
        for item in report.checks:
            print(f"  - {item}")
    if report.actions:
        print("actions:")
        for item in report.actions:
            print(f"  - {item}")
    if report.changed_files:
        print("changed_files:")
        for path in report.changed_files:
            print(f"  - {path}")
    if report.warnings:
        print("warnings:")
        for item in report.warnings:
            print(f"  - {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-sidebar-repair",
        description="Repair local Codex Desktop sidebar history after provider switches.",
    )
    parser.add_argument(
        "--codex-home",
        default=str(default_codex_home()),
        help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="Read-only diagnostics.")
    doctor.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    doctor.set_defaults(func=run_doctor)

    repair = subparsers.add_parser("repair", help="Preview or apply repair.")
    repair.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    repair.add_argument("--apply", action="store_true", help="Write backups and changes.")
    repair.add_argument(
        "--no-rollouts",
        action="store_true",
        help="Do not update session JSONL first-line metadata.",
    )
    repair.add_argument(
        "--no-global-state",
        action="store_true",
        help="Do not update .codex-global-state.json project/sidebar hints.",
    )
    repair.add_argument(
        "--repair-user-flags",
        action="store_true",
        help="Also force user-owned rows to thread_source=user and has_user_event=1.",
    )
    repair.add_argument(
        "--repair-jsonl-compat",
        action="store_true",
        help=(
            "Also repair session JSONL compatibility metadata for older Codex "
            "Desktop runtimes."
        ),
    )
    repair.add_argument(
        "--drop-invalid-jsonl-lines",
        action="store_true",
        help=(
            "With --repair-jsonl-compat, remove invalid JSONL lines after backup. "
            "Valid records are preserved."
        ),
    )
    repair.set_defaults(func=run_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
