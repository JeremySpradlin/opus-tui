"""Textual TUI for browsing the authenticated user's GitHub repos.

Same gh-CLI plumbing as before, fronted by a Textual DataTable.
"""

import json
import shutil
import subprocess
import sys

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header

REPO_FIELDS = [
    "name",
    "description",
    "primaryLanguage",
    "stargazerCount",
    "updatedAt",
    "visibility",
    "isFork",
    "isArchived",
]


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


def fetch_repos(limit: int = 100) -> list[dict]:
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
    return json.loads(result.stdout)


def repo_row(repo: dict) -> tuple[str, str, str, int, str]:
    name = repo["name"]
    visibility = repo["visibility"].lower()
    lang = (repo.get("primaryLanguage") or {}).get("name") or "-"
    stars = repo["stargazerCount"]
    updated = repo["updatedAt"][:10]
    return name, visibility, lang, stars, updated


class ReposApp(App):
    TITLE = "opus-tui"
    SUB_TITLE = "your GitHub repos"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "sort_stars", "Sort by stars"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Name", key="name", width=30)
        table.add_column("Vis", key="vis", width=8)
        table.add_column("Lang", key="lang", width=14)
        table.add_column("Stars", key="stars", width=7)
        table.add_column("Updated", key="updated", width=12)
        table.loading = True
        self.load_repos()

    @work(thread=True)
    def load_repos(self) -> None:
        repos = fetch_repos()
        self.call_from_thread(self.populate, repos)

    def populate(self, repos: list[dict]) -> None:
        table = self.query_one(DataTable)
        for repo in repos:
            table.add_row(*repo_row(repo))
        table.loading = False
        self.sub_title = f"{len(repos)} repos"

    def action_sort_stars(self) -> None:
        self.query_one(DataTable).sort("stars", reverse=True)


def main() -> None:
    preflight()
    ReposApp().run()


if __name__ == "__main__":
    main()
