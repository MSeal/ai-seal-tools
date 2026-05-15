# ai-seal-tools

Personal AI exploration repo — utilities, skills, and agents that make work and personal life more efficient. See [CLAUDE.md](CLAUDE.md) for the project philosophy and structure.

## Setup

Requires Python 3 and [uv](https://docs.astral.sh/uv/).

```bash
# Clone, then from the repo root:
uv sync
```

This machine's global `~/.config/uv/uv.toml` routes uv through Confluent's internal CodeArtifact registry. The repo's `.envrc` sets `UV_NO_CONFIG=1` automatically if you use [direnv](https://direnv.net/):

```bash
brew install direnv   # one-time
direnv allow          # once per clone
```

Without direnv, prefix uv commands manually: `UV_NO_CONFIG=1 uv sync`.

## Installing Skills

Skills under `skills/` need to be registered with Claude Code before they're invocable as `/<name>` in any session:

```bash
uv run utils/install_skills.py
```

This symlinks each `skills/<name>/` into `~/.claude/skills/<name>/` and writes a `.skill-env` into each skill recording the active Python/Node/npx paths so helper scripts run under the right interpreters. It also pins any `npx`-launched MCP servers in `.mcp.json` to the captured node version by injecting it onto the server's `env.PATH` — keeping `command`/`args` untouched so MDM allowlist matching still works on managed machines.

If a skill has a `links.yaml`, the installer additionally creates personal-config symlinks under `config.local/<skill>/` (gitignored). Each entry's `target` is the real file location — typically a Google Drive Desktop sync folder so prefs persist across machines. If the Drive folder isn't present, the installer falls back to `~/.config/<skill>/` and warns; on a later run when Drive is available, the real file is migrated to Drive and the fallback path becomes a symlink. See `skills/find-meeting-time/SETUP.md` for the concrete example.

Re-run after pulling new skills, after `nvm use <ver>`, or after recreating `.venv`.

Other flags:

```bash
uv run utils/install_skills.py --dry-run    # preview changes
uv run utils/install_skills.py --force      # replace non-symlink targets
uv run utils/install_skills.py --uninstall  # remove the symlinks
```

## Tests

```bash
uv run pytest
```

Tests live in `tests/` and cover:

- `utils/install_skills.py` — Drive availability detection, symlink idempotency, the bidirectional state-migration flows when Drive comes online / goes offline, `pin_mcp_json` PATH injection.
- `skills/find-meeting-time/freebusy.py` — the event-movability classifier (title-rule corpus + `eventType` overrides + opaque/declined/transparent cases), `score_slot` penalty math, `dedup_and_rank` conflict-signature collisions, and `load_working_hours` YAML parsing.

Tests don't hit Google APIs — anything that requires network or auth is excluded so the suite runs offline in under two seconds.

If you're behind Confluent's CodeArtifact and don't have direnv configured: `UV_NO_CONFIG=1 uv run pytest`.

## Running Things

```bash
uv run utils/<tool>.py             # standalone utility
uv run agents/<name>/agent.py      # agent
```
