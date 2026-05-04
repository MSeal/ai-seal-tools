# ai-seal-tools

Personal AI exploration repo. The goal is to build utilities, skills, and agents that make work and personal life more efficient — and to discover what's actually useful in an AI-assisted world versus what just sounds good.

## Philosophy

- **Experiment first.** Try it quickly; refine if it's useful.
- **Personal = opinionated.** These tools are for one person. Don't abstract prematurely or add config options "just in case."
- **Real value only.** A tool that saves 5 minutes a week earns its place. One that takes longer to invoke than doing the thing manually doesn't.
- **Composable over monolithic.** Small focused scripts/agents that pipe into each other beat one big framework.

## Structure

```
agents/       # Claude SDK agents for multi-step autonomous tasks
skills/       # Claude Code slash-command skills (SKILL.md files)
utils/        # Standalone scripts and utilities
prompts/      # Reusable prompt templates
```

Create the relevant directory when adding the first file in a new category.

## Tech Stack

- **Python** — primary language; use `uv` for dependency management
- **Claude API** — use `anthropic` SDK with prompt caching enabled by default
- **Claude Code skills** — for things invoked from the Claude Code CLI

## Adding a New Utility

1. Drop it in `utils/` as a standalone script.
2. Add a one-line docstring at the top describing what it does and when to use it.
3. Accept input from stdin or args; print to stdout. Keep it pipeable.
4. Dependencies go in a `pyproject.toml` or inline `uv` script header if tiny.

## Adding a New Agent

1. Create `agents/<name>/agent.py` (or `__init__.py` for packages).
2. Use the Anthropic SDK with `claude-sonnet-4-6` as the default model unless reasoning depth warrants Opus.
3. Always enable prompt caching (`cache_control` on system prompts and large context blocks).
4. Document the agent's purpose, inputs, and outputs in a top-level docstring.

## Adding a New Skill

Skills are invoked as slash commands from Claude Code. Each lives in `skills/<name>/SKILL.md`.

```
skills/<name>/
  SKILL.md      # The skill instructions Claude Code executes
  *.py          # Any helper scripts the skill calls out to
```

Reference the [Claude Code skills documentation](https://docs.anthropic.com/en/docs/claude-code/skills) for the SKILL.md format.

## Claude API Patterns

Always use these defaults when writing new Claude API code in this repo:

```python
import anthropic

client = anthropic.Anthropic()

# Cache system prompts on long-running agents
system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

# Default model choice
MODEL = "claude-sonnet-4-6"   # fast + capable
# MODEL = "claude-opus-4-7"   # when deep reasoning matters
```

## Running Things

```bash
# Run a utility directly
uv run utils/my_tool.py

# Run an agent
uv run agents/my_agent/agent.py
```

## uv / PyPI Note

This machine's global `~/.config/uv/uv.toml` routes uv through Confluent's internal
CodeArtifact registry (work config). To use public PyPI here, run uv with `UV_NO_CONFIG=1`.

The `.envrc` file sets this automatically if you use [direnv](https://direnv.net/):
```bash
brew install direnv   # one-time
direnv allow          # once per clone
```

Without direnv, prefix uv commands manually: `UV_NO_CONFIG=1 uv add <package>`

## What to Build Next

Capture ideas here as they come up so future sessions have context:

- [ ] Daily standup summarizer (pull from calendar + recent git activity)
- [ ] Meeting notes → action items extractor
- [ ] Email triage agent (prioritize, draft replies)
- [ ] Code review pre-check (run before opening a PR)
- [ ] Expense categorizer from receipts/screenshots
