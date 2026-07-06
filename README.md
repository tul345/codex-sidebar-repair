# codex-sidebar-repair

Repair local Codex Desktop history visibility after switching `model_provider`,
API profiles, or auth methods.

This project targets a narrow data-layer failure:

- local session files and `state_5.sqlite` still exist;
- Codex Desktop sidebar shows missing or empty project conversations;
- rows were left under an older provider bucket, for example `openai`;
- project sidebar caches were not refreshed after the provider switch.
- older session JSONL files are present but cannot be parsed by the current
  Desktop runtime, causing titles to fall back to `新对话`.

The tool does not patch Codex Desktop binaries, does not install recovery
software, and does not upload or print conversation content.

## Safety

Default mode is read-only:

```powershell
python -m codex_sidebar_repair doctor
python -m codex_sidebar_repair repair
```

Write mode requires `--apply`:

```powershell
python -m codex_sidebar_repair repair --apply
```

Before any write, the tool creates backups under:

```text
%USERPROFILE%\.codex\backups_state\sidebar-repair\<timestamp>
```

Never commit local Codex data. The `.gitignore` excludes SQLite databases,
session JSONL files, global state, backups, and logs.

## What it repairs

- Reads the active provider from `~/.codex/config.toml`.
- Normalizes `\\?\` Windows path prefixes in Codex SQLite rows.
- Aligns user-owned thread rows in both known `state_5.sqlite` stores to the
  active provider.
- Optionally aligns rollout first-line metadata for readable session JSONL
  files.
- Optionally repairs session JSONL compatibility metadata used by newer Desktop
  runtimes:
  - `turn_context.payload.multi_agent_version = "v1"`
  - model-produced `response_item.payload.id`
  - model-produced `response_item.payload.metadata.turn_id`
- Optionally removes invalid JSONL lines after backup.
- Refreshes `.codex-global-state.json` project/sidebar hints using existing
  saved workspace roots.

## Usage

From a checkout, install in editable mode first:

```powershell
python -m pip install -e .
```

Run a read-only diagnostic:

```powershell
python -m codex_sidebar_repair doctor
```

Preview the repair:

```powershell
python -m codex_sidebar_repair repair
```

Apply the repair:

```powershell
python -m codex_sidebar_repair repair --apply
```

Repair old session JSONL compatibility metadata:

```powershell
python -m codex_sidebar_repair repair --repair-jsonl-compat
python -m codex_sidebar_repair repair --repair-jsonl-compat --apply
```

If the diagnostic reports invalid JSONL lines and you have reviewed the backup
location, remove only the unparsable lines:

```powershell
python -m codex_sidebar_repair repair --repair-jsonl-compat --drop-invalid-jsonl-lines --apply
```

If old rows have missing user-display flags and the dry-run says those flags
need repair, opt in explicitly:

```powershell
python -m codex_sidebar_repair repair --repair-user-flags --apply
```

Use a custom Codex home for tests or recovery:

```powershell
python -m codex_sidebar_repair repair --codex-home C:\Users\me\.codex --apply
```

Without installing, prefix the command with `PYTHONPATH=src`:

```powershell
$env:PYTHONPATH='src'; python -m codex_sidebar_repair doctor
```

After applying, fully quit and reopen Codex Desktop so it reloads local state.

## Development

Run the local validation script:

```powershell
.\scripts\check.ps1
```

It runs `compileall`, the unittest suite, and CLI smoke checks.

Build a wheel without requiring the `build` module:

```powershell
.\scripts\build-wheel.ps1
```

The wheel is written to `dist/`, which is intentionally gitignored.

## Field notes

The live 2026-07-06 repair is documented in:

```text
docs/2026-07-06-jsonl-compatibility-case.md
```

That case confirmed that the apparent `50` item limit was only a diagnostic
query limit, not a Desktop sidebar limit. The actual causes were old JSONL
runtime metadata gaps and a small number of malformed JSONL fragments.

## GitHub setup

Because this project must not include private Codex data, inspect the staged
files before pushing:

```powershell
git status --short
git diff --cached --stat
```

Then create a GitHub repository in the browser or install GitHub CLI, add the
remote, and push:

```powershell
git branch -M main
git remote add origin https://github.com/<owner>/codex-sidebar-repair.git
git push -u origin main
```

Use a private repository first if you are not sure whether future changes might
include local evidence files.
