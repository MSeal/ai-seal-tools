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


def test_pin_mcp_json_injects_path_for_npx(tmp_path):
    """An entry with command=npx gets node bin prepended onto env.PATH."""
    mcp = tmp_path / ".mcp.json"
    _write_mcp(mcp, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}

    action = ins.pin_mcp_json(mcp, runtimes, dry=False)
    assert action == "update"
    written = json.loads(mcp.read_text())
    env = written["mcpServers"]["playwright"]["env"]
    assert env["PATH"].startswith("/opt/node/v26/bin:")
    assert written["mcpServers"]["playwright"]["command"] == "npx"
    assert written["mcpServers"]["playwright"]["args"] == ["@playwright/mcp@latest"]


def test_pin_mcp_json_idempotent(tmp_path):
    """Re-pinning with the same node should be a no-op."""
    mcp = tmp_path / ".mcp.json"
    _write_mcp(mcp, "npx", ["@playwright/mcp@latest"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}
    ins.pin_mcp_json(mcp, runtimes, dry=False)
    assert ins.pin_mcp_json(mcp, runtimes, dry=False) == "ok"


def test_pin_mcp_json_skips_non_npx(tmp_path):
    """An entry with a non-npx command should be left alone entirely."""
    mcp = tmp_path / ".mcp.json"
    _write_mcp(mcp, "chewie", ["mcp-server", "serve"])
    runtimes = {"NODE": "/opt/node/v26/bin/node"}
    assert ins.pin_mcp_json(mcp, runtimes, dry=False) == "ok"
    written = json.loads(mcp.read_text())
    assert "env" not in written["mcpServers"]["playwright"]
