from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
