# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Run / develop

- `uv run main.py` — start the TUI. `uv` resolves the env from `pyproject.toml` + `uv.lock`.
- Python 3.14+ required. Single dependency: `textual>=8.2.5`.
- No tests, no linter, no build step. Just run it.

Runtime prereqs the app enforces in `preflight()`:
- `gh` CLI on `$PATH` and `gh auth status` returns 0.
- `~/Projects/` exists.

Logs land in `./logs/opus-tui-YYYYMMDD-HHMMSS.log` (one per session, 20 most recent kept). File-only — nothing prints to the terminal because Textual owns stdout.

## Scope (read this before proposing features)

opus-tui is a **project switchboard / launcher**, not a GitHub dashboard or repo browser. The two-pane local+GitHub view is the navigation surface; the value is in actions that get the user *into* a project (clone, new project, open in editor). Features that mainly serve someone browsing their repos (issue lists, PR review, star counts, info-density columns) are off-scope unless explicitly requested.

A "local project" is defined as a direct subdirectory of `~/Projects/` that contains a `.git` directory. Bare folders without `.git` are intentionally not listed. Don't propose features that treat git-init as optional.

## Architecture

Two files, deliberately. The split is by *concern*, not size — `theme.py` is the only thing that talks to Omarchy.

### `main.py` — `ProjectsApp` (Textual `App`)

Layout: `Banner` (ASCII title + stats row + heavy `Rule`) over a `Horizontal` containing an `OptionList` (project list) and a `DetailPanel` (focused-item details), with a `Footer` for the keybinding hints.

State model worth knowing before editing:

- **Two parallel data sources**: `self.local_projects` (from `scan_local_projects()`) and `self.github_repos` (from `gh repo list --json …`). Reconciled by `nameWithOwner` — `_github_set` and `_local_set` are the derived membership sets that drive the synced glyphs.
- **Local-first load** (`load_data` → `_on_local_loaded` → `_on_github_loaded`): the local scan posts back first so the list paints in ~200ms; the slower `gh` call posts back later and updates sync glyphs in place via `_refresh_list_rows()` so cursor and scroll position survive.
- **Reactive view toggle**: `view: reactive[str]` switches between `"local"` and `"github"`. `watch_view` calls `_populate_list`, but reactive watchers do **not** fire on no-op assignments — code paths that need to repopulate while staying in the same view (e.g. post-create refresh in local view) must call `_populate_list()` explicitly. There's a comment in `_post_clone_local_loaded` flagging this.
- **Row rendering is recomputed on highlight change**: `_on_highlighted` re-renders the previously-focused row (to drop the `▸` arrow) and the new one (to add it). Rows are also right-padded based on current `OptionList` width — `on_resize` triggers a full `_refresh_list_rows()` because the pad calc depends on width.
- **Animated states share one spinner**: clone (`_cloning_repo_index`) and new-project (`_creating_name`) both drive `_spinner_timer`. `_stop_spinner()` is intentionally guarded — it only actually stops when *both* are clear, so finishing a clone while a create is still running won't kill the create's spinner.
- **`_refresh_after_clone` is reused for new-project**: after `_create_project` succeeds, it calls the same path. The `_post_clone_local_loaded` handler distinguishes the two cases via `_creating_name is not None` and synthesizes a placeholder GitHub entry for the freshly-created repo (since `github_repos` is stale until the background `_refetch_github` completes).

Threaded work uses `@work(thread=True)` with `call_from_thread` to bounce results back to the UI thread. `_refetch_github` uses `exclusive=True, group="github_refetch"` to coalesce repeated triggers.

### `theme.py` — Omarchy integration

Reads `~/.config/omarchy/current/theme.name` and `~/.config/omarchy/current/theme/colors.toml`, maps the 16-color palette onto two consumers:

1. `palette_to_textual_theme()` → `textual.theme.Theme` for Textual's CSS variables (`$primary`, `$background`, etc.).
2. `palette_to_app_colors()` → `AppColors` dataclass with semantic names (`glyph_synced`, `view_local`, `banner_gradient`, …) used by row/detail/banner renderers. **All ad-hoc color choices in `main.py` should pull from `AppColors`** — don't hardcode hex.

`FALLBACK_PALETTE` is Catppuccin Mocha with one deliberate deviation: `accent` is mauve (not Catppuccin's official peach) to preserve the banner's identity when Omarchy isn't installed.

`ProjectsApp._poll_theme` runs every 2s, checks `THEME_NAME_FILE`'s mtime, and triggers `_reload_theme` on change. Live `omarchy theme set` re-themes within ~2s without restart. `_reload_theme` toggles `self.theme` to `"textual-dark"` and back to force a re-render when the name hasn't changed but the palette has.

### Intentionally not themed

`LANGUAGE_COLORS` (the dict at the top of `main.py`) uses GitHub linguist colors verbatim — Python yellow, Rust orange, etc. — because they're semantic and globally recognized. There's a comment explaining this; don't "fix" it by routing through `AppColors`.

## Keybindings

Defined on `ProjectsApp.BINDINGS`: `q` quit, `g` toggle local↔github view, `c` clone (github view only; smart-jumps to local if already cloned), `n` new project (modal), `d` delete project (local view only; modal offers local-only or local+github), `r` refresh.

Delete-from-GitHub requires the `delete_repo` OAuth scope on `gh`. The TUI does a quick `gh auth status` check (`_has_delete_scope`) before invoking `gh repo delete` and refuses with a notification pointing at `gh auth refresh -h github.com -s delete_repo` if missing — local rmtree is *not* attempted in that path, so the user doesn't lose local files just because the github side is misconfigured.
