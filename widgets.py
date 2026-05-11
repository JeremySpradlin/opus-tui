"""TUI widgets and pure render helpers.

Banner, DetailPanel, the two modal screens, and a set of free functions
that turn data into Rich Text. None of these classes or functions reach
into ProjectsApp state — colors, spinner frame, scope flags etc. all
flow in as parameters. This is what lets app.py be the only file that
holds the orchestration state.
"""

from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Rule,
    Static,
)

from git_ops import PROJECTS_DIR
from theme import AppColors

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


# ── pure render helpers ───────────────────────────────────────────────────


def local_sync_cell(synced: bool, colors: AppColors) -> Text:
    if synced:
        return Text("●", style=f"bold {colors.glyph_synced}")
    return Text("○", style=colors.glyph_local_only)


def github_sync_cell(synced: bool, colors: AppColors) -> Text:
    if synced:
        return Text("●", style=f"bold {colors.glyph_synced}")
    return Text("☁", style=f"bold {colors.glyph_github_only}")


def vis_cell(visibility: str, colors: AppColors) -> Text:
    if visibility.lower() == "public":
        return Text(" PUBLIC ", style=f"bold {colors.badge_text} on {colors.badge_public_bg}")
    return Text(" PRIVATE ", style=f"bold {colors.badge_text} on {colors.badge_private_bg}")


def lang_cell(lang: str, colors: AppColors) -> Text:
    if lang == "-":
        return Text("—", style=f"dim {colors.dim}")
    color = LANGUAGE_COLORS.get(lang, colors.lang_fallback)
    return Text(f"● {lang}", style=f"bold {color}")


def kv(key: str, value: Text, colors: AppColors) -> Text:
    return Text.assemble(
        (f"  {key:>10}  ", f"{colors.dim}"),
        value,
    )


def list_row(
    item: dict, view: str, focused: bool, synced: bool,
    colors: AppColors, content_width: int,
) -> Text:
    if view == "local":
        glyph = local_sync_cell(synced, colors)
    else:
        glyph = github_sync_cell(synced, colors)

    arrow_color = colors.view_local if view == "local" else colors.view_github
    arrow = Text("▸ " if focused else "  ", style=f"bold {arrow_color}")
    content = Text.assemble(
        arrow,
        glyph,
        "  ",
        Text(item["name"], style=f"bold {colors.text}"),
    )
    pad = max(0, content_width - content.cell_len - 1)
    return Text.assemble(Text(" " * pad), content)


def cloning_row(
    repo: dict, focused: bool, spinner_frame: int,
    colors: AppColors, content_width: int,
) -> Text:
    """Row prompt for a repo currently being cloned (animated spinner)."""
    spinner = SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]
    arrow = Text("▸ " if focused else "  ", style=f"bold {colors.view_github}")
    glyph = Text(spinner, style=f"bold {colors.glyph_github_only}")
    content = Text.assemble(
        arrow,
        glyph,
        "  ",
        Text(repo["name"], style=f"bold {colors.text}"),
    )
    pad = max(0, content_width - content.cell_len - 1)
    return Text.assemble(Text(" " * pad), content)


# ── widget classes ────────────────────────────────────────────────────────


class Banner(Vertical):
    """Top banner: ASCII title with gradient, tagline + stats, heavy rule.

    Receives all theming + spinner state through `apply_theme()` and
    `show_stats()` — does not reach into ProjectsApp.
    """

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

    def __init__(self, colors: AppColors) -> None:
        super().__init__()
        self._colors: AppColors = colors
        # Captured args from the most recent show_stats so on_resize can re-render.
        self._last_stats: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="banner-ascii")
        yield Static(id="banner-stats")
        yield Rule(line_style="heavy")

    def on_mount(self) -> None:
        self.apply_theme(self._colors)
        self.show_stats("local", 0, 0, 0, self._colors)

    def _content_width(self) -> int:
        """Inner width available to banner content (Banner width minus padding)."""
        return self.size.width - 4 if self.size.width else 80

    def apply_theme(self, colors: AppColors) -> None:
        """(Re)render the ASCII gradient using the given AppColors."""
        self._colors = colors
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
        self.apply_theme(self._colors)
        if self._last_stats is not None:
            view, local, github, synced, colors, creating_name, spinner_frame = self._last_stats
            self.show_stats(
                view, local, github, synced, colors,
                creating_name=creating_name, spinner_frame=spinner_frame,
            )

    def show_stats(
        self, view: str, local: int, github: int, synced: int,
        colors: AppColors, *,
        creating_name: str | None = None, spinner_frame: int = 0,
    ) -> None:
        self._last_stats = (view, local, github, synced, colors, creating_name, spinner_frame)
        view_label = "  Local" if view == "local" else "  GitHub"
        view_color = colors.view_local if view == "local" else colors.view_github

        creating_suffix = ""
        if creating_name:
            spinner = SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]
            creating_suffix = f"    {spinner} creating {creating_name}…"

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
        if creating_name:
            spinner = SPINNER_FRAMES[spinner_frame % len(SPINNER_FRAMES)]
            stats.append(f"    {spinner} ", style=f"bold {colors.view_local}")
            stats.append(f"creating {creating_name}…", style=f"italic {colors.subtle}")
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
        if "/" in name:
            self.notify("Name can't contain slashes", severity="warning")
            return
        # Convert internal whitespace to dashes (matches github.com's
        # new-repo input behavior — type "my new project", get "my-new-project").
        # Runs of whitespace collapse to a single dash.
        name = "-".join(name.split())
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
