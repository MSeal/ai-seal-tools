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

If a .mcp.json.template exists at the repo root, this installer generates
.mcp.json from it, injecting the captured node's bin directory onto each
npx-launched server's `env.PATH`. The template is the canonical, committed
source; the generated .mcp.json is gitignored and carries the machine-
specific PATH. This keeps the committed file clean while still defeating
nvm version drift between shells. Confluent's MDM allowlist gates MCP
loading on a literal match of `command` + `args` (env isn't validated), so
the template-with-env-injection scheme stays compliant. Re-run after
`nvm use <ver>` to re-pin to a different node.

If a skill has a `links.yaml`, this installer also creates personal-config
symlinks under <repo>/config.local/<skill>/ pointing to the canonical target
paths (typically ~/.config/ai-seal-tools/<skill>/). The targets are created
from `template_if_missing` files on first install so the symlinks aren't
dangling. `config.local/` is gitignored so the symlinks stay personal —
they exist purely so you can open your prefs from the repo without typing
the hidden-directory path.

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

import yaml


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
        install_config_links(skill, repo, args.dry_run, prefix)
    mcp_action = pin_mcp_json(repo / ".mcp.json.template", repo / ".mcp.json", runtimes, args.dry_run)
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


PUBLIC_NPM_REGISTRY = "https://registry.npmjs.org/"


def pin_mcp_json(template_file: Path, mcp_file: Path, runtimes: dict[str, str], dry: bool) -> str:
    """Materialize `mcp_file` from `template_file` with machine-specific
    env injected for any npx-launched server:

    - `PATH` is rewritten to lead with the captured node's bin directory,
      defeating nvm version drift between shells.
    - `NPM_CONFIG_REGISTRY` is pinned to the public npm registry. On work
      machines, `~/.npmrc` routes npm through Confluent's CodeArtifact,
      whose auth tokens expire every ~12 hours. When the token expires,
      `npx <package>` returns E401 silently and the MCP server fails to
      launch in fresh sessions. Public registry is the right default for
      MCP packages anyway — they're all published there.

    Source of truth is `.mcp.json.template` (committed, canonical). The
    generated `.mcp.json` (gitignored) carries the env injection. Reading
    from the template each run keeps the generated file from accumulating
    cruft and means the env injection is always relative to the canonical
    command/args.

    Confluent's MDM allowlist gates MCP loading on a literal match of
    `command` + `args` (see CLAUDE memory `mdm-mcp-allowlist`). The
    template holds the literal-match form; env is invisible to MDM, so
    injecting it is safe.
    """
    if not template_file.exists():
        return f"skip (no {template_file.name})"
    node = runtimes.get("NODE")
    if not node:
        return "skip (node not on PATH)"
    node_bin = str(Path(node).parent)

    data = json.loads(template_file.read_text())
    for server in data.get("mcpServers", {}).values():
        if server.get("command") != "npx":
            continue
        env = dict(server.get("env", {}))
        existing = env.get("PATH", os.environ.get("PATH", ""))
        parts = [p for p in existing.split(os.pathsep) if p and p != node_bin]
        env["PATH"] = os.pathsep.join([node_bin, *parts])
        env.setdefault("NPM_CONFIG_REGISTRY", PUBLIC_NPM_REGISTRY)
        server["env"] = env

    rendered = json.dumps(data, indent=2) + "\n"
    if mcp_file.exists() and mcp_file.read_text() == rendered:
        return "ok"
    if not dry:
        mcp_file.write_text(rendered)
    return "update"


def install_config_links(skill: Path, repo: Path, dry: bool, prefix: str) -> None:
    """Create per-skill personal-config symlinks declared in `links.yaml`.

    Schema:
      - target: absolute path to the *desired* real file (typically inside
        Google Drive Desktop's sync folder).
      - symlinks: list of paths that should resolve to the real file.
        Absolute or `~`-paths used as-is; relative paths resolve against
        the repo root.
      - template_if_missing: relative-to-this-skill template path. Used to
        materialize the real file if no existing file is found at the
        desired target or at any symlink path.

    Drive-availability + fallback:
      The installer checks whether `target`'s Drive-account folder exists.
      If yes (e.g., Drive Desktop is set up and synced), the real file lives
      at `target` and all `symlinks` point to it.
      If no (Drive not installed, different account, etc.), it falls back
      to using `symlinks[0]` as the real file and warns the user. Other
      symlinks point at this fallback location.

    Bidirectional migration — survives changes to Drive availability without
    losing file state:
      - Real file at desired target, Drive went away → next install detects
        the dangling symlink at fallback, promotes its (likely still-cached)
        content to a real file.
      - Real file at fallback (~/.config/), Drive came online → next install
        moves the real file to Drive and replaces the fallback path with a
        symlink. No data loss either direction.
    """
    spec_file = skill / "links.yaml"
    if not spec_file.is_file():
        return
    spec = yaml.safe_load(spec_file.read_text()) or []
    if not spec:
        return

    for entry in spec:
        desired_target = Path(entry["target"]).expanduser()
        symlinks = [_resolve_symlink_path(s, repo) for s in entry["symlinks"]]
        tmpl_rel = entry.get("template_if_missing")

        if _drive_target_available(desired_target):
            effective = desired_target
        else:
            effective = symlinks[0] if symlinks else desired_target
            print(
                f"{prefix}{skill.name}: WARNING — Drive folder not found for "
                f"{desired_target.parent}; using {_shown(effective, repo)} as fallback real file"
            )

        target_action = _ensure_real_file(skill, effective, symlinks, desired_target, tmpl_rel, dry)
        if target_action.startswith("skipped"):
            continue

        for link_path in symlinks:
            if link_path == effective:
                # This path IS the real file; nothing to symlink here.
                print(f"{prefix}{skill.name}: {_shown(link_path, repo)} [real file, target {target_action}]")
                continue
            link_action = _create_symlink(link_path, effective, dry)
            print(f"{prefix}{skill.name}: {_shown(link_path, repo)} → {effective} [{link_action}]")


def _resolve_symlink_path(spec: str, repo: Path) -> Path:
    p = Path(spec).expanduser()
    if p.is_absolute():
        return p
    return repo / p


def _shown(path: Path, repo: Path) -> str:
    """Pretty-print a path: repo-relative if under the repo, else absolute."""
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def _drive_target_available(target: Path) -> bool:
    """True if `target`'s Drive account folder exists on disk.

    On macOS, Drive Desktop mounts each signed-in account at
    `~/Library/CloudStorage/GoogleDrive-<email>/`. We check whether that
    specific account dir exists — if so, Drive is set up for this user and
    the target path is usable (intermediate dirs we'll create). If not, the
    user either doesn't have Drive Desktop or is signed in to a different
    account, and we should fall back.

    Targets outside `~/Library/CloudStorage/` are treated as always available.
    """
    drive_root = Path.home() / "Library" / "CloudStorage"
    try:
        rel = target.relative_to(drive_root)
    except ValueError:
        return True  # not a Drive path
    if not rel.parts:
        return False
    return (drive_root / rel.parts[0]).exists()


def _ensure_real_file(
    skill: Path,
    effective: Path,
    symlinks: list[Path],
    desired_target: Path,
    tmpl_rel: str | None,
    dry: bool,
) -> str:
    """Make sure `effective` is a real file, migrating from other locations
    as needed to preserve any existing state."""

    # Already a real file in the right place
    if effective.exists() and not effective.is_symlink():
        return "exists"

    # Symlink at the effective path — try to resolve its content and promote
    # it to a real file. Handles the "Drive went offline; this used to be a
    # symlink to Drive" case so we don't lose data.
    if effective.is_symlink():
        try:
            content = effective.read_text()
        except (FileNotFoundError, OSError):
            content = None
        if not dry:
            effective.unlink()
        if content is not None:
            if not dry:
                effective.parent.mkdir(parents=True, exist_ok=True)
                effective.write_text(content)
            return "promoted from symlink"
        # Dangling symlink, no recoverable content; fall through

    # Look for a real file elsewhere we can migrate from. Includes the
    # `desired_target` (handles the "Drive came online; ~/.config has the
    # real file" case) and all symlink paths.
    candidates = [desired_target] + symlinks
    for candidate in candidates:
        if candidate == effective:
            continue
        if candidate.exists() and not candidate.is_symlink():
            if not dry:
                effective.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(candidate), str(effective))
            return f"migrated from {candidate}"

    # Template fallback
    if tmpl_rel:
        template = skill / tmpl_rel
        if not template.is_file():
            return f"skipped (template {template} missing)"
        if not dry:
            effective.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template, effective)
        return "materialized from template"

    return "skipped (no source, no template)"


def _create_symlink(link_path: Path, target: Path, dry: bool) -> str:
    if link_path.is_symlink():
        if Path(os.readlink(link_path)) == target:
            return "ok"
        if not dry:
            link_path.unlink()
            link_path.parent.mkdir(parents=True, exist_ok=True)
            link_path.symlink_to(target)
        return "relink"
    if link_path.exists():
        return "skip (non-symlink in the way)"
    if not dry:
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(target)
    return "link"


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
