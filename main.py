"""Textual TUI for managing projects: local-primary, GitHub on toggle.

A "local project" is any direct subdir of ~/Projects/ that contains
a .git directory. A local project is considered "synced" with GitHub
when its origin remote points at a github.com URL whose owner/name
matches one of the user's gh repos.

Theming follows the active Omarchy theme (read via theme.py) and
re-applies live within ~2 seconds when the user runs `omarchy theme set`.
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
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Rule, Static

from theme import (
    AppColors,
    FALLBACK_NAME,
    FALLBACK_PALETTE,
    THEME_NAME_FILE,
    palette_to_app_colors,
    palette_to_textual_theme,
    read_omarchy,
)

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

ASCII_TITLE = [
    " ██████╗ ██████╗ ██╗   ██╗███████╗ ████████╗██╗   ██╗██╗",
    "██╔═══██╗██╔══██╗██║   ██║██╔════╝ ╚══██╔══╝██║   ██║██║",
    "██║   ██║██████╔╝██║   ██║███████╗    ██║   ██║   ██║██║",
    "██║   ██║██╔═══╝ ██║   ██║╚════██║    ██║   ██║   ██║██║",
    "╚██████╔╝██║     ╚██████╔╝███████║    ██║   ╚██████╔╝██║",
    " ╚═════╝ ╚═╝      ╚═════╝ ╚══════╝    ╚═╝    ╚═════╝ ╚═╝",
]

# Intentionally NOT themed: GitHub linguist colors are semantic and widely
# recognized (Python yellow everywhere, Rust orange everywhere). Tinting
# these with the active Omarchy palette would lose that recognizability.
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


class Banner(Vertical):
    """Top banner: ASCII title with gradient, tagline + stats, heavy rule."""

    DEFAULT_CSS = """
    Banner {
        height: 10;
        padding: 1 2 0 2;
        background: $background;
    }
    Banner > #banner-ascii {
        height: 6;
        background: transparent;
    }
    Banner > #banner-stats {
        height: 1;
        margin-top: 1;
        background: transparent;
    }
    Banner > Rule {
        height: 1;
        margin: 0;
        color: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="banner-ascii")
        yield Static(id="banner-stats")
        yield Rule(line_style="heavy")

    def on_mount(self) -> None:
        self.apply_theme()
        self.show_stats("local", 0, 0, 0)

    def apply_theme(self) -> None:
        """(Re)render the ASCII gradient using the app's current AppColors."""
        colors: AppColors = self.app._app_colors  # type: ignore[attr-defined]
        gradient = colors.banner_gradient
        # 6-line ASCII art; pair lines by gradient stop: [a,a,b,b,c,c]
        line_colors = (gradient[0], gradient[0], gradient[1], gradient[1], gradient[2], gradient[2])
        text = Text()
        for idx, line in enumerate(ASCII_TITLE):
            text.append(line, style=f"bold {line_colors[idx]}")
            if idx < len(ASCII_TITLE) - 1:
                text.append("\n")
        self.query_one("#banner-ascii", Static).update(text)

    def show_stats(self, view: str, local: int, github: int, synced: int) -> None:
        colors: AppColors = self.app._app_colors  # type: ignore[attr-defined]
        view_label = "  Local" if view == "local" else "  GitHub"
        view_color = colors.view_local if view == "local" else colors.view_github
        stats = Text.assemble(
            ("your project switchboard", f"italic {colors.subtle}"),
            ("    ", ""),
            (view_label, f"bold {view_color}"),
            ("  ·  ", colors.muted_rule),
            (str(local), f"bold {colors.text}"),
            (" projects  ·  ", colors.muted_rule),
            (str(github), f"bold {colors.text}"),
            (" on github  ·  ", colors.muted_rule),
            (str(synced), f"bold {colors.glyph_synced}"),
            (" synced", colors.muted_rule),
        )
        self.query_one("#banner-stats", Static).update(stats)


class ProjectsApp(App):
    TITLE = "opus-tui"

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
    }
    DataTable {
        height: 1fr;
        border: blank;
        background: transparent;
        margin: 1 3 0 3;
        scrollbar-size-vertical: 1;
    }
    Footer {
        background: $surface;
        color: $foreground;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("g", "toggle_view", "Switch view"),
        Binding("r", "refresh", "Refresh"),
    ]

    view: reactive[str] = reactive("local", init=False)

    def __init__(self) -> None:
        super().__init__()
        self.local_projects: list[dict] = []
        self.github_repos: list[dict] = []
        # Default colors so widgets mounting before _reload_theme have something
        # to read. _reload_theme overwrites in on_mount.
        self._app_colors: AppColors = palette_to_app_colors(FALLBACK_PALETTE)
        self._omarchy_mtime: float | None = None

    def compose(self) -> ComposeResult:
        yield Banner()
        yield DataTable(
            id="projects",
            cursor_type="row",
            zebra_stripes=True,
            show_header=False,
        )
        yield Footer()

    def on_mount(self) -> None:
        self._reload_theme()
        self._setup_columns_for_view("local")
        table = self.query_one("#projects", DataTable)
        table.loading = True
        table.focus()
        self.set_interval(2.0, self._poll_theme)
        self.load_data()

    # ── theme reload pipeline ──────────────────────────────────────────────

    def _poll_theme(self) -> None:
        try:
            mtime = THEME_NAME_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._omarchy_mtime:
            self._reload_theme()

    def _reload_theme(self) -> None:
        result = read_omarchy()
        name, palette = result if result else (FALLBACK_NAME, FALLBACK_PALETTE)

        try:
            self._omarchy_mtime = THEME_NAME_FILE.stat().st_mtime
        except OSError:
            self._omarchy_mtime = None

        self._app_colors = palette_to_app_colors(palette)
        self.register_theme(palette_to_textual_theme(palette, name))

        if self.theme != name:
            self.theme = name
        else:
            # Same name, palette possibly mutated. Force re-render via toggle.
            self.theme = "textual-dark"
            self.theme = name

        try:
            self.query_one(Banner).apply_theme()
        except Exception:
            pass  # Banner may not be mounted on first call from on_mount

        if self.local_projects or self.github_repos:
            self._render_view()

    # ── cell rendering ─────────────────────────────────────────────────────

    def _name_cell(self, name: str) -> Text:
        return Text(name, style=f"bold {self._app_colors.text}")

    def _remote_cell(self, remote: str | None) -> Text:
        if remote is None:
            return Text("—", style=f"dim {self._app_colors.dim}")
        return Text(remote, style=self._app_colors.subtle)

    def _lang_cell(self, lang: str) -> Text:
        if lang == "-":
            return Text("—", style=f"dim {self._app_colors.dim}")
        color = LANGUAGE_COLORS.get(lang, self._app_colors.lang_fallback)
        return Text(f"● {lang}", style=f"bold {color}")

    def _vis_cell(self, visibility: str) -> Text:
        c = self._app_colors
        if visibility.lower() == "public":
            return Text(" PUBLIC ", style=f"bold {c.badge_text} on {c.badge_public_bg}")
        return Text(" PRIVATE ", style=f"bold {c.badge_text} on {c.badge_private_bg}")

    def _local_sync_cell(self, synced: bool) -> Text:
        if synced:
            return Text("●", style=f"bold {self._app_colors.glyph_synced}")
        return Text("○", style=self._app_colors.glyph_local_only)

    def _github_sync_cell(self, synced: bool) -> Text:
        if synced:
            return Text("●", style=f"bold {self._app_colors.glyph_synced}")
        return Text("☁", style=f"bold {self._app_colors.glyph_github_only}")

    # ── data load + render ─────────────────────────────────────────────────

    def _setup_columns_for_view(self, view: str) -> None:
        table = self.query_one("#projects", DataTable)
        table.clear(columns=True)
        if view == "local":
            table.add_column(" ", key="sync", width=2)
            table.add_column("Name", key="name")
            table.add_column("Remote", key="remote")
        else:
            table.add_column(" ", key="sync", width=2)
            table.add_column("Name", key="name")
            table.add_column("Visibility", key="vis", width=11)
            table.add_column("Language", key="lang")

    @work(thread=True)
    def load_data(self) -> None:
        local_projects = scan_local_projects()
        github_repos = fetch_github_repos()
        self.call_from_thread(self._on_data_loaded, local_projects, github_repos)

    def _on_data_loaded(
        self, local_projects: list[dict], github_repos: list[dict]
    ) -> None:
        self.local_projects = local_projects
        self.github_repos = github_repos
        self._render_view()
        self.query_one("#projects", DataTable).loading = False

    def _render_view(self) -> None:
        github_owner_names = {r["nameWithOwner"] for r in self.github_repos}
        local_owner_names = {p["github"] for p in self.local_projects if p["github"]}
        synced_count = sum(
            1 for p in self.local_projects if p["github"] in github_owner_names
        )

        self.query_one(Banner).show_stats(
            self.view,
            len(self.local_projects),
            len(self.github_repos),
            synced_count,
        )

        self._setup_columns_for_view(self.view)
        table = self.query_one("#projects", DataTable)

        if self.view == "local":
            for proj in self.local_projects:
                synced = proj["github"] in github_owner_names
                table.add_row(
                    self._local_sync_cell(synced),
                    self._name_cell(proj["name"]),
                    self._remote_cell(proj["github"]),
                )
        else:
            for repo in self.github_repos:
                synced = repo["nameWithOwner"] in local_owner_names
                lang = (repo.get("primaryLanguage") or {}).get("name") or "-"
                table.add_row(
                    self._github_sync_cell(synced),
                    self._name_cell(repo["name"]),
                    self._vis_cell(repo["visibility"]),
                    self._lang_cell(lang),
                )

    def watch_view(self, _old: str, _new: str) -> None:
        if self.local_projects or self.github_repos:
            self._render_view()

    def action_toggle_view(self) -> None:
        self.view = "github" if self.view == "local" else "local"

    def action_refresh(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.clear()
        table.loading = True
        self.load_data()


def main() -> None:
    preflight()
    ProjectsApp().run()


if __name__ == "__main__":
    main()
