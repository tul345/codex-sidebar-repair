from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_sidebar_repair.cli import main, normalize_win_path


def make_state_db(path: Path, rollout: Path, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            model_provider text not null,
            cwd text not null,
            title text not null,
            sandbox_policy text not null,
            approval_mode text not null,
            has_user_event integer not null default 0,
            archived integer not null default 0,
            thread_source text,
            preview text not null default '',
            recency_at integer not null default 0
        )
        """
    )
    con.execute(
        """
        insert into threads (
          id, rollout_path, created_at, updated_at, source, model_provider, cwd,
          title, sandbox_policy, approval_mode, has_user_event, archived,
          thread_source, preview, recency_at
        ) values (?, ?, 1, 20, '', 'openai', ?, '', '', '', 0, 0, null, '', 30)
        """,
        ("abc", str(rollout), cwd),
    )
    con.commit()
    con.close()


class RepairTests(unittest.TestCase):
    def test_normalize_win_path(self) -> None:
        self.assertEqual(normalize_win_path(r"\\?\C:\tmp"), r"C:\tmp")
        self.assertEqual(normalize_win_path(r"\\?\UNC\server\share"), r"\\server\share")

    def test_json_flag_is_accepted_after_doctor_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".codex"
            root.mkdir()
            (root / "config.toml").write_text('model_provider = "Codex"\n', encoding="utf-8")

            stdout = StringIO()
            with patch("sys.stdout", stdout):
                result = main(["--codex-home", str(root), "doctor", "--json"])

            self.assertEqual(result, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["provider"], "Codex")
            self.assertTrue(payload["warnings"])

    def test_repair_apply_updates_provider_and_global_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".codex"
            root.mkdir()
            cwd = str(Path(temp) / "project")
            prefixed_cwd = "\\\\?\\" + cwd
            rollout = root / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "abc",
                            "cwd": prefixed_cwd,
                            "model_provider": "openai",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "config.toml").write_text('model_provider = "Codex"\n', encoding="utf-8")
            make_state_db(root / "state_5.sqlite", rollout, prefixed_cwd)
            (root / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [cwd],
                        "thread-workspace-root-hints": {},
                        "thread-project-assignments": {},
                        "sidebar-project-thread-orders": {},
                        "project-order": [],
                        "projectless-thread-ids": ["abc"],
                    }
                ),
                encoding="utf-8",
            )

            result = main(["--codex-home", str(root), "repair", "--apply"])
            self.assertEqual(result, 0)

            con = sqlite3.connect(root / "state_5.sqlite")
            row = con.execute(
                "select model_provider, cwd, thread_source, has_user_event from threads where id='abc'"
            ).fetchone()
            con.close()
            self.assertEqual(row, ("Codex", cwd, None, 0))

            first = json.loads(rollout.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(first["payload"]["model_provider"], "Codex")
            self.assertEqual(first["payload"]["cwd"], cwd)

            state = json.loads((root / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["thread-workspace-root-hints"]["abc"], cwd)
            self.assertIn("local:abc", state["sidebar-project-thread-orders"][cwd]["threadIds"])
            self.assertNotIn("abc", state["projectless-thread-ids"])

    def test_dry_run_does_not_modify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".codex"
            root.mkdir()
            cwd = str(Path(temp) / "project")
            rollout = root / "sessions" / "rollout.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                '{"type":"session_meta","payload":{"id":"abc","model_provider":"openai"}}\n',
                encoding="utf-8",
            )
            (root / "config.toml").write_text('model_provider = "Codex"\n', encoding="utf-8")
            make_state_db(root / "state_5.sqlite", rollout, cwd)
            before = (root / "state_5.sqlite").read_bytes()

            result = main(["--codex-home", str(root), "repair", "--no-global-state"])
            self.assertEqual(result, 0)
            self.assertEqual((root / "state_5.sqlite").read_bytes(), before)

    def test_repair_jsonl_compat_adds_runtime_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".codex"
            root.mkdir()
            cwd = str(Path(temp) / "project")
            rollout = root / "sessions" / "2026" / "06" / "rollout-abc.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {
                                    "id": "abc",
                                    "cwd": cwd,
                                    "model_provider": "Codex",
                                    "cli_version": "0.130.0-alpha.5",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"type": "message", "role": "user"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn_context",
                                "payload": {"turn_id": "turn-1", "cwd": cwd},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"type": "message", "role": "assistant"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"type": "reasoning"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"type": "function_call"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {"type": "function_call_output"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "config.toml").write_text('model_provider = "Codex"\n', encoding="utf-8")
            make_state_db(root / "state_5.sqlite", rollout, cwd)

            result = main(
                [
                    "--codex-home",
                    str(root),
                    "repair",
                    "--repair-jsonl-compat",
                    "--no-rollouts",
                    "--no-global-state",
                    "--apply",
                ]
            )
            self.assertEqual(result, 0)

            records = [
                json.loads(line)
                for line in rollout.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            turn_context = records[2]["payload"]
            self.assertEqual(turn_context["multi_agent_version"], "v1")

            user_item = records[1]["payload"]
            self.assertNotIn("id", user_item)
            self.assertNotIn("metadata", user_item)

            for record in records[3:6]:
                payload = record["payload"]
                self.assertTrue(payload["id"].startswith("repair-"))
                self.assertEqual(payload["metadata"]["turn_id"], "turn-1")

            tool_output = records[6]["payload"]
            self.assertNotIn("id", tool_output)
            self.assertNotIn("metadata", tool_output)

    def test_drop_invalid_jsonl_lines_requires_explicit_option(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / ".codex"
            root.mkdir()
            cwd = str(Path(temp) / "project")
            rollout = root / "sessions" / "rollout-abc.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(
                '{"type":"session_meta","payload":{"id":"abc"}}\n'
                '{"type":"broken"\n'
                '{"type":"turn_context","payload":{"turn_id":"turn-1"}}\n',
                encoding="utf-8",
            )
            (root / "config.toml").write_text('model_provider = "Codex"\n', encoding="utf-8")
            make_state_db(root / "state_5.sqlite", rollout, cwd)

            result = main(
                [
                    "--codex-home",
                    str(root),
                    "repair",
                    "--repair-jsonl-compat",
                    "--no-rollouts",
                    "--no-global-state",
                    "--apply",
                ]
            )
            self.assertEqual(result, 1)
            self.assertIn('{"type":"broken"', rollout.read_text(encoding="utf-8"))

            result = main(
                [
                    "--codex-home",
                    str(root),
                    "repair",
                    "--repair-jsonl-compat",
                    "--drop-invalid-jsonl-lines",
                    "--no-rollouts",
                    "--no-global-state",
                    "--apply",
                ]
            )
            self.assertEqual(result, 0)

            lines = rollout.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                json.loads(line)
            self.assertEqual(json.loads(lines[1])["payload"]["multi_agent_version"], "v1")


if __name__ == "__main__":
    unittest.main()
