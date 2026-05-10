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
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, OptionList, Rule, Static

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

        visible = (
            f"your project switchboard    {view_label}  ·  "
            f"{local} projects  ·  {github} on github  ·  {synced} linked"
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
        self._spinner_frame: int = 0
        self._spinner_timer = None  # set_interval handle

    def compose(self) -> ComposeResult:
        yield Banner()
        with Horizontal(id="main"):
            yield OptionList(id="projects")
            yield DetailPanel(id="detail")
        yield Footer()

    def on_mount(self) -> None:
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
        self.view = "github" if self.view == "local" else "local"

    def action_refresh(self) -> None:
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
        result = subprocess.run(
            ["gh", "repo", "clone", owner_name, target],
            capture_output=True, text=True,
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
        self.local_projects = local_projects
        target_path = Path(target)
        new_idx = next(
            (i for i, p in enumerate(local_projects) if p["path"] == target_path),
            None,
        )
        # Switching the view triggers watch_view → _populate_list (sets cursor to 0).
        self.view = "local"
        if new_idx is not None and new_idx > 0:
            def _select_new() -> None:
                try:
                    plist = self.query_one(OptionList)
                    if 0 <= new_idx < plist.option_count:
                        plist.highlighted = new_idx
                except Exception:
                    pass
            self.call_after_refresh(_select_new)

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

    # ── clone spinner ──────────────────────────────────────────────────────

    def _start_spinner(self) -> None:
        self._spinner_frame = 0
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._spinner_frame = 0

    def _tick_spinner(self) -> None:
        self._spinner_frame = (self._spinner_frame + 1) % len(SPINNER_FRAMES)
        idx = self._cloning_repo_index
        if idx is None or self.view != "github":
            return
        # Update the cloning row's prompt
        try:
            plist = self.query_one(OptionList)
            if 0 <= idx < len(self.github_repos):
                focused = idx == self._last_highlighted
                plist.replace_option_prompt_at_index(
                    idx, self._cloning_row(self.github_repos[idx], focused)
                )
        except Exception:
            pass
        # If the user is looking at the cloning project, update the detail too
        if self._last_highlighted == idx and 0 <= idx < len(self.github_repos):
            self._show_github_detail(self.github_repos[idx])

    def on_resize(self) -> None:
        """Re-render right-padded list rows when the layout reflows."""
        self._refresh_list_rows()


def main() -> None:
    preflight()
    ProjectsApp().run()


if __name__ == "__main__":
    main()
