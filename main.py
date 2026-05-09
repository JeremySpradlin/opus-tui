"""Textual TUI for managing projects: local + GitHub side-by-side.

A "local project" is any direct subdir of ~/Projects/ that contains
a .git directory. A local project is considered "synced" with GitHub
when its origin remote points at a github.com URL whose owner/name
matches one of the user's gh repos.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header

PROJECTS_DIR = Path.home() / "Projects"

REPO_FIELDS = [
    "name",
    "nameWithOwner",
    "description",
    "primaryLanguage",
    "stargazerCount",
    "updatedAt",
    "visibility",
    "isFork",
    "isArchived",
]

GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

CTP = {
    "text":     "#cdd6f4", "subtext0": "#a6adc8", "overlay2": "#9399b2",
    "overlay1": "#7f849c", "overlay0": "#6c7086",
    "green":    "#a6e3a1", "teal":     "#94e2d5", "red":      "#f38ba8",
    "crust":    "#11111b",
}

LANGUAGE_COLORS = {
    "Python":     "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Go":         "#00ADD8", "Rust":       "#dea584", "Lua":        "#5d8fd1",
    "Shell":      "#89e051", "Bash":       "#89e051", "Ruby":       "#cc3434",
    "Java":       "#b07219", "C":          "#8a8a8a", "C++":        "#f34b7d",
    "C#":         "#178600", "HTML":       "#e34c26", "CSS":        "#a06ed7",
    "SCSS":       "#c6538c", "Vue":        "#41b883", "Swift":      "#F05138",
    "Kotlin":     "#A97BFF", "Elixir":     "#9A6CCB", "Haskell":    "#a78bfa",
    "Zig":        "#ec915c", "Nim":        "#ffc200", "Dart":       "#00B4AB",
    "Markdown":   "#89b4fa", "Vim Script": "#199f4b", "Nix":        "#7e7eff",
    "Fish":       "#4aae47", "Dockerfile": "#9caec4",
}


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


def scan_local_projects() -> list[dict]:
    projects = []
    for entry in sorted(PROJECTS_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or not (entry / ".git").exists():
            continue
        projects.append({
            "name": entry.name,
            "path": entry,
            "github": github_remote_for(entry),
        })
    return projects


def name_cell(name: str) -> Text:
    return Text(name, style=f"bold {CTP['text']}")


def remote_cell(remote: str | None) -> Text:
    if remote is None:
        return Text("—", style=f"dim {CTP['overlay0']}")
    return Text(remote, style=CTP["subtext0"])


def lang_cell(lang: str) -> Text:
    if lang == "-":
        return Text("—", style=f"dim {CTP['overlay0']}")
    color = LANGUAGE_COLORS.get(lang, CTP["overlay2"])
    return Text(f"● {lang}", style=f"bold {color}")


def vis_cell(visibility: str) -> Text:
    visibility = visibility.lower()
    if visibility == "public":
        return Text(" PUBLIC ", style=f"bold {CTP['crust']} on {CTP['green']}")
    return Text(" PRIVATE ", style=f"bold {CTP['crust']} on {CTP['red']}")


def local_sync_cell(synced: bool) -> Text:
    if synced:
        return Text("●", style=f"bold {CTP['green']}")
    return Text("○", style=CTP["overlay1"])


def github_sync_cell(synced: bool) -> Text:
    if synced:
        return Text("●", style=f"bold {CTP['green']}")
    return Text("☁", style=f"bold {CTP['teal']}")


class ProjectsApp(App):
    TITLE = "opus-tui"
    SUB_TITLE = "loading…"

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
    }
    Header {
        background: $background;
        color: $primary;
        text-style: bold;
    }
    Horizontal {
        height: 1fr;
    }
    DataTable {
        width: 1fr;
        height: 1fr;
        border: heavy $surface;
        border-title-color: $secondary;
        border-title-style: bold;
        border-title-align: left;
        margin: 0 1;
    }
    DataTable:focus {
        border: heavy $primary;
        border-title-color: $primary;
    }
    Footer {
        background: $surface;
        color: $foreground;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "switch_pane", "Switch pane"),
        Binding("r", "refresh", "Refresh"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            yield DataTable(id="local", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="github", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "catppuccin-mocha"

        local = self.query_one("#local", DataTable)
        local.border_title = "  Local · ~/Projects "
        local.add_column(" ", key="sync", width=2)
        local.add_column("Name", key="name")
        local.add_column("Remote", key="remote")
        local.loading = True

        github = self.query_one("#github", DataTable)
        github.border_title = "  GitHub "
        github.add_column(" ", key="sync", width=2)
        github.add_column("Name", key="name")
        github.add_column("Vis", key="vis", width=11)
        github.add_column("Lang", key="lang")
        github.loading = True

        local.focus()
        self.load_data()

    @work(thread=True)
    def load_data(self) -> None:
        local_projects = scan_local_projects()
        github_repos = fetch_github_repos()
        self.call_from_thread(self.populate, local_projects, github_repos)

    def populate(self, local_projects: list[dict], github_repos: list[dict]) -> None:
        github_owner_names = {r["nameWithOwner"] for r in github_repos}
        local_owner_names = {p["github"] for p in local_projects if p["github"]}

        local_table = self.query_one("#local", DataTable)
        local_table.clear()
        for proj in local_projects:
            synced = proj["github"] in github_owner_names
            local_table.add_row(
                local_sync_cell(synced),
                name_cell(proj["name"]),
                remote_cell(proj["github"]),
            )
        local_table.loading = False

        github_table = self.query_one("#github", DataTable)
        github_table.clear()
        for repo in github_repos:
            synced = repo["nameWithOwner"] in local_owner_names
            lang = (repo.get("primaryLanguage") or {}).get("name") or "-"
            github_table.add_row(
                github_sync_cell(synced),
                name_cell(repo["name"]),
                vis_cell(repo["visibility"]),
                lang_cell(lang),
            )
        github_table.loading = False

        synced_count = sum(
            1 for p in local_projects if p["github"] in github_owner_names
        )
        self.sub_title = (
            f"{len(local_projects)} local · {len(github_repos)} on github · "
            f"{synced_count} synced"
        )

    def action_switch_pane(self) -> None:
        tables = list(self.query(DataTable))
        if not tables:
            return
        focused = self.focused
        if focused in tables:
            idx = tables.index(focused)
            tables[(idx + 1) % len(tables)].focus()
        else:
            tables[0].focus()

    def action_refresh(self) -> None:
        self.sub_title = "refreshing…"
        for table in self.query(DataTable):
            table.clear()
            table.loading = True
        self.load_data()


def main() -> None:
    preflight()
    ProjectsApp().run()


if __name__ == "__main__":
    main()
