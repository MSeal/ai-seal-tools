#!/usr/bin/env python3
"""Install project skills into ~/.claude/skills/ as symlinks.

Each subdirectory of ./skills/ containing a SKILL.md is symlinked into
~/.claude/skills/<name>/ so it becomes invocable as /<name> in any Claude Code
session. A .skill-env file is written into each project skill dir recording the
runtimes that were active at install time:

    PYTHON=<repo>/.venv/bin/python     (always — required, from uv sync)
    NODE=<abs path to node>            (if node is on PATH)
    NPX=<abs path to npx>              (if npx is on PATH)

Helper scripts a skill calls out to should read .skill-env rather than relying
on $PATH, so they run under the same interpreters the install was wired up for.

If a .mcp.json exists at the repo root, any MCP server with `command: "npx"` is
also rewritten in-place to launch via the captured absolute node + npx paths.
That keeps the MCP servers from breaking when Claude Code is launched from a
shell with a different nvm default selected. Re-run after `nvm use <ver>` to
re-pin to a different node.

Usage:
    uv run utils/install_skills.py              # install or update all
    uv run utils/install_skills.py --dry-run    # show what would change
    uv run utils/install_skills.py --force      # replace non-symlink targets
    uv run utils/install_skills.py --uninstall  # remove the symlinks
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing non-symlink entries in ~/.claude/skills/",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove symlinks this installer created",
    )
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    src_root = repo / "skills"
    dst_root = Path.home() / ".claude" / "skills"

    if not src_root.is_dir():
        sys.exit(f"No skills directory at {src_root}")

    dst_root.mkdir(parents=True, exist_ok=True)

    skills = [
        d for d in sorted(src_root.iterdir())
        if d.is_dir() and (d / "SKILL.md").is_file()
    ]
    if not skills:
        sys.exit(f"No SKILL.md files found under {src_root}")

    prefix = "[dry-run] " if args.dry_run else ""

    if args.uninstall:
        for skill in skills:
            uninstall_one(skill.name, dst_root, args.dry_run, prefix)
        return

    runtimes = discover_runtimes(repo)
    print(f"{prefix}runtimes: " + ", ".join(f"{k}={v}" for k, v in runtimes.items()))
    for skill in skills:
        install_one(skill, dst_root / skill.name, runtimes, args.force, args.dry_run, prefix)
    mcp_action = pin_mcp_json(repo / ".mcp.json", runtimes, args.dry_run)
    print(f"{prefix}.mcp.json: {mcp_action}")


def discover_runtimes(repo: Path) -> dict[str, str]:
    """Capture the interpreters that should drive skill helpers and MCP servers."""
    venv_python = repo / ".venv" / "bin" / "python"
    if not venv_python.exists():
        sys.exit(f"Missing {venv_python} — run `uv sync` first")
    runtimes = {"PYTHON": str(venv_python)}
    for tool in ("node", "npx"):
        found = shutil.which(tool)
        if found:
            runtimes[tool.upper()] = found
    return runtimes


def install_one(src: Path, dst: Path, runtimes: dict[str, str], force: bool, dry: bool, prefix: str) -> None:
    action = link_action(src, dst, force)
    if not dry:
        if action in ("relink", "replace"):
            if dst.is_symlink() or not dst.is_dir():
                dst.unlink()
            else:
                shutil.rmtree(dst)
        if action in ("link", "relink", "replace"):
            dst.symlink_to(src, target_is_directory=True)

    env_action = write_skill_env(src, runtimes, dry)
    print(f"{prefix}{src.name}: link={action}, env={env_action}")


def link_action(src: Path, dst: Path, force: bool) -> str:
    if dst.is_symlink():
        return "ok" if Path(os.readlink(dst)) == src else "relink"
    if dst.exists():
        return "replace" if force else "skip (exists, pass --force to overwrite)"
    return "link"


def write_skill_env(src: Path, runtimes: dict[str, str], dry: bool) -> str:
    env_file = src / ".skill-env"
    content = "".join(f"{k}={v}\n" for k, v in runtimes.items())
    if env_file.exists() and env_file.read_text() == content:
        return "ok"
    action = "update" if env_file.exists() else "write"
    if not dry:
        env_file.write_text(content)
    return action


def pin_mcp_json(mcp_file: Path, runtimes: dict[str, str], dry: bool) -> str:
    """Rewrite npx-launched MCP servers to run under the captured node/npx.

    This avoids the MCP server inheriting whatever node version was active in
    the shell that launched Claude Code (which on this machine often defaults
    to an old nvm version that lacks newer JS features).
    """
    if not mcp_file.exists():
        return "skip (no .mcp.json)"
    node = runtimes.get("NODE")
    npx = runtimes.get("NPX")
    if not node or not npx:
        return "skip (node or npx not on PATH)"

    data = json.loads(mcp_file.read_text())
    changed = False
    for server in data.get("mcpServers", {}).values():
        cmd = server.get("command", "")
        args = list(server.get("args", []))

        # Detect prior pinning so we can re-pin to the current runtimes.
        previously_pinned = (
            cmd.endswith("/node") or cmd.endswith("\\node.exe")
        ) and args and (args[0].endswith("/npx") or args[0].endswith("\\npx"))

        if cmd == "npx" or cmd.endswith("/npx"):
            new_args = [npx, *args]
        elif previously_pinned:
            new_args = [npx, *args[1:]]
        else:
            continue

        if server.get("command") != node or server.get("args") != new_args:
            server["command"] = node
            server["args"] = new_args
            changed = True

    if not changed:
        return "ok"
    if not dry:
        mcp_file.write_text(json.dumps(data, indent=2) + "\n")
    return "update"


def uninstall_one(name: str, dst_root: Path, dry: bool, prefix: str) -> None:
    dst = dst_root / name
    if dst.is_symlink():
        if not dry:
            dst.unlink()
        print(f"{prefix}{name}: removed")
    elif dst.exists():
        print(f"{prefix}{name}: skipped (not a symlink — leaving alone)")
    else:
        print(f"{prefix}{name}: not installed")


if __name__ == "__main__":
    main()
