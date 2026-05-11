"""Pure data layer: scan ~/Projects/, query the GitHub CLI, and parse git state.

Nothing in this module imports from Textual or Rich. It's the layer the TUI
talks to when it needs to know what exists on disk or on github.com.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECTS_DIR = Path.home() / "Projects"

REPO_FIELDS = [
    "name",
    "nameWithOwner",
    "primaryLanguage",
    "visibility",
]

GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def preflight() -> None:
    if shutil.which("gh") is None:
        sys.exit("error: `gh` (GitHub CLI) not found on PATH. Install it first.")

    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit("error: `gh` is not authenticated. Run `gh auth login` first.")

    if not PROJECTS_DIR.is_dir():
        sys.exit(f"error: projects directory {PROJECTS_DIR} does not exist.")


def fetch_github_repos(limit: int = 1000) -> list[dict]:
    logger.debug("fetch_github_repos: limit=%d", limit)
    result = subprocess.run(
        [
            "gh", "repo", "list",
            "--limit", str(limit),
            "--json", ",".join(REPO_FIELDS),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    repos = json.loads(result.stdout)
    logger.info("fetch_github_repos: %d repos returned", len(repos))
    return repos


def github_remote_for(project_path: Path) -> str | None:
    """Return 'owner/repo' if origin points at github.com, else None."""
    result = subprocess.run(
        ["git", "-C", str(project_path), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = GITHUB_REMOTE_RE.match(result.stdout.strip())
    if not match:
        return None
    return f"{match['owner']}/{match['repo']}"


def read_branch(project_path: Path) -> str | None:
    """Read the current branch name from .git/HEAD. Detached HEAD → short SHA."""
    try:
        head = (project_path / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):]
    return head[:7] if head else None


def git_status_summary(project_path: Path) -> dict | None:
    """Run `git status --porcelain --branch` and parse dirty / ahead / behind.

    Returns {"dirty": int, "ahead": int|None, "behind": int|None} or None
    on error. ahead/behind are None if the branch has no upstream tracking.
    """
    result = subprocess.run(
        ["git", "-C", str(project_path), "status", "--porcelain", "--branch"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    branch_line = lines[0] if lines else ""
    ahead = behind = 0
    has_upstream = "..." in branch_line
    if has_upstream and "[" in branch_line and "]" in branch_line:
        bracket = branch_line[branch_line.index("[") + 1:branch_line.index("]")]
        for part in bracket.split(","):
            part = part.strip()
            if part.startswith("ahead "):
                ahead = int(part[6:])
            elif part.startswith("behind "):
                behind = int(part[7:])
    dirty = sum(1 for line in lines[1:] if line.strip())
    return {
        "dirty": dirty,
        "ahead": ahead if has_upstream else None,
        "behind": behind if has_upstream else None,
    }


def scan_local_projects() -> list[dict]:
    projects = []
    for entry in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or not (entry / ".git").exists():
            continue
        projects.append({
            "name": entry.name,
            "path": entry,
            "github": github_remote_for(entry),
            "branch": read_branch(entry),
        })
    logger.info("scan_local_projects: %d git-initialized dirs in %s",
                len(projects), PROJECTS_DIR)
    return projects
