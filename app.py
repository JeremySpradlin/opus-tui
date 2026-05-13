"""ProjectsApp — the Textual orchestrator.

Holds the reactive state, wires actions to worker threads, and updates
the UI via widgets.py. Pure data lives in git_ops.py; pure rendering
lives in widgets.py; this module is the glue.
"""

import logging
import shutil
import subprocess
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Footer, Input, OptionList

from git_ops import (
    PROJECTS_DIR,
    fetch_github_repos,
    git_status_summary,
    scan_local_projects,
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
from widgets import (
    SPINNER_FRAMES,
    Banner,
    DeleteProjectModal,
    DetailPanel,
    NewProjectModal,
    cloning_row,
    kv,
    lang_cell,
    list_row,
    vis_cell,
)

logger = logging.getLogger(__name__)


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
    #search {
        height: 1;
        background: $surface;
        color: $foreground;
        border: none;
        padding: 0 2;
    }
    #search.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("g", "toggle_view", "Switch view"),
        Binding("c", "clone", "Clone"),
        Binding("n", "new_project", "New"),
        Binding("d", "delete_project", "Delete"),
        Binding("o", "open_project", "Open"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "open_search", "Search", key_display="/"),
        Binding("escape", "close_search", show=False),
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
        # Search/filter state. `_visible_indices` maps OptionList row index →
        # index into the current view's data list (local_projects or
        # github_repos). When no filter is active it's `range(len(items))`.
        # All actions that read self._last_highlighted as a data index MUST
        # route through _data_idx() first.
        self._search_query: str = ""
        self._visible_indices: list[int] = []
        self._search_active: bool = False

    def compose(self) -> ComposeResult:
        yield Banner(self._app_colors)
        with Horizontal(id="main"):
            yield OptionList(id="projects")
            yield DetailPanel(id="detail")
        yield Input(id="search", placeholder="filter by name…", classes="hidden")
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
            self.query_one(Banner).apply_theme(self._app_colors)
        except Exception:
            pass  # Banner may not be mounted on first call from on_mount

        if self.local_projects or self.github_repos:
            self._populate_list()

    # ── list rendering ─────────────────────────────────────────────────────

    def _list_content_width(self) -> int:
        """Width of the OptionList's content area (after CSS padding 0 2)."""
        try:
            plist = self.query_one(OptionList)
            return max(0, (plist.size.width or 0) - 4)
        except Exception:
            return 40

    def _current_items(self) -> list[dict]:
        return self.local_projects if self.view == "local" else self.github_repos

    def _compute_visible(self) -> None:
        """Rebuild _visible_indices from current view + search query."""
        items = self._current_items()
        if self._search_query:
            q = self._search_query.lower()
            self._visible_indices = [
                i for i, item in enumerate(items) if q in item["name"].lower()
            ]
        else:
            self._visible_indices = list(range(len(items)))

    def _data_idx(self, row_idx: int | None) -> int | None:
        """Map an OptionList row index → index into the data list."""
        if row_idx is None or not (0 <= row_idx < len(self._visible_indices)):
            return None
        return self._visible_indices[row_idx]

    def _row_idx_for_data(self, data_idx: int) -> int | None:
        """Reverse map: data index → OptionList row index (None if filtered out)."""
        try:
            return self._visible_indices.index(data_idx)
        except ValueError:
            return None

    def _row_for_index(self, row_idx: int, focused: bool) -> Text:
        """Render the OptionList row at the given visible row index."""
        data_idx = self._data_idx(row_idx)
        if data_idx is None:
            return Text()
        items = self._current_items()
        width = self._list_content_width()
        if self.view == "github" and data_idx == self._cloning_repo_index:
            return cloning_row(
                items[data_idx], focused, self._spinner_frame, self._app_colors, width,
            )
        item = items[data_idx]
        if self.view == "local":
            synced = item.get("github") in self._github_set
        else:
            synced = item["nameWithOwner"] in self._local_set
        return list_row(item, self.view, focused, synced, self._app_colors, width)

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
            self._app_colors,
            creating_name=self._creating_name,
            spinner_frame=self._spinner_frame,
        )

    def _populate_list(self) -> None:
        self._update_banner_stats()
        self._compute_visible()

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

        if not self._visible_indices:
            if self._search_query:
                self._show_no_matches_detail()
            else:
                self._show_empty_detail()
            return

        for row_idx in range(len(self._visible_indices)):
            plist.add_option(self._row_for_index(row_idx, focused=False))

        # Explicit first-row selection. OptionList auto-highlights index 0 on
        # first add and fires OptionHighlighted, but timing is racy with the
        # post-load layout pass — better to set state ourselves and let the
        # deferred refresh below do the actual prompt re-render at the
        # now-known content width.
        self._last_highlighted = 0
        plist.highlighted = 0  # Textual's selection state (Enter will trigger OptionSelected)
        # Don't steal focus from the search input while the user is typing.
        if not self._search_active:
            plist.focus()
        self._update_detail_for_index(0)
        self.call_after_refresh(self._refresh_list_rows)

    def _refresh_list_rows(self) -> None:
        """Re-render every visible list row at current OptionList width."""
        if not self._visible_indices:
            return
        try:
            plist = self.query_one(OptionList)
        except Exception:
            return
        for row_idx in range(len(self._visible_indices)):
            focused = row_idx == self._last_highlighted
            try:
                plist.replace_option_prompt_at_index(
                    row_idx, self._row_for_index(row_idx, focused)
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

    def _update_detail_for_index(self, row_idx: int) -> None:
        data_idx = self._data_idx(row_idx)
        if data_idx is None:
            self._show_empty_detail()
            return
        items = self._current_items()
        if self.view == "local":
            self._show_local_detail(items[data_idx])
        else:
            self._show_github_detail(items[data_idx])

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

    def _show_no_matches_detail(self) -> None:
        c = self._app_colors
        self.query_one(DetailPanel).update(
            Text(f"(no matches for '{self._search_query}')", style=f"dim {c.dim}")
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
        body.append_text(kv("github", Text(github, style=c.subtle), c))
        body.append("\n")
        body.append_text(kv("branch", Text(branch, style=c.subtle), c))
        body.append("\n")
        body.append_text(kv("changes", changes_text, c))
        body.append("\n")
        body.append_text(kv("remote", remote_text, c))
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
        body.append_text(kv("visibility", vis_cell(repo["visibility"], c), c))
        body.append("\n")
        body.append_text(kv("language", lang_cell(lang, c), c))
        body.append("\n")
        body.append_text(kv("status", status_text, c))
        self.query_one(DetailPanel).update(body)

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
        # Filter is per-view: clear it on toggle so the new view shows everything.
        # The Input widget's value is reset by action_open_search next time the
        # user opens search, so no need to touch it here.
        if self._search_query:
            self._search_query = ""
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
        row_idx = self._last_highlighted
        data_idx = self._data_idx(row_idx)
        if data_idx is None or not self.github_repos:
            return
        repo = self.github_repos[data_idx]
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

        self._cloning_repo_index = data_idx
        self._start_spinner()
        # Initial render of the cloning state for both row and detail.
        try:
            plist = self.query_one(OptionList)
            plist.replace_option_prompt_at_index(
                row_idx,
                cloning_row(
                    repo, row_idx == self._last_highlighted,
                    self._spinner_frame, self._app_colors,
                    self._list_content_width(),
                ),
            )
        except Exception:
            pass
        if self._last_highlighted == row_idx:
            self._show_github_detail(repo)
        self.clone_repo(owner_name, str(target), data_idx)

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
        self, owner_name: str, target: str, data_idx: int, success: bool, err: str
    ) -> None:
        self._stop_spinner()
        self._cloning_repo_index = None

        if success:
            self.notify(f"Cloned {owner_name}", severity="information", timeout=3)
            self._refresh_after_clone(target)
            return

        # Failure: restore the row + detail to their pre-clone state. The row
        # may have been filtered out while the clone was running — skip if so.
        row_idx = self._row_idx_for_data(data_idx)
        if row_idx is not None:
            try:
                plist = self.query_one(OptionList)
                focused = row_idx == self._last_highlighted
                plist.replace_option_prompt_at_index(
                    row_idx, self._row_for_index(row_idx, focused),
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

        # Clear any active search filter so the freshly-created/cloned project
        # is visible (its name likely doesn't match the prior filter).
        self._search_query = ""

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
            # Detect github replication race: gh creates the repo via the API
            # but the immediate `git push` runs before github's git endpoint
            # is ready, failing with "Repository not found" / fatal: repo not
            # found. The repo URL appears in stdout iff the API call succeeded;
            # the stderr signals the push race. When we see this pattern,
            # retry the push (the repo already exists, no need to recreate).
            repo_created = "github.com/" in r.stdout
            push_race = (
                repo_created
                and "Repository not found" in r.stderr
                and "fatal: repository" in r.stderr
            )
            if push_race:
                logger.warning(
                    "create_project: gh push race detected, retrying push"
                )
                push_ok = False
                for delay in (0.5, 1.0, 2.0):
                    time.sleep(delay)
                    push_cmd = [
                        "git", "-C", str(target),
                        "push", "-u", "origin", "main",
                    ]
                    logger.debug("retry push: $ %s", " ".join(push_cmd))
                    pr = subprocess.run(
                        push_cmd, capture_output=True, text=True,
                    )
                    logger.info(
                        "retry push: rc=%d stderr=%r",
                        pr.returncode, pr.stderr.strip()[:200],
                    )
                    if pr.returncode == 0:
                        push_ok = True
                        break
                if push_ok:
                    self.call_from_thread(
                        self.notify,
                        f"Created {name} (recovered from push race)",
                        severity="information",
                        timeout=4,
                    )
                else:
                    self.call_from_thread(
                        self.notify,
                        f"Created {name} on GitHub, but push failed. "
                        f"Run: git push -u origin main from the project dir",
                        severity="warning",
                        timeout=10,
                    )
            else:
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
        data_idx = self._data_idx(self._last_highlighted)
        if data_idx is None or not self.local_projects:
            return
        project = self.local_projects[data_idx]
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

    # ── open project ───────────────────────────────────────────────────────

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, _event: OptionList.OptionSelected) -> None:
        # Enter on a row → same as pressing `o`. The OptionList captures Enter
        # and fires OptionSelected; we route both inputs through the same action.
        self.action_open_project()

    def action_open_project(self) -> None:
        if self.view != "local":
            self.notify(
                "Switch to local view (g) to open a project",
                severity="information",
            )
            return
        data_idx = self._data_idx(self._last_highlighted)
        if data_idx is None or not self.local_projects:
            return
        project = self.local_projects[data_idx]
        name = project["name"]
        path = project["path"]
        if not path.is_dir():
            self.notify(
                f"{path} no longer exists — try refresh (r)",
                severity="warning",
            )
            return

        logger.info("open_project: name=%s path=%s", name, path)
        try:
            # Popen + start_new_session: detach from opus-tui's process group
            # so the editor survives our exit() below.
            subprocess.Popen(
                ["omarchy-launch-editor", str(path)],
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error("open_project: omarchy-launch-editor not on PATH")
            self.notify(
                "omarchy-launch-editor not found on PATH",
                severity="error",
                timeout=8,
            )
            return

        # Switchboard metaphor: connect the call, get out of the way.
        self.exit()

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
        # Cloning row + detail. _cloning_repo_index is a data index; the row
        # may be filtered out of view, in which case we skip the row repaint.
        data_idx = self._cloning_repo_index
        if data_idx is not None and self.view == "github":
            row_idx = self._row_idx_for_data(data_idx)
            if row_idx is not None:
                try:
                    plist = self.query_one(OptionList)
                    focused = row_idx == self._last_highlighted
                    plist.replace_option_prompt_at_index(
                        row_idx, self._row_for_index(row_idx, focused),
                    )
                except Exception:
                    pass
            if (
                self._data_idx(self._last_highlighted) == data_idx
                and 0 <= data_idx < len(self.github_repos)
            ):
                self._show_github_detail(self.github_repos[data_idx])
        # Banner stats during a create
        if self._creating_name is not None:
            self._update_banner_stats()

    def on_resize(self) -> None:
        """Re-render right-padded list rows when the layout reflows."""
        self._refresh_list_rows()

    # ── search / filter ────────────────────────────────────────────────────

    def action_open_search(self) -> None:
        """`/` — show the search input and focus it. Resets any prior query."""
        try:
            search = self.query_one("#search", Input)
        except Exception:
            return  # not on the main screen (e.g. a modal is up)
        self._search_active = True
        # Reset to a clean filter every time the user opens search. If the
        # value differs from "", this fires Input.Changed → _on_search_changed
        # which clears the filter and repopulates.
        self._search_query = ""
        search.value = ""
        search.remove_class("hidden")
        search.focus()

    def action_close_search(self) -> None:
        """Escape — clear filter and close the search input.

        Also clears a *committed* filter (input hidden after Enter), so the
        user can recover the full list with Esc whether the input is open or
        not.
        """
        if not self._search_active and not self._search_query:
            return
        try:
            search = self.query_one("#search", Input)
        except Exception:
            return
        self._search_active = False
        search.add_class("hidden")
        # Two cases for clearing the query + repopulating the list:
        #   1. Input still has text — set value = "", which fires Input.Changed;
        #      that handler clears _search_query and repopulates.
        #   2. Input is already empty but _search_query is non-empty (Esc after
        #      Enter-commit with no further typing) — clear and populate here.
        if search.value:
            search.value = ""
        elif self._search_query:
            self._search_query = ""
            self._populate_list()
        self.query_one(OptionList).focus()

    @on(Input.Changed, "#search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        new_query = event.value.strip()
        if new_query == self._search_query:
            return
        self._search_query = new_query
        self._populate_list()

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self, _event: Input.Submitted) -> None:
        """Enter in the search input — commit filter, hide input, focus list."""
        try:
            search = self.query_one("#search", Input)
        except Exception:
            return
        self._search_active = False
        search.add_class("hidden")
        self.query_one(OptionList).focus()
