"""Omarchy theme integration.

Reads the active Omarchy theme from `~/.config/omarchy/current/theme.name`
and its 16-color palette from `~/.config/omarchy/current/theme/colors.toml`,
then maps it onto two consumers:

  - a `textual.theme.Theme` (for Textual's chrome and CSS variables)
  - an `AppColors` dataclass (semantic colors used by cell/banner helpers)

Falls back to a Catppuccin Mocha palette if Omarchy isn't installed or
the files are missing/malformed. The fallback's `accent` is overridden
to mauve (instead of Catppuccin's official peach) to preserve the
current banner's visual identity when Omarchy isn't available.
"""

import colorsys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from textual.theme import Theme

OMARCHY_DIR = Path.home() / ".config" / "omarchy" / "current"
THEME_NAME_FILE = OMARCHY_DIR / "theme.name"
COLORS_TOML_FILE = OMARCHY_DIR / "theme" / "colors.toml"


@dataclass(frozen=True)
class OmarchyPalette:
    accent: str
    cursor: str
    foreground: str
    background: str
    selection_foreground: str
    selection_background: str
    colors: tuple[str, ...]  # length 16, indices 0..15


@dataclass(frozen=True)
class AppColors:
    """Semantic colors used by cell/banner helpers in main.py."""

    text: str
    dim: str
    subtle: str
    muted_rule: str
    glyph_synced: str
    glyph_local_only: str
    glyph_github_only: str
    badge_public_bg: str
    badge_private_bg: str
    badge_text: str
    view_local: str
    view_github: str
    banner_gradient: tuple[str, str, str]  # top, mid, bot
    lang_fallback: str


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return (
        int(h[0:2], 16) / 255,
        int(h[2:4], 16) / 255,
        int(h[4:6], 16) / 255,
    )


def _rgb_to_hex(r: float, g: float, b: float) -> str:
    return f"#{int(round(r * 255)):02x}{int(round(g * 255)):02x}{int(round(b * 255)):02x}"


def _shade(hex_color: str, delta: float) -> str:
    """Adjust HSL lightness by `delta` in [-1, 1]. Returns input on error."""
    try:
        r, g, b = _hex_to_rgb(hex_color)
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        l = max(0.0, min(1.0, l + delta))
        return _rgb_to_hex(*colorsys.hls_to_rgb(h, l, s))
    except (ValueError, IndexError):
        return hex_color


def _is_dark(hex_color: str) -> bool:
    """Perceptual luminance check on a #rrggbb value. Defaults to True on error."""
    try:
        r, g, b = _hex_to_rgb(hex_color)
        return (0.299 * r + 0.587 * g + 0.114 * b) < 0.5
    except (ValueError, IndexError):
        return True


def _hex_ok(s: object) -> bool:
    if not isinstance(s, str) or not s.startswith("#") or len(s) != 7:
        return False
    try:
        int(s[1:], 16)
    except ValueError:
        return False
    return True


def read_omarchy() -> tuple[str, OmarchyPalette] | None:
    """Parse Omarchy's active theme. Returns None on any failure."""
    try:
        if not THEME_NAME_FILE.is_file() or not COLORS_TOML_FILE.is_file():
            return None
        name = THEME_NAME_FILE.read_text().strip()
        if not name:
            return None
        with COLORS_TOML_FILE.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    required = (
        "accent",
        "cursor",
        "foreground",
        "background",
        "selection_foreground",
        "selection_background",
    )
    for key in required:
        if not _hex_ok(data.get(key)):
            return None
    for i in range(16):
        if not _hex_ok(data.get(f"color{i}")):
            return None

    palette = OmarchyPalette(
        accent=data["accent"],
        cursor=data["cursor"],
        foreground=data["foreground"],
        background=data["background"],
        selection_foreground=data["selection_foreground"],
        selection_background=data["selection_background"],
        colors=tuple(data[f"color{i}"] for i in range(16)),
    )
    return name, palette


def palette_to_textual_theme(p: OmarchyPalette, name: str) -> Theme:
    return Theme(
        name=name,
        primary=p.accent,
        secondary=p.colors[5],
        accent=p.accent,
        foreground=p.foreground,
        background=p.background,
        surface=p.colors[0],
        panel=p.colors[8],
        success=p.colors[2],
        warning=p.colors[3],
        error=p.colors[1],
        dark=_is_dark(p.background),
    )


def palette_to_app_colors(p: OmarchyPalette) -> AppColors:
    return AppColors(
        text=p.foreground,
        dim=p.colors[8],
        subtle=p.colors[7],
        muted_rule=p.colors[8],
        glyph_synced=p.colors[2],
        glyph_local_only=p.colors[8],
        glyph_github_only=p.colors[6],
        badge_public_bg=p.colors[2],
        badge_private_bg=p.colors[1],
        badge_text=p.background,
        view_local=p.colors[2],
        view_github=p.colors[6],
        banner_gradient=(
            _shade(p.accent, +0.10),
            p.accent,
            _shade(p.accent, -0.10),
        ),
        lang_fallback=p.colors[7],
    )


FALLBACK_NAME = "catppuccin-mocha"
FALLBACK_PALETTE = OmarchyPalette(
    accent="#cba6f7",  # mauve (preserves current banner identity, not Catppuccin's peach)
    cursor="#f5e0dc",
    foreground="#cdd6f4",
    background="#181825",
    selection_foreground="#181825",
    selection_background="#b4befe",
    colors=(
        "#45475a",  # 0  surface1
        "#f38ba8",  # 1  red
        "#a6e3a1",  # 2  green
        "#f9e2af",  # 3  yellow
        "#89b4fa",  # 4  blue
        "#cba6f7",  # 5  mauve
        "#94e2d5",  # 6  teal
        "#bac2de",  # 7  subtext1
        "#585b70",  # 8  surface2
        "#f38ba8",  # 9  red bright
        "#a6e3a1",  # 10 green bright
        "#f9e2af",  # 11 yellow bright
        "#89b4fa",  # 12 blue bright
        "#f5c2e7",  # 13 pink
        "#94e2d5",  # 14 teal bright
        "#a6adc8",  # 15 subtext0
    ),
)
