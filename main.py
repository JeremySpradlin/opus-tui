"""Textual TUI for managing projects: local-primary, GitHub on toggle.

A "local project" is any direct subdir of ~/Projects/ that contains
a .git directory. A local project is considered "synced" with GitHub
when its origin remote points at a github.com URL whose owner/name
matches one of the user's gh repos.

Theming follows the active Omarchy theme (read via theme.py) and
re-applies live within ~2 seconds when the user runs `omarchy theme set`.
"""

import json
import logging
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Rule,
    Static,
)

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
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_KEEP = 20  # keep this many most-recent log files

logger = logging.getLogger(__name__)


def setup_logging() -> Path:
    """Configure file-only logging at DEBUG level.

    One log file per app session, named opus-tui-YYYYMMDD-HHMMSS.log under
    ./logs/. Keeps only the most recent LOG_KEEP files. Returns the path of
    the freshly opened log file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"opus-tui-{datetime.now():%Y%m%d-%H%M%S}.log"
    logging.basicConfig(
        filename=str(log_file),
        filemode="w",
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("markdown_it").setLevel(logging.WARNING)
    _prune_old_logs()
    return log_file


def _prune_old_logs(keep: int = LOG_KEEP) -> None:
    try:
        existing = sorted(LOG_DIR.glob("opus-tui-*.log"), reverse=True)
        for old in existing[keep:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

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
    " ██████╗  ██████╗  ██╗   ██╗ ███████╗",
    "██╔═══██╗ ██╔══██╗ ██║   ██║ ██╔════╝",
    "██║   ██║ ██║  ██║ ██║   ██║ ██║     ",
    "██║   ██║ ██████╔╝ ██║   ██║ ███████╗",
    "██║   ██║ ██╔═══╝  ██║   ██║ ╚════██║",
    "██║   ██║ ██║      ██║   ██║ ╚════██║",
    "╚██████╔╝ ██║      ╚██████╔╝ ███████║",
    " ╚═════╝  ╚═╝       ╚═════╝  ╚══════╝",
]

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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


class Banner(Vertical):
    """Top banner: ASCII title with gradient, tagline + stats, heavy rule."""

    DEFAULT_CSS = """
    Banner {
        height: 12;
        padding: 1 2 0 2;
        background: $background;
    }
    Banner > #banner-ascii {
        height: 8;
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

    _last_stats: tuple[str, int, int, int] | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="banner-ascii")
        yield Static(id="banner-stats")
        yield Rule(line_style="heavy")

    def on_mount(self) -> None:
        self.apply_theme()
        self.show_stats("local", 0, 0, 0)

    def _content_width(self) -> int:
        """Inner width available to banner content (Banner width minus padding)."""
        return self.size.width - 4 if self.size.width else 80

    def apply_theme(self) -> None:
        """(Re)render the ASCII gradient using the app's current AppColors."""
        colors: AppColors = self.app._app_colors  # type: ignore[attr-defined]
        g = colors.banner_gradient
        # 8-line ASCII art; gradient distributed 2/4/2 (symmetric).
        line_colors = (g[0], g[0], g[1], g[1], g[1], g[1], g[2], g[2])

        art_width = max(len(line) for line in ASCII_TITLE)
        pad = " " * max(0, (self._content_width() - art_width) // 2)

        text = Text()
        for idx, line in enumerate(ASCII_TITLE):
            text.append(pad + line, style=f"bold {line_colors[idx]}")
            if idx < len(ASCII_TITLE) - 1:
                text.append("\n")
        self.query_one("#banner-ascii", Static).update(text)

    def on_resize(self) -> None:
        """Re-center on terminal resize."""
        self.apply_theme()
        if self._last_stats is not None:
            self.show_stats(*self._last_stats)

    def show_stats(self, view: str, local: int, github: int, synced: int) -> None:
        self._last_stats = (view, local, github, synced)
        colors: AppColors = self.app._app_colors  # type: ignore[attr-defined]
        view_label = "  Local" if view == "local" else "  GitHub"
        view_color = colors.view_local if view == "local" else colors.view_github

        creating: str | None = getattr(self.app, "_creating_name", None)
        creating_suffix = ""
        if creating:
            spinner = SPINNER_FRAMES[
                getattr(self.app, "_spinner_frame", 0) % len(SPINNER_FRAMES)
            ]
            creating_suffix = f"    {spinner} creating {creating}…"

        visible = (
            f"your project switchboard    {view_label}  ·  "
            f"{local} projects  ·  {github} on github  ·  {synced} linked"
            f"{creating_suffix}"
        )
        pad = " " * max(0, (self._content_width() - len(visible)) // 2)

        stats = Text.assemble(
            (pad, ""),
            ("your project switchboard", f"italic {colors.subtle}"),
            ("    ", ""),
            (view_label, f"bold {view_color}"),
            ("  ·  ", colors.muted_rule),
            (str(local), f"bold {colors.text}"),
            (" projects  ·  ", colors.muted_rule),
            (str(github), f"bold {colors.text}"),
            (" on github  ·  ", colors.muted_rule),
            (str(synced), f"bold {colors.glyph_synced}"),
            (" linked", colors.muted_rule),
        )
        if creating:
            spinner = SPINNER_FRAMES[
                getattr(self.app, "_spinner_frame", 0) % len(SPINNER_FRAMES)
            ]
            stats.append(f"    {spinner} ", style=f"bold {colors.view_local}")
            stats.append(f"creating {creating}…", style=f"italic {colors.subtle}")
        self.query_one("#banner-stats", Static).update(stats)


class DetailPanel(Static):
    """Right-side panel showing details of the focused project."""

    DEFAULT_CSS = """
    DetailPanel {
        width: 1fr;
        height: 1fr;
        padding: 1 3;
        background: transparent;
        border-left: vkey $surface;
    }
    """


class NewProjectModal(ModalScreen[dict | None]):
    """Modal form for creating a new project (~/Projects + GitHub repo)."""

    DEFAULT_CSS = """
    NewProjectModal {
        align: center middle;
    }
    NewProjectModal > Vertical {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $background;
        border: heavy $primary;
        border-title-align: left;
        border-title-color: $primary;
        border-title-style: bold;
    }
    NewProjectModal Label {
        margin-top: 1;
        color: $foreground;
    }
    NewProjectModal Label.first {
        margin-top: 0;
    }
    NewProjectModal Input {
        margin: 0;
    }
    NewProjectModal RadioSet {
        margin: 0;
        padding: 0 1;
        height: auto;
        background: transparent;
        border: blank;
    }
    NewProjectModal Horizontal#actions {
        align: right middle;
        height: 3;
        margin-top: 1;
    }
    NewProjectModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Name", classes="first")
            yield Input(placeholder="my-new-project", id="name-input")
            yield Label("Visibility")
            with RadioSet(id="visibility"):
                yield RadioButton("public")
                yield RadioButton("private", value=True)
            yield Label("Description (optional)")
            yield Input(placeholder="What's this project for?", id="desc-input")
            with Horizontal(id="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Create", variant="primary", id="submit")

    def on_mount(self) -> None:
        self.query_one("#dialog", Vertical).border_title = " New project "
        self.query_one("#name-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _on_submit(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def _on_input_submit(self) -> None:
        self._submit()

    def _submit(self) -> None:
        name = self.query_one("#name-input", Input).value.strip()
        if not name:
            self.notify("Name is required", severity="warning")
            self.query_one("#name-input", Input).focus()
            return
        if "/" in name or " " in name:
            self.notify(
                "Name can't contain slashes or spaces", severity="warning"
            )
            return
        target = PROJECTS_DIR / name
        if target.exists():
            self.notify(
                f"~/Projects/{name} already exists", severity="warning"
            )
            return

        radioset = self.query_one("#visibility", RadioSet)
        is_private = True
        if radioset.pressed_button is not None:
            is_private = "private" in str(radioset.pressed_button.label).lower()

        self.dismiss({
            "name": name,
            "private": is_private,
            "description": self.query_one("#desc-input", Input).value.strip(),
        })


class DeleteProjectModal(ModalScreen[dict | None]):
    """Confirm dialog for deleting a project (local-only or local + GitHub)."""

    DEFAULT_CSS = """
    DeleteProjectModal {
        align: center middle;
    }
    DeleteProjectModal > Vertical {
        width: 64;
        height: auto;
        padding: 1 2;
        background: $background;
        border: heavy $error;
        border-title-align: left;
        border-title-color: $error;
        border-title-style: bold;
    }
    DeleteProjectModal Label {
        margin-top: 1;
        color: $foreground;
    }
    DeleteProjectModal Label.first {
        margin-top: 0;
    }
    DeleteProjectModal Label.path {
        color: $text-muted;
        margin-top: 0;
    }
    DeleteProjectModal Label.warning {
        color: $warning;
        margin-top: 1;
    }
    DeleteProjectModal RadioSet {
        margin: 0;
        padding: 0 1;
        height: auto;
        background: transparent;
        border: blank;
    }
    DeleteProjectModal Horizontal#actions {
        align: right middle;
        height: 3;
        margin-top: 1;
    }
    DeleteProjectModal Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, name: str, path: Path, owner_name: str | None) -> None:
        super().__init__()
        self._name = name
        self._path = path
        self._owner_name = owner_name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._name, classes="first")
            yield Label(str(self._path).replace(str(Path.home()), "~", 1),
                        classes="path")
            if self._owner_name:
                yield Label(self._owner_name, classes="path")
                yield Label("Scope")
                with RadioSet(id="scope"):
                    yield RadioButton("Local only", value=True, id="local-only")
                    yield RadioButton("Local + GitHub", id="local-github")
            yield Label("This cannot be undone.", classes="warning")
            with Horizontal(id="actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", variant="error", id="submit")

    def on_mount(self) -> None:
        self.query_one("#dialog", Vertical).border_title = " Delete project "
        # Focus the scope radio (or Cancel if there's no scope choice). Either
        # way, Enter on the focused widget is a no-op — neither RadioSet nor
        # Cancel triggers delete — so we keep the "Enter won't delete" safety
        # while removing a tab for the user who actually wants to change scope.
        if self._owner_name:
            self.query_one("#scope", RadioSet).focus()
        else:
            self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#submit")
    def _on_submit(self) -> None:
        include_github = False
        if self._owner_name:
            radioset = self.query_one("#scope", RadioSet)
            pressed = radioset.pressed_button
            include_github = pressed is not None and pressed.id == "local-github"
        self.dismiss({"include_github": include_github})


class ProjectsApp(App):
    TITLE = "opus-tui"

    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: $background;
    }
    #main {
        height: 1fr;
    }
    OptionList {
        width: 1fr;
        height: 1fr;
        background: transparent;
        border: blank;
        padding: 0 2;
        scrollbar-size-vertical: 1;
    }
    OptionList:focus {
        border: blank;
    }
    OptionList > .option-list--option-highlighted {
        background: $primary 20%;
        text-style: bold;
    }
    OptionList:focus > .option-list--option-highlighted {
        background: $primary 35%;
        text-style: bold;
    }
    Footer {
        background: $surface;
        color: $foreground;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("g", "toggle_view", "Switch view"),
        Binding("c", "clone", "Clone"),
        Binding("n", "new_project", "New"),
        Binding("d", "delete_project", "Delete"),
        Binding("r", "refresh", "Refresh"),
    ]

    view: reactive[str] = reactive("local", init=False)

    def __init__(self) -> None:
        super().__init__()
        self.local_projects: list[dict] = []
        self.github_repos: list[dict] = []
        self._app_colors: AppColors = palette_to_app_colors(FALLBACK_PALETTE)
        self._omarchy_mtime: float | None = None
        self._github_set: set[str] = set()
        self._local_set: set[str] = set()
        self._last_highlighted: int | None = None
        self._github_loaded: bool = False
        # Clone-in-progress state. _cloning_repo_index points into github_repos.
        self._cloning_repo_index: int | None = None
        self._creating_name: str | None = None
        self._creating_was_private: bool = True
        self._spinner_frame: int = 0
        self._spinner_timer = None  # set_interval handle

    def compose(self) -> ComposeResult:
        yield Banner()
        with Horizontal(id="main"):
            yield OptionList(id="projects")
            yield DetailPanel(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        logger.info("ProjectsApp.on_mount")
        self._reload_theme()
        plist = self.query_one(OptionList)
        plist.loading = True
        plist.focus()
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
        logger.info("reload_theme: name=%s (omarchy=%s)", name, result is not None)

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
            self._populate_list()

    # ── cell rendering helpers (used by list rows + detail panel) ──────────

    def _local_sync_cell(self, synced: bool) -> Text:
        if synced:
            return Text("●", style=f"bold {self._app_colors.glyph_synced}")
        return Text("○", style=self._app_colors.glyph_local_only)

    def _github_sync_cell(self, synced: bool) -> Text:
        if synced:
            return Text("●", style=f"bold {self._app_colors.glyph_synced}")
        return Text("☁", style=f"bold {self._app_colors.glyph_github_only}")

    def _vis_cell(self, visibility: str) -> Text:
        c = self._app_colors
        if visibility.lower() == "public":
            return Text(" PUBLIC ", style=f"bold {c.badge_text} on {c.badge_public_bg}")
        return Text(" PRIVATE ", style=f"bold {c.badge_text} on {c.badge_private_bg}")

    def _lang_cell(self, lang: str) -> Text:
        if lang == "-":
            return Text("—", style=f"dim {self._app_colors.dim}")
        color = LANGUAGE_COLORS.get(lang, self._app_colors.lang_fallback)
        return Text(f"● {lang}", style=f"bold {color}")

    # ── list rendering ─────────────────────────────────────────────────────

    def _list_content_width(self) -> int:
        """Width of the OptionList's content area (after CSS padding 0 2)."""
        try:
            plist = self.query_one(OptionList)
            return max(0, (plist.size.width or 0) - 4)
        except Exception:
            return 40

    def _list_row(self, item: dict, view: str, focused: bool) -> Text:
        if view == "local":
            synced = item.get("github") in self._github_set
            glyph = self._local_sync_cell(synced)
        else:
            synced = item["nameWithOwner"] in self._local_set
            glyph = self._github_sync_cell(synced)

        arrow_color = (
            self._app_colors.view_local
            if view == "local"
            else self._app_colors.view_github
        )
        arrow = Text("▸ " if focused else "  ", style=f"bold {arrow_color}")
        content = Text.assemble(
            arrow,
            glyph,
            "  ",
            Text(item["name"], style=f"bold {self._app_colors.text}"),
        )

        # Right-align: pad the start so content drifts toward the central divider.
        pad = max(0, self._list_content_width() - content.cell_len - 1)
        return Text.assemble(Text(" " * pad), content)

    def _cloning_row(self, repo: dict, focused: bool) -> Text:
        """Row prompt for a repo currently being cloned (animated spinner)."""
        c = self._app_colors
        spinner = SPINNER_FRAMES[self._spinner_frame]
        arrow = Text("▸ " if focused else "  ", style=f"bold {c.view_github}")
        glyph = Text(spinner, style=f"bold {c.glyph_github_only}")
        content = Text.assemble(
            arrow,
            glyph,
            "  ",
            Text(repo["name"], style=f"bold {c.text}"),
        )
        pad = max(0, self._list_content_width() - content.cell_len - 1)
        return Text.assemble(Text(" " * pad), content)

    def _row_for_index(self, idx: int, focused: bool) -> Text:
        """Render any list row, special-casing the cloning one if applicable."""
        items = self.local_projects if self.view == "local" else self.github_repos
        if not (0 <= idx < len(items)):
            return Text()
        if self.view == "github" and idx == self._cloning_repo_index:
            return self._cloning_row(items[idx], focused)
        return self._list_row(items[idx], self.view, focused)

    def _update_banner_stats(self) -> None:
        """Recompute derived sets and refresh the banner's stats line."""
        self._github_set = {r["nameWithOwner"] for r in self.github_repos}
        self._local_set = {p["github"] for p in self.local_projects if p["github"]}
        synced_count = sum(
            1 for p in self.local_projects if p.get("github") in self._github_set
        )
        self.query_one(Banner).show_stats(
            self.view,
            len(self.local_projects),
            len(self.github_repos),
            synced_count,
        )

    def _populate_list(self) -> None:
        self._update_banner_stats()

        plist = self.query_one(OptionList)

        # GitHub view requested but the gh fetch hasn't returned yet.
        if self.view == "github" and not self._github_loaded:
            plist.clear_options()
            plist.loading = True
            self._last_highlighted = None
            self._show_loading_detail()
            return

        plist.loading = False
        plist.clear_options()
        self._last_highlighted = None

        items = self.local_projects if self.view == "local" else self.github_repos
        if not items:
            self._show_empty_detail()
            return

        for item in items:
            plist.add_option(self._list_row(item, self.view, focused=False))

        # Explicit first-row selection. OptionList auto-highlights index 0 on
        # first add and fires OptionHighlighted, but timing is racy with the
        # post-load layout pass — better to set state ourselves and let the
        # deferred refresh below do the actual prompt re-render at the
        # now-known content width.
        self._last_highlighted = 0
        plist.highlighted = 0  # Textual's selection state (Enter will trigger OptionSelected)
        plist.focus()          # loading-state transition can drop focus; re-assert
        self._update_detail_for_index(0)
        self.call_after_refresh(self._refresh_list_rows)

    def _refresh_list_rows(self) -> None:
        """Re-render every list row at current OptionList width."""
        items = self.local_projects if self.view == "local" else self.github_repos
        if not items:
            return
        try:
            plist = self.query_one(OptionList)
        except Exception:
            return
        for idx in range(len(items)):
            focused = idx == self._last_highlighted
            try:
                plist.replace_option_prompt_at_index(
                    idx, self._row_for_index(idx, focused)
                )
            except Exception:
                pass

    @on(OptionList.OptionHighlighted)
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        plist = event.option_list
        idx = event.option_index

        # Remove arrow from previously focused row.
        if self._last_highlighted is not None and self._last_highlighted != idx:
            try:
                plist.replace_option_prompt_at_index(
                    self._last_highlighted,
                    self._row_for_index(self._last_highlighted, focused=False),
                )
            except IndexError:
                pass

        # Apply arrow to newly focused row.
        try:
            plist.replace_option_prompt_at_index(idx, self._row_for_index(idx, focused=True))
        except IndexError:
            pass

        self._last_highlighted = idx
        self._update_detail_for_index(idx)

    # ── detail panel ───────────────────────────────────────────────────────

    def _update_detail_for_index(self, idx: int) -> None:
        items = self.local_projects if self.view == "local" else self.github_repos
        if not (0 <= idx < len(items)):
            self._show_empty_detail()
            return
        if self.view == "local":
            self._show_local_detail(items[idx])
        else:
            self._show_github_detail(items[idx])

    def _show_empty_detail(self) -> None:
        c = self._app_colors
        self.query_one(DetailPanel).update(
            Text("(no projects)", style=f"dim {c.dim}")
        )

    def _show_loading_detail(self) -> None:
        c = self._app_colors
        self.query_one(DetailPanel).update(
            Text("loading github repos…", style=f"italic {c.subtle}")
        )

    def _ensure_git_status(self, project: dict) -> None:
        """Lazily compute and cache git status info on the project dict."""
        if "_status" in project:
            return
        project["_status"] = git_status_summary(project["path"])

    def _show_local_detail(self, project: dict) -> None:
        self._ensure_git_status(project)
        c = self._app_colors
        path = str(project["path"])
        home = str(Path.home())
        if path.startswith(home):
            path = "~" + path[len(home):]

        github = project.get("github") or "—"
        branch = project.get("branch") or "?"

        # changes line
        status = project.get("_status")
        if status is None:
            changes_text = Text("?", style=c.muted_rule)
        elif status["dirty"] == 0:
            changes_text = Text("clean", style=c.muted_rule)
        else:
            n = status["dirty"]
            changes_text = Text(
                f"{n} {'change' if n == 1 else 'changes'}",
                style=f"bold {c.text}",
            )

        # remote line (ahead/behind upstream)
        if status is None or status["ahead"] is None:
            remote_text = Text("no upstream", style=c.muted_rule)
        else:
            ahead, behind = status["ahead"], status["behind"]
            if ahead == 0 and behind == 0:
                remote_text = Text("up to date", style=c.muted_rule)
            else:
                parts = []
                if ahead > 0:
                    parts.append(f"↑{ahead}")
                if behind > 0:
                    parts.append(f"↓{behind}")
                remote_text = Text(" ".join(parts), style=f"bold {c.text}")

        body = Text()
        body.append(project["name"], style=f"bold {c.text}")
        body.append("\n")
        body.append(path, style=f"italic {c.subtle}")
        body.append("\n\n")
        body.append_text(self._kv("github", Text(github, style=c.subtle)))
        body.append("\n")
        body.append_text(self._kv("branch", Text(branch, style=c.subtle)))
        body.append("\n")
        body.append_text(self._kv("changes", changes_text))
        body.append("\n")
        body.append_text(self._kv("remote", remote_text))
        self.query_one(DetailPanel).update(body)

    def _show_github_detail(self, repo: dict) -> None:
        c = self._app_colors
        synced = repo["nameWithOwner"] in self._local_set
        lang = (repo.get("primaryLanguage") or {}).get("name") or "-"

        is_cloning = (
            self._cloning_repo_index is not None
            and 0 <= self._cloning_repo_index < len(self.github_repos)
            and self.github_repos[self._cloning_repo_index] is repo
        )

        if is_cloning:
            spinner = SPINNER_FRAMES[self._spinner_frame]
            target = str(PROJECTS_DIR / repo["name"])
            home = str(Path.home())
            if target.startswith(home):
                target = "~" + target[len(home):]
            status_text = Text.assemble(
                (spinner + " ", f"bold {c.glyph_github_only}"),
                ("cloning to ", c.subtle),
                (target, f"italic {c.text}"),
            )
        elif synced:
            status_text = Text("cloned locally", style=f"bold {c.glyph_synced}")
        else:
            status_text = Text("not cloned", style=f"bold {c.glyph_github_only}")

        body = Text()
        body.append(repo["name"], style=f"bold {c.text}")
        body.append("\n")
        body.append(repo["nameWithOwner"], style=f"italic {c.subtle}")
        body.append("\n\n")
        body.append_text(self._kv("visibility", self._vis_cell(repo["visibility"])))
        body.append("\n")
        body.append_text(self._kv("language", self._lang_cell(lang)))
        body.append("\n")
        body.append_text(self._kv("status", status_text))
        self.query_one(DetailPanel).update(body)

    def _kv(self, key: str, value: Text) -> Text:
        c = self._app_colors
        return Text.assemble(
            (f"  {key:>10}  ", f"{c.dim}"),
            value,
        )

    # ── data load ──────────────────────────────────────────────────────────

    @work(thread=True)
    def load_data(self) -> None:
        # Local-first split: post local results as soon as the scan returns
        # so the list appears in ~200ms, then post github when the slower
        # network call completes.
        local_projects = scan_local_projects()
        self.call_from_thread(self._on_local_loaded, local_projects)
        github_repos = fetch_github_repos()
        self.call_from_thread(self._on_github_loaded, github_repos)

    def _on_local_loaded(self, local_projects: list[dict]) -> None:
        self.local_projects = local_projects
        if self.view == "local":
            self.query_one(OptionList).loading = False
            self._populate_list()
        else:
            # User toggled to github view before local arrived. Just refresh
            # banner stats; populate logic owns the github loading state.
            self._update_banner_stats()

    def _on_github_loaded(self, github_repos: list[dict]) -> None:
        self.github_repos = github_repos
        self._github_loaded = True

        if self.view == "github":
            self.query_one(OptionList).loading = False
            self._populate_list()
            return

        # Local view: update sync glyphs (○ → ●) in place so the user's
        # cursor and scroll position are preserved.
        self._update_banner_stats()
        self._refresh_list_rows()
        if self._last_highlighted is not None:
            self._update_detail_for_index(self._last_highlighted)

    # ── reactive watchers + actions ────────────────────────────────────────

    def watch_view(self, _old: str, _new: str) -> None:
        if self.local_projects or self.github_repos:
            self._populate_list()

    def action_toggle_view(self) -> None:
        new_view = "github" if self.view == "local" else "local"
        logger.info("toggle view: %s → %s", self.view, new_view)
        self.view = new_view

    def action_refresh(self) -> None:
        logger.info("refresh requested")
        plist = self.query_one(OptionList)
        plist.clear_options()
        plist.loading = True
        self._github_loaded = False
        self.load_data()

    # ── clone flow ─────────────────────────────────────────────────────────

    def action_clone(self) -> None:
        if self.view != "github":
            self.notify("Switch to GitHub view (g) first", severity="information")
            return
        if self._cloning_repo_index is not None:
            self.notify("A clone is already in progress", severity="warning")
            return
        if self._last_highlighted is None or not self.github_repos:
            return
        idx = self._last_highlighted
        if not (0 <= idx < len(self.github_repos)):
            return
        repo = self.github_repos[idx]
        owner_name = repo["nameWithOwner"]

        # Already cloned: smart-jump to the local project instead.
        if owner_name in self._local_set:
            self._jump_to_local_for_remote(owner_name)
            return

        target = PROJECTS_DIR / repo["name"]
        if target.exists():
            self.notify(
                f"~/Projects/{repo['name']} already exists; refusing to overwrite",
                severity="warning",
                timeout=5,
            )
            return

        self._cloning_repo_index = idx
        self._start_spinner()
        # Initial render of the cloning state for both row and detail.
        try:
            plist = self.query_one(OptionList)
            plist.replace_option_prompt_at_index(
                idx, self._cloning_row(repo, idx == self._last_highlighted)
            )
        except Exception:
            pass
        if self._last_highlighted == idx:
            self._show_github_detail(repo)
        self.clone_repo(owner_name, str(target), idx)

    @work(thread=True)
    def clone_repo(self, owner_name: str, target: str, idx: int) -> None:
        cmd = ["gh", "repo", "clone", owner_name, target]
        logger.info("clone start: %s → %s", owner_name, target)
        logger.debug("$ %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        logger.info(
            "clone done: rc=%d owner_name=%s",
            result.returncode, owner_name,
        )
        if result.returncode != 0:
            logger.warning(
                "clone stderr: %s", (result.stderr or "").strip()[:300]
            )
        self.call_from_thread(
            self._on_clone_done,
            owner_name,
            target,
            idx,
            result.returncode == 0,
            (result.stderr or result.stdout).strip(),
        )

    def _on_clone_done(
        self, owner_name: str, target: str, idx: int, success: bool, err: str
    ) -> None:
        self._stop_spinner()
        self._cloning_repo_index = None

        if success:
            self.notify(f"Cloned {owner_name}", severity="information", timeout=3)
            self._refresh_after_clone(target)
            return

        # Failure: restore the row + detail to their pre-clone state.
        try:
            plist = self.query_one(OptionList)
            if 0 <= idx < len(self.github_repos):
                focused = idx == self._last_highlighted
                plist.replace_option_prompt_at_index(
                    idx, self._list_row(self.github_repos[idx], "github", focused)
                )
        except Exception:
            pass
        if self._last_highlighted is not None:
            self._update_detail_for_index(self._last_highlighted)
        self.notify(f"Clone failed: {err[:120]}", severity="error", timeout=6)

    @work(thread=True)
    def _refresh_after_clone(self, target: str) -> None:
        local_projects = scan_local_projects()
        self.call_from_thread(self._post_clone_local_loaded, local_projects, target)

    def _post_clone_local_loaded(
        self, local_projects: list[dict], target: str
    ) -> None:
        target_path = Path(target)
        new_project = next(
            (p for p in local_projects if p["path"] == target_path), None
        )

        # If this scan was triggered by a CREATE (not a clone), our
        # github_repos cache is stale — the new repo on github.com isn't in
        # it. Synthesize an entry now so the sync glyph reflects reality;
        # background github refetch (below) replaces it with real metadata.
        was_creating = self._creating_name is not None
        if was_creating and new_project is not None:
            self._synthesize_github_entry(new_project)

        # Clear creation state. Spinner stops if no clone is also running.
        self._creating_name = None
        self._stop_spinner()

        self.local_projects = local_projects

        # Setting view to "local" triggers watch_view → _populate_list, but
        # reactive watchers don't fire on no-op assignments. If we're already
        # in local view (common for the new-project path), we have to populate
        # explicitly.
        if self.view != "local":
            self.view = "local"
        else:
            self._populate_list()

        new_idx = next(
            (i for i, p in enumerate(local_projects) if p["path"] == target_path),
            None,
        )
        if new_idx is not None and new_idx > 0:
            def _select_new() -> None:
                try:
                    plist = self.query_one(OptionList)
                    if 0 <= new_idx < plist.option_count:
                        plist.highlighted = new_idx
                except Exception:
                    pass
            self.call_after_refresh(_select_new)

        # Background-refetch github after create so metadata catches up to
        # the synthesized entry. Skipped for clone — github state didn't
        # change.
        if was_creating:
            self._refetch_github()

    def _synthesize_github_entry(self, local_project: dict) -> None:
        """Append a placeholder github_repos entry for a freshly-created repo."""
        owner_name = local_project.get("github")
        if not owner_name:
            return
        if any(r["nameWithOwner"] == owner_name for r in self.github_repos):
            return
        self.github_repos.append({
            "name": local_project["name"],
            "nameWithOwner": owner_name,
            "primaryLanguage": None,
            "visibility": "private" if self._creating_was_private else "public",
        })

    @work(thread=True, exclusive=True, group="github_refetch")
    def _refetch_github(self) -> None:
        github_repos = fetch_github_repos()
        # Reuse _on_github_loaded — it already does the right in-place
        # refresh in local view (preserves cursor) and full repopulate in
        # github view.
        self.call_from_thread(self._on_github_loaded, github_repos)

    def _jump_to_local_for_remote(self, owner_name: str) -> None:
        new_idx = next(
            (i for i, p in enumerate(self.local_projects)
             if p.get("github") == owner_name),
            None,
        )
        if new_idx is None:
            self.notify(
                f"Couldn't locate {owner_name} in local list",
                severity="warning",
            )
            return
        self.view = "local"
        if new_idx > 0:
            def _select() -> None:
                try:
                    plist = self.query_one(OptionList)
                    if 0 <= new_idx < plist.option_count:
                        plist.highlighted = new_idx
                except Exception:
                    pass
            self.call_after_refresh(_select)

    # ── new project ────────────────────────────────────────────────────────

    def action_new_project(self) -> None:
        if self._cloning_repo_index is not None:
            self.notify(
                "Wait for the current clone to finish first",
                severity="warning",
            )
            return

        def handle_result(result: dict | None) -> None:
            if result is None:
                return
            self._creating_name = result["name"]
            self._creating_was_private = result["private"]
            self._start_spinner()
            self._update_banner_stats()  # immediate first paint of "creating …"
            self._create_project(
                result["name"], result["private"], result["description"]
            )

        self.push_screen(NewProjectModal(), handle_result)

    @work(thread=True)
    def _create_project(
        self, name: str, is_private: bool, description: str
    ) -> None:
        target = PROJECTS_DIR / name
        logger.info(
            "create_project start: name=%s private=%s target=%s",
            name, is_private, target,
        )

        # mkdir
        try:
            target.mkdir(parents=True)
        except OSError as e:
            logger.error("mkdir failed: %s", e)
            self.call_from_thread(
                self.notify,
                f"Couldn't create {target}: {e}",
                severity="error",
                timeout=6,
            )
            return

        def fail(msg: str) -> None:
            logger.error("create_project fail: %s", msg)
            self.call_from_thread(
                self.notify, msg, severity="error", timeout=6
            )

        # git init
        cmd = ["git", "-C", str(target), "init", "-b", "main"]
        logger.debug("$ %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        logger.debug(
            "git init: rc=%d stdout=%r stderr=%r",
            r.returncode, r.stdout.strip()[:200], r.stderr.strip()[:200],
        )
        if r.returncode != 0:
            fail(f"git init failed: {r.stderr.strip()[:120]}")
            return

        # README + initial commit so we have something to push
        readme = target / "README.md"
        readme.write_text(
            f"# {name}\n\n{description}\n" if description else f"# {name}\n"
        )

        cmd = ["git", "-C", str(target), "add", "."]
        logger.debug("$ %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        logger.debug(
            "git add: rc=%d stderr=%r",
            r.returncode, r.stderr.strip()[:200],
        )
        if r.returncode != 0:
            fail(f"git add failed: {r.stderr.strip()[:120]}")
            return

        cmd = ["git", "-C", str(target), "commit", "-m", "initial commit"]
        logger.debug("$ %s", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        logger.debug(
            "git commit: rc=%d stdout=%r stderr=%r",
            r.returncode, r.stdout.strip()[:200], r.stderr.strip()[:200],
        )
        if r.returncode != 0:
            fail(f"git commit failed: {r.stderr.strip()[:120]}")
            return

        # gh repo create + push
        gh_args = [
            "gh", "repo", "create", name,
            "--source", str(target),
            "--push",
            "--private" if is_private else "--public",
        ]
        if description:
            gh_args.extend(["--description", description])
        logger.debug("$ %s", " ".join(gh_args))
        r = subprocess.run(gh_args, capture_output=True, text=True)
        logger.info(
            "gh repo create: rc=%d stdout=%r stderr=%r",
            r.returncode, r.stdout.strip()[:300], r.stderr.strip()[:300],
        )
        if r.returncode != 0:
            self.call_from_thread(
                self.notify,
                f"GitHub create failed (local repo intact): "
                f"{r.stderr.strip()[:120]}",
                severity="warning",
                timeout=8,
            )
        else:
            self.call_from_thread(
                self.notify,
                f"Created {name}",
                severity="information",
                timeout=3,
            )

        # Either way, refresh local list and jump to the new project.
        self.call_from_thread(self._refresh_after_clone, str(target))

    # ── delete project ─────────────────────────────────────────────────────

    def action_delete_project(self) -> None:
        if self.view != "local":
            self.notify(
                "Switch to local view (g) to delete a project",
                severity="information",
            )
            return
        if self._cloning_repo_index is not None or self._creating_name is not None:
            self.notify(
                "Wait for the current operation to finish",
                severity="warning",
            )
            return
        if self._last_highlighted is None or not self.local_projects:
            return
        idx = self._last_highlighted
        if not (0 <= idx < len(self.local_projects)):
            return
        project = self.local_projects[idx]
        name = project["name"]
        path = project["path"]
        owner_name = project.get("github")

        def handle_result(result: dict | None) -> None:
            if result is None:
                return
            include_github = result["include_github"]
            if include_github and not self._has_delete_scope():
                self.notify(
                    "Need delete_repo scope. Run:\n"
                    "gh auth refresh -h github.com -s delete_repo",
                    severity="warning",
                    timeout=10,
                )
                return
            self.notify(f"Deleting {name}…", severity="information", timeout=2)
            self._delete_project(name, path, owner_name, include_github)

        self.push_screen(
            DeleteProjectModal(name, path, owner_name),
            handle_result,
        )

    def _has_delete_scope(self) -> bool:
        """Quick check: does gh auth include the delete_repo scope?"""
        try:
            r = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("delete_scope check failed: %s", e)
            return False
        # gh auth status prints the granted scopes on a "Token scopes:" line
        return "delete_repo" in (r.stdout + r.stderr)

    @work(thread=True)
    def _delete_project(
        self, name: str, path: Path, owner_name: str | None, include_github: bool,
    ) -> None:
        logger.info(
            "delete_project start: name=%s path=%s github=%s include_github=%s",
            name, path, owner_name, include_github,
        )

        # Local rmtree
        try:
            shutil.rmtree(path)
            logger.info("local rmtree ok: %s", path)
        except OSError as e:
            logger.error("rmtree failed: %s", e)
            self.call_from_thread(
                self.notify,
                f"Couldn't delete {path}: {e}",
                severity="error",
                timeout=6,
            )
            return

        # Optional GitHub delete
        github_succeeded = False
        if include_github and owner_name:
            cmd = ["gh", "repo", "delete", owner_name, "--yes"]
            logger.debug("$ %s", " ".join(cmd))
            r = subprocess.run(cmd, capture_output=True, text=True)
            logger.info(
                "gh repo delete: rc=%d stderr=%r",
                r.returncode, r.stderr.strip()[:300],
            )
            if r.returncode != 0:
                stderr = r.stderr.strip()
                hint = ""
                if "delete_repo" in stderr.lower() or "scope" in stderr.lower():
                    hint = " (need delete_repo scope; see README)"
                self.call_from_thread(
                    self.notify,
                    f"Local deleted; GitHub remove failed{hint}: {stderr[:120]}",
                    severity="warning",
                    timeout=8,
                )
            else:
                github_succeeded = True
                self.call_from_thread(
                    self.notify,
                    f"Deleted {name} (local + GitHub)",
                    severity="information",
                    timeout=3,
                )
        else:
            self.call_from_thread(
                self.notify,
                f"Deleted {name} (local)",
                severity="information",
                timeout=3,
            )

        # Refresh local; refetch github only if we actually changed github state.
        local_projects = scan_local_projects()
        self.call_from_thread(
            self._post_delete_local_loaded,
            local_projects,
            github_succeeded,
        )

    def _post_delete_local_loaded(
        self, local_projects: list[dict], do_github_refetch: bool,
    ) -> None:
        prev_idx = self._last_highlighted
        self.local_projects = local_projects
        # We only delete from local view, so this populate is always for local.
        # _populate_list will reset cursor to 0 — clamp to prev_idx after refresh.
        self._populate_list()

        def _restore_cursor() -> None:
            try:
                plist = self.query_one(OptionList)
                if plist.option_count == 0:
                    return
                target = min(
                    prev_idx if prev_idx is not None else 0,
                    plist.option_count - 1,
                )
                plist.highlighted = target
            except Exception:
                pass
        self.call_after_refresh(_restore_cursor)

        if do_github_refetch:
            self._refetch_github()

    # ── clone spinner ──────────────────────────────────────────────────────

    def _start_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_frame = 0
            self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        """Stop only when no animated state is active (clone OR create)."""
        if self._cloning_repo_index is not None or self._creating_name is not None:
            return
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._spinner_frame = 0

    def _tick_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER_FRAMES)
        # Cloning row + detail
        idx = self._cloning_repo_index
        if idx is not None and self.view == "github":
            try:
                plist = self.query_one(OptionList)
                if 0 <= idx < len(self.github_repos):
                    focused = idx == self._last_highlighted
                    plist.replace_option_prompt_at_index(
                        idx, self._cloning_row(self.github_repos[idx], focused)
                    )
            except Exception:
                pass
            if self._last_highlighted == idx and 0 <= idx < len(self.github_repos):
                self._show_github_detail(self.github_repos[idx])
        # Banner stats during a create
        if self._creating_name is not None:
            self._update_banner_stats()

    def on_resize(self) -> None:
        """Re-render right-padded list rows when the layout reflows."""
        self._refresh_list_rows()


def main() -> None:
    log_file = setup_logging()
    logger.info("opus-tui starting; log_file=%s", log_file)
    try:
        preflight()
        ProjectsApp().run()
    except Exception:
        logger.exception("Fatal error in main")
        raise
    finally:
        logger.info("opus-tui exiting")
        logging.shutdown()


if __name__ == "__main__":
    main()
