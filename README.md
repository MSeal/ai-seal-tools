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

This symlinks each `skills/<name>/` into `~/.claude/skills/<name>/` and writes a `.skill-env` into each skill recording the active Python/Node/npx paths so helper scripts run under the right interpreters. It also re-pins any `npx`-launched MCP servers in `.mcp.json` to absolute node paths so they don't break when nvm switches versions.

Re-run after pulling new skills, after `nvm use <ver>`, or after recreating `.venv`.

Other flags:

```bash
uv run utils/install_skills.py --dry-run    # preview changes
uv run utils/install_skills.py --force      # replace non-symlink targets
uv run utils/install_skills.py --uninstall  # remove the symlinks
```

## Running Things

```bash
uv run utils/<tool>.py             # standalone utility
uv run agents/<name>/agent.py      # agent
```
