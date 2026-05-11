# opus-tui
A TUI application for Omarchy for quickly adding and managing projects

## Omarchy / Hyprland setup (Super+P)

opus-tui follows Omarchy's centered-floating-window convention (same style as
`cliamp`, `lazydocker`, `btop`). Pressing **Super+P** opens it as a 875×600
centered float, separate from the tiled workspace.

### What was added

Two single-line additions to the user's Hyprland config — no install script,
no PATH wrapper, no symlinks. `uv run main.py` always picks up the latest
code, so editing source is enough; there is no reinstall step.

`~/.config/hypr/hyprland.conf` (under "Add any other personal Hyprland
configuration below"):

    # opus-tui — opt into Omarchy's floating-window treatment (float, center, 875x600)
    windowrule = tag +floating-window, match:class org.omarchy.opus-tui

`~/.config/hypr/bindings.conf` (under "# Add extra bindings"):

    unbind = SUPER, P  # was Omarchy's "Pseudo window" tiling toggle
    bindd = SUPER, P, Opus TUI, exec, uwsm-app -- xdg-terminal-exec --app-id=org.omarchy.opus-tui -e bash -c "cd /home/erbun/Projects/opus-tui && uv run main.py"

Then `hyprctl reload`.

### How it works

- `xdg-terminal-exec --app-id=org.omarchy.opus-tui` opens the user's default
  terminal with that Wayland app-id.
- The user-level windowrule tags any window with that class as
  `+floating-window`, which inherits Omarchy's existing system rules
  (float, center, size 875×600). No size duplication — if Omarchy ever
  changes the default, opus-tui follows.
- `unbind = SUPER, P` is required because Omarchy binds Super+P to a
  "Pseudo window" tiling toggle in
  `~/.local/share/omarchy/default/hypr/bindings/tiling-v2.conf`. Without the
  unbind, both fire and the focused window gets pseudo-tiled alongside the
  launch.

### Streamlining for others (future)

If this ever becomes a one-command install:

1. Detect Omarchy + Hyprland (look for `~/.local/share/omarchy/` and
   `hyprctl` on PATH).
2. Append the windowrule to `~/.config/hypr/hyprland.conf` (idempotent —
   grep before appending).
3. Append the `unbind` + `bindd` to `~/.config/hypr/bindings.conf`
   (idempotent), with the project path detected at install time, not
   hard-coded.
4. `hyprctl reload`.
5. Optionally install `uv` if missing and run `uv sync` once.

A wrapper at `~/.local/bin/opus-tui` would let the binding use
`omarchy-launch-or-focus-tui opus-tui` instead of the inline command,
gaining "second Super+P focuses the existing instance" behavior.
