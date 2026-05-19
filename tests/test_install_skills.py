"""Tests for utils/install_skills.py — symlink + Drive-fallback logic.

Focused on the cases that have actually bit us:
  - Drive availability detection
  - Bidirectional state migration (fallback ↔ drive)
  - Symlink idempotency
  - .mcp.json env.PATH pinning (idempotency)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import install_skills as ins


# ---------------------------------------------------------------------------
# _drive_target_available
# ---------------------------------------------------------------------------

def test_drive_target_available_existing_account():
    """Real account dir under ~/Library/CloudStorage/ → True."""
    home = Path.home()
    cloud = home / "Library" / "CloudStorage"
    if not cloud.exists():
        pytest.skip("No ~/Library/CloudStorage on this machine")
    accounts = [d for d in cloud.iterdir() if d.is_dir() and d.name.startswith("GoogleDrive-")]
    if not accounts:
        pytest.skip("No GoogleDrive-* accounts present")
    target = accounts[0] / "My Drive" / "anywhere" / "x.yaml"
    assert ins._drive_target_available(target) is True


def test_drive_target_available_fake_account():
    """Fake account dir not present → False."""
    fake = Path.home() / "Library" / "CloudStorage" / "GoogleDrive-NOPE@example.com" / "My Drive" / "x.yaml"
    assert ins._drive_target_available(fake) is False


def test_drive_target_available_non_drive_path():
    """Path outside ~/Library/CloudStorage/ → always available."""
    assert ins._drive_target_available(Path.home() / ".config" / "anything") is True
    assert ins._drive_target_available(Path("/tmp/somewhere")) is True


# ---------------------------------------------------------------------------
# _create_symlink
# ---------------------------------------------------------------------------

def test_create_symlink_link(tmp_path):
    """Nothing at link_path → creates symlink."""
    target = tmp_path / "real.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    assert ins._create_symlink(link, target, dry=False) == "link"
    assert link.is_symlink()
    assert link.read_text() == "hi"


def test_create_symlink_ok_when_already_correct(tmp_path):
    """Symlink already points at target → 'ok', no churn."""
    target = tmp_path / "real.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert ins._create_symlink(link, target, dry=False) == "ok"
    assert link.is_symlink()


def test_create_symlink_relink_when_wrong_target(tmp_path):
    """Symlink points elsewhere → 'relink' and now points at target."""
    real = tmp_path / "real.txt"
    real.write_text("hi")
    wrong = tmp_path / "wrong.txt"
    wrong.write_text("nope")
    link = tmp_path / "link.txt"
    link.symlink_to(wrong)
    assert ins._create_symlink(link, real, dry=False) == "relink"
    assert link.is_symlink() and Path(os.readlink(link)) == real


def test_create_symlink_skip_when_non_symlink_in_way(tmp_path):
    """A real file at the link path is preserved (we don't clobber user data)."""
    target = tmp_path / "real.txt"
    target.write_text("hi")
    link = tmp_path / "link.txt"
    link.write_text("user content")
    assert ins._create_symlink(link, target, dry=False) == "skip (non-symlink in the way)"
    assert link.read_text() == "user content"  # unchanged


# ---------------------------------------------------------------------------
# install_config_links — end-to-end Drive fallback / promotion flows
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_drive(tmp_path, monkeypatch):
    """Replace _drive_target_available so tests can pretend a tempdir-rooted
    'Library/CloudStorage' is the real one."""
    drive_root = tmp_path / "Library" / "CloudStorage"

    def patched(target: Path) -> bool:
        try:
            rel = target.relative_to(drive_root)
        except ValueError:
            return True
        if not rel.parts:
            return False
        return (drive_root / rel.parts[0]).exists()

    monkeypatch.setattr(ins, "_drive_target_available", patched)
    return drive_root


def _scaffold_test_skill(tmp_path: Path, drive_root: Path):
    """Build a minimal repo + skill + links.yaml referencing a Drive path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    skill = repo / "skills" / "test-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# test")
    (skill / "templates").mkdir()
    (skill / "templates" / "config.yaml").write_text("# template content\n")

    drive_target = drive_root / "GoogleDrive-test@example.com" / "My Drive" / "x" / "config.yaml"
    fallback = tmp_path / ".config" / "test-skill" / "config.yaml"
    repo_link_rel = "config.local/test-skill/config.yaml"
    repo_link = repo / repo_link_rel

    (skill / "links.yaml").write_text(
        f"- target: {drive_target}\n"
        f"  symlinks:\n"
        f"    - {fallback}\n"
        f"    - {repo_link_rel}\n"
        f"  template_if_missing: templates/config.yaml\n"
    )
    return repo, skill, drive_target, fallback, repo_link


def test_first_run_drive_unavailable_falls_back_to_config(tmp_path, fake_drive, capsys):
    """Drive missing → real file at ~/.config-style fallback path, other
    symlinks point there, Drive target NOT created."""
    repo, skill, drive_target, fallback, repo_link = _scaffold_test_skill(tmp_path, fake_drive)

    ins.install_config_links(skill, repo, dry=False, prefix="")
    out = capsys.readouterr().out
    assert "WARNING" in out  # warns about missing Drive folder

    assert fallback.exists() and not fallback.is_symlink()
    assert fallback.read_text() == "# template content\n"
    assert repo_link.is_symlink() and Path(os.readlink(repo_link)) == fallback
    assert not drive_target.exists()


def test_drive_comes_online_migrates_with_state_preservation(tmp_path, fake_drive):
    """Fallback has user edits; Drive comes online; install moves the file
    to Drive (preserving edits) and the fallback becomes a symlink."""
    repo, skill, drive_target, fallback, repo_link = _scaffold_test_skill(tmp_path, fake_drive)

    # Run 1: Drive unavailable, fallback gets template
    ins.install_config_links(skill, repo, dry=False, prefix="")
    # User edits the fallback file
    fallback.write_text("# my edits\n")

    # Drive comes online
    (fake_drive / "GoogleDrive-test@example.com").mkdir(parents=True)

    # Run 2: should migrate fallback → Drive
    ins.install_config_links(skill, repo, dry=False, prefix="")
    assert drive_target.exists() and not drive_target.is_symlink()
    assert drive_target.read_text() == "# my edits\n"
    assert fallback.is_symlink() and Path(os.readlink(fallback)) == drive_target
    assert repo_link.is_symlink() and Path(os.readlink(repo_link)) == drive_target


def test_drive_offline_promotes_symlink_to_real_file(tmp_path, fake_drive):
    """Drive was online (real file there, symlinks point at it). Drive
    'goes offline' (we keep the file in place — Drive Desktop's local cache
    stays available even when offline). Install should promote the fallback
    symlink to a real file at that path."""
    repo, skill, drive_target, fallback, repo_link = _scaffold_test_skill(tmp_path, fake_drive)

    # Setup: Drive online, run install, edit the real file
    (fake_drive / "GoogleDrive-test@example.com").mkdir(parents=True)
    ins.install_config_links(skill, repo, dry=False, prefix="")
    drive_target.write_text("# important state\n")

    # Drive "goes offline" — remove the account dir entry but KEEP the file
    # (simulates "Drive Desktop process not running but local files cached").
    # In our tempdir we have to be cruder — move the real file out so symlinks
    # dangle, but stash content somewhere we can recover by reading the symlink.
    # Actually, the cleanest simulation: keep Drive content where it is, just
    # make _drive_target_available return False. Symlinks still resolve.
    monkey_root = tmp_path / "PRETEND_GONE"
    monkey_root.mkdir()

    def patched_unavailable(_target):
        return False

    import unittest.mock as mock
    with mock.patch.object(ins, "_drive_target_available", patched_unavailable):
        ins.install_config_links(skill, repo, dry=False, prefix="")

    # After: fallback should be a real file with the content (read from the
    # symlink before it was unlinked)
    assert fallback.exists() and not fallback.is_symlink()
    assert fallback.read_text() == "# important state\n"


def test_idempotent_no_op(tmp_path, fake_drive):
    """Run install twice in steady state — second run should be no-op."""
    repo, skill, drive_target, fallback, repo_link = _scaffold_test_skill(tmp_path, fake_drive)
    (fake_drive / "GoogleDrive-test@example.com").mkdir(parents=True)
    ins.install_config_links(skill, repo, dry=False, prefix="")
    drive_target.write_text("# stable content\n")
    drive_mtime = drive_target.stat().st_mtime

    # Second run — file should not be touched
    ins.install_config_links(skill, repo, dry=False, prefix="")
    assert drive_target.stat().st_mtime == drive_mtime
    assert drive_target.read_text() == "# stable content\n"


# ---------------------------------------------------------------------------
# pin_mcp_json
# ---------------------------------------------------------------------------

def _write_mcp(path: Path, command: str, args: list[str], env: dict | None = None):
    data = {"mcpServers": {"playwright": {"command": command, "args": args}}}
    if env is not None:
        data["mcpServers"]["playwright"]["env"] = env
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_pin_mcp_json_generates_from_template(tmp_path):
    """Template → generated file with env.PATH injected; template untouched.
    Leading PATH order: <shim-dir>, ~/.local/bin, captured node-bin."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    action = ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    assert action == "update"
    assert mcp.exists()
    written = json.loads(mcp.read_text())
    env = written["mcpServers"]["playwright"]["env"]
    path_dirs = env["PATH"].split(":")
    assert path_dirs[0] == ins.MCP_SHIMS_DIR
    assert path_dirs[1] == ins.USER_BIN_DIR
    assert path_dirs[2] == "/opt/node/v26/bin"
    assert written["mcpServers"]["playwright"]["command"] == "npx"
    assert written["mcpServers"]["playwright"]["args"] == ["@playwright/mcp@latest"]
    # template is unchanged (no env injected back into it)
    assert "env" not in json.loads(template.read_text())["mcpServers"]["playwright"]


def test_pin_mcp_json_idempotent(tmp_path):
    """Second run with same node + same template → 'ok', no rewrite."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}
    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    assert ins.pin_mcp_json(template, mcp, runtimes, dry=False) == "ok"


def test_pin_mcp_json_skips_non_npx(tmp_path):
    """Non-npx entries in the template come through unchanged (no env added)."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "chewie", ["mcp-server", "serve"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    written = json.loads(mcp.read_text())
    assert "env" not in written["mcpServers"]["playwright"]


def test_pin_mcp_json_skips_when_template_missing(tmp_path):
    """No template at the repo root → skip with a clear message, do nothing."""
    template = tmp_path / "does-not-exist.template"
    mcp = tmp_path / ".mcp.json"
    runtimes = {"NODE": "/opt/node/v26/bin/node"}
    action = ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    assert action.startswith("skip")
    assert not mcp.exists()


def test_pin_mcp_json_does_not_inject_env_for_non_npx(tmp_path):
    """Non-npx commands don't fetch from npm at all; no env injection."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "chewie", ["mcp-server", "serve"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    server = json.loads(mcp.read_text())["mcpServers"]["playwright"]
    assert "env" not in server


def test_pin_mcp_json_does_not_inject_for_unknown_wrapper(tmp_path):
    """If a future MCP entry uses a bare-name wrapper as its command
    (rather than `npx`), the current install_skills only injects env
    for commands in NPX_BASED_COMMANDS. That set will need to grow if
    we add such an entry — until then, no injection for other commands
    (the MDM allowlist would block them anyway)."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "some-future-wrapper", ["serve"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    server = json.loads(mcp.read_text())["mcpServers"]["playwright"]
    assert "env" not in server


def test_pin_mcp_json_does_not_pin_npm_registry(tmp_path):
    """Don't bypass the managed npm registry — pinning it in env would
    sidestep Confluent's package-install controls. Users refresh the
    CodeArtifact token before invoking MCP-using skills if it's expired."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    env = json.loads(mcp.read_text())["mcpServers"]["playwright"]["env"]
    assert "NPM_CONFIG_REGISTRY" not in env


def test_pin_mcp_json_path_ordering(tmp_path):
    """For npx servers, env.PATH must lead with the shim dir (so our
    npx shim wins lookup), then ~/.local/bin, then captured node-bin,
    then the rest. This ordering is the contract install_mcp_shims +
    pin_mcp_json + utils/npx-mcp-shim depend on."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    env = json.loads(mcp.read_text())["mcpServers"]["playwright"]["env"]
    path_dirs = env["PATH"].split(":")
    assert path_dirs[0] == ins.MCP_SHIMS_DIR
    assert path_dirs[1] == ins.USER_BIN_DIR
    assert path_dirs[2] == "/opt/node/v26/bin"


def test_pin_mcp_json_strips_duplicate_leading_dirs(tmp_path, monkeypatch):
    """If the inherited PATH already contains our leading dirs, strip
    them from later positions so the intended ordering wins."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    # Inherit a PATH that has node-bin AND user-bin AND shim-dir already
    # in it, in a different order.
    polluted_path = ":".join([
        "/some/random/dir",
        ins.USER_BIN_DIR,
        "/opt/node/v26/bin",
        ins.MCP_SHIMS_DIR,
        "/usr/local/bin",
    ])
    monkeypatch.setenv("PATH", polluted_path)

    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    env = json.loads(mcp.read_text())["mcpServers"]["playwright"]["env"]
    path_dirs = env["PATH"].split(":")
    # Each of the three leading dirs appears exactly once, at the head.
    assert path_dirs[:3] == [ins.MCP_SHIMS_DIR, ins.USER_BIN_DIR, "/opt/node/v26/bin"]
    assert path_dirs.count(ins.MCP_SHIMS_DIR) == 1
    assert path_dirs.count(ins.USER_BIN_DIR) == 1
    assert path_dirs.count("/opt/node/v26/bin") == 1


# ---------------------------------------------------------------------------
# install_wrapper_scripts
# ---------------------------------------------------------------------------


def _make_fake_repo_with_wrappers(tmp_path: Path) -> Path:
    """Build a fake repo containing all WRAPPER_SCRIPTS as executable
    files. Returns the repo path. Used by tests that exercise normal
    install paths (which require every wrapper to be present)."""
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    for name in ins.WRAPPER_SCRIPTS:
        script = repo / "utils" / name
        script.write_text("#!/usr/bin/env bash\n")
        script.chmod(0o755)
    return repo


def test_install_wrapper_scripts_creates_symlinks(tmp_path, monkeypatch):
    """Run from a fake repo with all wrappers present; expect a symlink
    at ~/.local/bin/<name> pointing at utils/<name> for each entry in
    WRAPPER_SCRIPTS."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = _make_fake_repo_with_wrappers(tmp_path)
    action = ins.install_wrapper_scripts(repo, dry=False)
    assert "linked" in action

    for name in ins.WRAPPER_SCRIPTS:
        target = fake_home / ".local" / "bin" / name
        assert target.is_symlink(), f"{name} not symlinked"
        assert Path(os.readlink(target)) == (repo / "utils" / name).resolve()


def test_install_wrapper_scripts_idempotent(tmp_path, monkeypatch):
    """Second run with the symlinks already correct returns 'ok' — no
    rewrite, no flap."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = _make_fake_repo_with_wrappers(tmp_path)
    ins.install_wrapper_scripts(repo, dry=False)
    assert ins.install_wrapper_scripts(repo, dry=False) == "ok"


def test_install_wrapper_scripts_relinks_when_stale(tmp_path, monkeypatch):
    """If a symlink at the target points somewhere else (e.g., user
    moved the repo), replace it. Don't error out, don't leave a stale
    pointer."""
    fake_home = tmp_path / "home"
    bin_dir = fake_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = _make_fake_repo_with_wrappers(tmp_path)

    # Pre-existing symlink pointing at a stale path for the first wrapper.
    first_wrapper = ins.WRAPPER_SCRIPTS[0]
    stale = tmp_path / "old-location" / first_wrapper
    stale.parent.mkdir(parents=True)
    stale.write_text("# old")
    target = bin_dir / first_wrapper
    target.symlink_to(stale)

    action = ins.install_wrapper_scripts(repo, dry=False)
    assert "linked" in action
    assert Path(os.readlink(target)) == (repo / "utils" / first_wrapper).resolve()


def test_install_wrapper_scripts_skips_non_executable(tmp_path, monkeypatch):
    """If any wrapper isn't marked executable (e.g., checked in without
    the +x bit), the installer reports skip with a clear note rather
    than linking a useless target."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = _make_fake_repo_with_wrappers(tmp_path)
    # Strip executable bit from one wrapper to simulate the bug.
    (repo / "utils" / ins.WRAPPER_SCRIPTS[0]).chmod(0o644)

    action = ins.install_wrapper_scripts(repo, dry=False)
    assert "skip" in action
    assert "not executable" in action


def test_wrapper_scripts_registry_includes_signin(tmp_path, monkeypatch):
    """WRAPPER_SCRIPTS is the install registry for terminal-invoked
    helpers (NOT MCP-spawned commands — those route through MCP_SHIMS).
    Locks in that we ship the playwright-sign-in helper."""
    assert "playwright-sign-in" in ins.WRAPPER_SCRIPTS
    # The earlier playwright-mcp-with-profile wrapper has been
    # superseded by the MCP_SHIMS approach; this guards against an
    # accidental revival.
    assert "playwright-mcp-with-profile" not in ins.WRAPPER_SCRIPTS


def test_install_wrapper_scripts_links_multiple(tmp_path, monkeypatch):
    """When WRAPPER_SCRIPTS has multiple entries and all are present
    + executable, all get symlinked."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    for name in ins.WRAPPER_SCRIPTS:
        script = repo / "utils" / name
        script.write_text("#!/usr/bin/env bash\n")
        script.chmod(0o755)

    action = ins.install_wrapper_scripts(repo, dry=False)
    assert "linked" in action
    bin_dir = fake_home / ".local" / "bin"
    for name in ins.WRAPPER_SCRIPTS:
        assert (bin_dir / name).is_symlink(), f"{name} not symlinked"


def test_install_wrapper_scripts_dry_run_makes_no_changes(tmp_path, monkeypatch):
    """--dry-run should preview but not create the symlinks."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    repo = _make_fake_repo_with_wrappers(tmp_path)
    action = ins.install_wrapper_scripts(repo, dry=True)
    assert "linked" in action
    bin_dir = fake_home / ".local" / "bin"
    for name in ins.WRAPPER_SCRIPTS:
        assert not (bin_dir / name).exists(), f"{name} should not be linked in dry-run"


# ---------------------------------------------------------------------------
# install_mcp_shims
# ---------------------------------------------------------------------------


def _make_fake_repo_with_shims(tmp_path: Path) -> Path:
    """Like _make_fake_repo_with_wrappers but for MCP_SHIMS sources."""
    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True, exist_ok=True)
    for src_name in set(ins.MCP_SHIMS.values()):
        src = repo / "utils" / src_name
        src.write_text("#!/usr/bin/env bash\nexec /usr/bin/env npx \"$@\"\n")
        src.chmod(0o755)
    return repo


def test_install_mcp_shims_creates_symlinks_under_installed_names(tmp_path, monkeypatch):
    """utils/<src> → <MCP_SHIMS_DIR>/<installed-name>. The installed
    name (e.g., `npx`) is what the MCP server will look up on PATH."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # MCP_SHIMS_DIR was captured at import time against the real home;
    # rebind it for this test against the fake home.
    monkeypatch.setattr(ins, "MCP_SHIMS_DIR", str(fake_home / ".config" / "ai-seal-tools" / "mcp-shims"))

    repo = _make_fake_repo_with_shims(tmp_path)
    action = ins.install_mcp_shims(repo, dry=False)
    assert "linked" in action

    for installed_name, src_name in ins.MCP_SHIMS.items():
        target = Path(ins.MCP_SHIMS_DIR) / installed_name
        assert target.is_symlink(), f"shim {installed_name} not symlinked"
        assert Path(os.readlink(target)) == (repo / "utils" / src_name).resolve()


def test_install_mcp_shims_idempotent(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(ins, "MCP_SHIMS_DIR", str(fake_home / ".config" / "ai-seal-tools" / "mcp-shims"))

    repo = _make_fake_repo_with_shims(tmp_path)
    ins.install_mcp_shims(repo, dry=False)
    assert ins.install_mcp_shims(repo, dry=False) == "ok"


def test_install_mcp_shims_skips_when_source_missing(tmp_path, monkeypatch):
    """If the source file disappeared from utils/, skip with a clear
    note — don't error out the whole install pass."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(ins, "MCP_SHIMS_DIR", str(fake_home / ".config" / "ai-seal-tools" / "mcp-shims"))

    repo = tmp_path / "repo"
    (repo / "utils").mkdir(parents=True)
    # No shim sources written.

    action = ins.install_mcp_shims(repo, dry=False)
    assert "skip" in action
    assert "missing" in action


def test_mcp_shims_registry_includes_npx(tmp_path, monkeypatch):
    """The MCP shim registry must expose `npx` (mapping to npx-mcp-shim).
    That's the entrypoint MDM-allowlisted Playwright MCP entries hit."""
    assert "npx" in ins.MCP_SHIMS
    assert ins.MCP_SHIMS["npx"] == "npx-mcp-shim"


# ---------------------------------------------------------------------------
# npx-mcp-shim runtime behavior
# ---------------------------------------------------------------------------


def _shim_subprocess(tmp_path, args, extra_env=None):
    """Run the real utils/npx-mcp-shim with a fake `real npx` that just
    echoes its argv. Returns the CompletedProcess. Used to verify the
    shim's intercept/passthrough behavior end-to-end without bringing
    up an actual MCP server.

    Inherits the host PATH so the shim's `#!/usr/bin/env bash` shebang
    can find bash, then prepends our shim+real test dirs so the shim
    wins lookup and finds our fake real-npx next."""
    import subprocess
    repo_root = Path(__file__).resolve().parents[1]
    shim_src = repo_root / "utils" / "npx-mcp-shim"
    assert shim_src.exists()

    shim_dir = tmp_path / "shim-dir"
    real_dir = tmp_path / "real-dir"
    shim_dir.mkdir()
    real_dir.mkdir()
    (shim_dir / "npx").symlink_to(shim_src)
    fake_real = real_dir / "npx"
    fake_real.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    fake_real.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{real_dir}:{env.get('PATH', '')}"
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [str(shim_dir / "npx"), *args],
        capture_output=True, env=env, text=True,
    )
    return result


def test_shim_passthrough_for_unrelated_packages(tmp_path):
    """Non-Playwright packages must reach real npx with args unchanged."""
    r = _shim_subprocess(tmp_path, ["-y", "@some/other-mcp@latest"])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["-y", "@some/other-mcp@latest"]


def test_shim_injects_playwright_flags(tmp_path):
    """@playwright/mcp@latest gets --user-data-dir and --headless
    appended after the package name (so the flags reach playwright-mcp,
    not npx itself)."""
    r = _shim_subprocess(tmp_path, ["-y", "@playwright/mcp@latest"])
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == "-y"
    assert lines[1] == "@playwright/mcp@latest"
    assert "--user-data-dir" in lines
    assert "--headless" in lines
    # The flags appear immediately after the package name.
    pkg_idx = lines.index("@playwright/mcp@latest")
    assert lines[pkg_idx + 1] == "--user-data-dir"
    assert lines[pkg_idx + 3] == "--headless"


def test_shim_injects_for_versioned_playwright(tmp_path):
    """Match on @playwright/mcp@<anything>, not just @latest — covers
    pinned versions like @playwright/mcp@1.50.0."""
    r = _shim_subprocess(tmp_path, ["@playwright/mcp@1.50.0"])
    assert r.returncode == 0, r.stderr
    assert "--user-data-dir" in r.stdout
    assert "--headless" in r.stdout


def test_shim_respects_profile_dir_env_override(tmp_path):
    """PLAYWRIGHT_MCP_PROFILE_DIR overrides the default profile location
    so users can point at a different profile (e.g., for a one-off
    test) without rebuilding the shim."""
    custom = tmp_path / "custom-profile"
    r = _shim_subprocess(
        tmp_path,
        ["-y", "@playwright/mcp@latest"],
        extra_env={"PLAYWRIGHT_MCP_PROFILE_DIR": str(custom)},
    )
    assert r.returncode == 0, r.stderr
    assert str(custom) in r.stdout


def test_shim_errors_when_no_real_npx(tmp_path):
    """If PATH only contains the shim dir + system dirs that don't have
    npx, the shim should exit non-zero with a clear message rather than
    recursing into itself.

    Inherits the host PATH for bash/coreutils resolution but strips any
    dir containing an npx executable so only our shim's `npx` survives."""
    import shutil, subprocess
    repo_root = Path(__file__).resolve().parents[1]
    shim_src = repo_root / "utils" / "npx-mcp-shim"
    shim_dir = tmp_path / "shim-dir"
    shim_dir.mkdir()
    (shim_dir / "npx").symlink_to(shim_src)

    # Build a PATH that has /bin and /usr/bin (for bash) but no node bin dir.
    host_path = os.environ.get("PATH", "")
    clean_parts = [
        p for p in host_path.split(":")
        if p and not (Path(p) / "npx").exists()
    ]
    test_path = f"{shim_dir}:" + ":".join(clean_parts)

    result = subprocess.run(
        [str(shim_dir / "npx"), "-y", "@playwright/mcp@latest"],
        capture_output=True,
        env={"PATH": test_path, "HOME": str(tmp_path)},
        text=True,
    )
    assert result.returncode != 0
    assert "no real npx" in result.stderr


def test_pin_mcp_json_regenerates_canonical_when_mcp_drifted(tmp_path):
    """If something modified .mcp.json (e.g. an old install pass), running
    again from the template overwrites it back to the canonical+env form."""
    template = tmp_path / ".mcp.json.template"
    mcp = tmp_path / ".mcp.json"
    _write_mcp(template, "npx", ["@playwright/mcp@latest"])
    # Simulate drift: someone wrote bogus content into .mcp.json
    mcp.write_text('{"mcpServers": {"playwright": {"command": "wrong"}}}')
    runtimes = {"NODE": "/opt/node/v26/bin/node"}
    ins.pin_mcp_json(template, mcp, runtimes, dry=False)
    written = json.loads(mcp.read_text())
    assert written["mcpServers"]["playwright"]["command"] == "npx"
    assert "env" in written["mcpServers"]["playwright"]
