# 2026-07-06 JSONL compatibility case

## Summary

Some Codex Desktop project sidebar entries showed `新对话` even though the
local history was still present. SQLite titles and `session_index.jsonl` were
valid. The Desktop runtime could not parse selected session JSONL files, so the
sidebar fell back to a new-chat display label.

The earlier `50` theory was false. It came from using a diagnostic query with
`limit=50`; it was not a Desktop sidebar limit.

## Confirmed failure modes

1. Old `0.130.0-alpha.5` JSONL sessions were missing runtime metadata expected
   by newer Desktop builds:
   - model-produced `response_item.payload.id`
   - model-produced `response_item.payload.metadata.turn_id`
   - `turn_context.payload.multi_agent_version = "v1"`
2. Some newer `0.142.0-alpha.6` sessions contained invalid JSONL fragments.
   One malformed line was enough for the Desktop reader to reject the thread.

## Signals that separated the causes

- SQLite `threads.title` was valid.
- `session_index.jsonl` had the correct titles.
- `codex_app.read_thread` returned `No Codex thread found` for affected IDs.
- After adding `multi_agent_version` to one old failing session, `read_thread`
  immediately returned the real title.
- After dropping invalid JSONL fragments from one newer failing session,
  `read_thread` immediately returned the real title.

## Local repair result

The local repair used backups before every write and changed only compatibility
metadata or invalid JSONL fragments.

- Added missing `multi_agent_version` to visible/sidebar old sessions.
- Added missing model-produced response item IDs and turn metadata in the
  earlier migration pass.
- Removed invalid JSONL lines only after backup.
- Verified all 49 visible sidebar session files had:
  - no missing `multi_agent_version`
  - no JSON parse errors
- Verified representative failed threads with `read_thread`, including:
  - `启动一下这个项目`
  - `这个项目介绍一下`
  - `填写毕业论文过程性材料`
  - `这个项目git了吗？你看看`
  - `研究一下获取宝石有没有漏洞。可以无限获取宝石的`
  - `用一下`
  - `继续`

## Productized behavior

This project now exposes the JSONL repair as an explicit opt-in path:

```powershell
python -m codex_sidebar_repair repair --repair-jsonl-compat
python -m codex_sidebar_repair repair --repair-jsonl-compat --apply
python -m codex_sidebar_repair repair --repair-jsonl-compat --drop-invalid-jsonl-lines --apply
```

Default repair remains conservative. Invalid JSONL lines are reported by
default and are only removed when `--drop-invalid-jsonl-lines` is provided.

## Safety notes

- The tool does not print conversation content.
- The tool does not upload local Codex data.
- The tool backs up changed files before writing.
- Invalid JSONL cleanup preserves all valid records and drops only lines that
  cannot be parsed as JSON.
