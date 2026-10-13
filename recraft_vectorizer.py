"""
Recraft Vectorizer
==================
A small desktop GUI to convert local raster images to SVG using the Recraft
vector API (https://www.recraft.ai/docs/api-reference/examples).

Features
--------
* Pick a local image and convert it to SVG via Recraft's `/images/vectorize`.
* Choose a vector *style* and *model* from dropdowns.
* Optionally down-scale the image before uploading (smaller / faster / cheaper).
* Before / after preview toggle rendered directly inside the UI.
* Live input / output file-size information.
* "Save as..." for the resulting SVG.
* Settings dialog: API key, default download folder, default style & model.
* Dark theme with a red accent.

Run:  python recraft_vectorizer.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests
from PIL import Image, ImageDraw, ImageFont

import svgo_engine as engine

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False

# --------------------------------------------------------------------------- #
#  Constants / configuration
# --------------------------------------------------------------------------- #

APP_NAME = "Recraft Vectorizer"
API_BASE = "https://external.api.recraft.ai/v1"
VECTORIZE_URL = f"{API_BASE}/images/vectorize"

APP_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
ICON_SETTINGS_PATH = APP_DIR / "icon" / "settings.svg"

CONFIG_DIR = Path.home() / ".recraft_vectorizer"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Colour palette (dark theme + red accent)
COL_BG = "#161616"
COL_PANEL = "#1f1f1f"
COL_PANEL_2 = "#262626"
COL_BORDER = "#333333"
COL_TEXT = "#ECECEC"
COL_TEXT_DIM = "#9a9a9a"
COL_RED = "#E23744"
COL_RED_HOVER = "#c02734"
COL_RED_DIM = "#7a2029"

# Recraft V4.1 vector models. `label` is the friendly UI name, `id` the API id.
MODELS = [
    {
        "id": "recraftv4_1_vector",
        "label": "Standard Vector",
        "price": "$0.08",
        "desc": "Editable SVG with expressive, artistic results — great for logos "
                "and illustrations. The most affordable option.",
    },
    {
        "id": "recraftv4_1_pro_vector",
        "label": "Pro Vector",
        "price": "$0.30",
        "desc": "Print-ready output at higher resolution with the finest detail. "
                "Best when you need professional-grade quality.",
    },
    {
        "id": "recraftv4_1_utility_pro_vector",
        "label": "Utility Pro Vector",
        "price": "$0.30",
        "desc": "Clean, predictable, general-purpose vectors — ideal for mockups "
                "and product presentations.",
    },
]
MODEL_LABELS = [m["label"] for m in MODELS]
MODEL_BY_LABEL = {m["label"]: m for m in MODELS}
MODEL_BY_ID = {m["id"]: m for m in MODELS}
DEFAULT_MODEL_ID = MODELS[0]["id"]

# Recraft requires the shorter image edge to be at least this many pixels.
MIN_EDGE = 256

SUPPORTED_INPUT = [
    ("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tiff"),
    ("All files", "*.*"),
]


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #

def human_size(num_bytes: int | None) -> str:
    """Return a human readable file size like '243.1 KB'."""
    if num_bytes is None:
        return "–"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def svg_dimensions(svg_bytes: bytes) -> tuple[int | None, int | None]:
    """Best-effort extraction of width/height from an SVG document."""
    try:
        head = svg_bytes[:2000].decode("utf-8", "ignore")
    except Exception:
        return None, None

    def _num(match: str) -> int | None:
        m = re.search(r"[\d.]+", match)
        return int(round(float(m.group()))) if m else None

    w = re.search(r'\bwidth\s*=\s*"([^"]+)"', head)
    h = re.search(r'\bheight\s*=\s*"([^"]+)"', head)
    if w and h:
        wv, hv = _num(w.group(1)), _num(h.group(1))
        if wv and hv:
            return wv, hv

    vb = re.search(r'viewBox\s*=\s*"([^"]+)"', head)
    if vb:
        parts = re.split(r"[ ,]+", vb.group(1).strip())
        if len(parts) == 4:
            try:
                return int(round(float(parts[2]))), int(round(float(parts[3])))
            except ValueError:
                pass
    return None, None


def render_svg_to_pil(svg_bytes: bytes, target: int = 1000) -> Image.Image | None:
    """Rasterize an SVG document to a PIL image for preview.

    Uses resvg (browser-faithful, handles gradients/clip-paths, self-contained
    wheels) as the primary renderer and falls back to PyMuPDF. Returns None only
    if no renderer is available or all fail.
    """
    # 1) resvg ---------------------------------------------------------------
    try:
        import resvg_py  # type: ignore

        text = svg_bytes.decode("utf-8", "replace")
        png = resvg_py.svg_to_bytes(svg_string=text, width=target)
        return Image.open(io.BytesIO(bytes(png))).convert("RGBA")
    except Exception:
        pass

    # 2) PyMuPDF (fallback) --------------------------------------------------
    try:
        import pymupdf  # type: ignore

        doc = pymupdf.open(stream=svg_bytes, filetype="svg")
        page = doc[0]
        zoom = target / max(page.rect.width, page.rect.height, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=True)
        mode = "RGBA" if pix.alpha else "RGB"
        return Image.frombytes(mode, (pix.width, pix.height), pix.samples).convert("RGBA")
    except Exception:
        return None


def load_svg_icon(
    path: Path | str,
    size: tuple[int, int] = (20, 20),
    color: str | None = None,
) -> ctk.CTkImage | None:
    """Rasterize an SVG icon from file into a high-DPI ctk.CTkImage.

    Optionally replaces white fills (#fff / #ffffff) with ``color``.
    Returns None if the file does not exist or rasterization fails.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        svg_text = p.read_text(encoding="utf-8")
        if color:
            svg_text = re.sub(r"#fff(fff)?\b", color, svg_text, flags=re.IGNORECASE)
        target = max(size) * 4
        pil_img = render_svg_to_pil(svg_text.encode("utf-8"), target=target)
        if pil_img is None:
            return None
        return ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=size)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Hover tooltip
# --------------------------------------------------------------------------- #

class Tooltip:
    """A lightweight hover tooltip for any Tk/CTk widget (children included)."""

    def __init__(self, widget, text: str, wraplength: int = 340, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.delay = delay
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        self._bind(widget)

    def _bind(self, widget) -> None:
        # CTk widgets are composites; bind the children too.
        try:
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind(child)

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        self._tip.configure(bg=COL_BORDER)
        tk.Label(
            self._tip, text=self.text, justify="left", bg=COL_PANEL_2, fg=COL_TEXT,
            wraplength=self.wraplength, font=("Segoe UI", 9), padx=12, pady=10, bd=0,
        ).pack(padx=1, pady=1)

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


# --------------------------------------------------------------------------- #
#  Persistent configuration
# --------------------------------------------------------------------------- #

class Config:
    DEFAULTS = {
        "api_key": "",
        "download_dir": str(Path.home() / "Downloads"),
        "default_model": DEFAULT_MODEL_ID,
        "resize_enabled": False,
        "resize_max_edge": 1024,
        "minify_enabled": False,
        "minify_settings": engine.default_settings(),
        "collapsed_groups": [],
    }

    def __init__(self) -> None:
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if CONFIG_PATH.exists():
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self.data.update({k: loaded[k] for k in loaded if k in self.DEFAULTS})
        except Exception:
            pass  # fall back to defaults on any corruption

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(self.data, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # pragma: no cover
            messagebox.showerror(APP_NAME, f"Could not save settings:\n{exc}")

    def __getitem__(self, key: str):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value


# --------------------------------------------------------------------------- #
#  Recraft API client
# --------------------------------------------------------------------------- #

class RecraftError(Exception):
    pass


class RecraftClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key.strip()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def vectorize(self, image_bytes: bytes, filename: str, model: str) -> bytes:
        """Convert a raster image to SVG bytes via `/images/vectorize`.

        The endpoint accepts `file`, `response_format` and (harmlessly) `model`.
        If the request with `model` is rejected we retry with a minimal payload.
        """
        if not self.api_key:
            raise RecraftError("No API key set. Open Settings and add your Recraft API key.")

        files = {"file": (filename, image_bytes, "application/octet-stream")}

        extended = {"response_format": "url"}
        if model:
            extended["model"] = model

        resp = self._post(files, extended)
        if resp is not None:
            return resp

        # Fallback: minimal payload that is always accepted.
        resp = self._post(files, {"response_format": "url"}, raise_on_error=True)
        return resp  # type: ignore[return-value]

    def _post(self, files, data, raise_on_error: bool = False) -> bytes | None:
        try:
            r = requests.post(
                VECTORIZE_URL,
                headers=self._headers(),
                files=files,
                data=data,
                timeout=180,
            )
        except requests.RequestException as exc:
            raise RecraftError(f"Network error while contacting Recraft:\n{exc}")

        if r.status_code >= 400:
            detail = self._error_detail(r)
            # 400/422 with extended params -> signal caller to retry minimal.
            if not raise_on_error and r.status_code in (400, 422) and len(data) > 1:
                return None
            raise RecraftError(f"Recraft API error {r.status_code}: {detail}")

        try:
            payload = r.json()
        except ValueError:
            raise RecraftError("Unexpected (non-JSON) response from Recraft.")

        url = self._extract_url(payload)
        if not url:
            raise RecraftError(f"Could not find image URL in response:\n{payload}")

        return self._download(url)

    @staticmethod
    def _extract_url(payload: dict) -> str | None:
        if isinstance(payload, dict):
            img = payload.get("image")
            if isinstance(img, dict) and img.get("url"):
                return img["url"]
            data = payload.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0].get("url")
        return None

    @staticmethod
    def _error_detail(r: requests.Response) -> str:
        try:
            j = r.json()
            if isinstance(j, dict):
                return str(j.get("message") or j.get("error") or j)
            return str(j)
        except ValueError:
            return r.text[:400] or r.reason

    def _download(self, url: str) -> bytes:
        try:
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            raise RecraftError(f"Could not download result SVG:\n{exc}")


# --------------------------------------------------------------------------- #
#  Settings dialog
# --------------------------------------------------------------------------- #

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: "App", config: Config) -> None:
        super().__init__(master)
        self.config_ref = config
        self.title("Settings")
        self.geometry("560x340")
        self.configure(fg_color=COL_BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        pad = {"padx": 20, "pady": (6, 0)}

        self._settings_icon = load_svg_icon(ICON_SETTINGS_PATH, size=(20, 20), color=COL_TEXT)
        ctk.CTkLabel(
            self, text="  Settings" if self._settings_icon else "Settings",
            image=self._settings_icon, compound="left",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COL_TEXT,
        ).pack(anchor="w", padx=20, pady=(18, 4))

        # API key ------------------------------------------------------------
        ctk.CTkLabel(self, text="Recraft API key", text_color=COL_TEXT_DIM).pack(anchor="w", **pad)
        key_row = ctk.CTkFrame(self, fg_color="transparent")
        key_row.pack(fill="x", padx=20)
        self.api_entry = ctk.CTkEntry(
            key_row, show="•", fg_color=COL_PANEL_2, border_color=COL_BORDER,
        )
        self.api_entry.insert(0, config["api_key"])
        self.api_entry.pack(side="left", fill="x", expand=True)
        self._show_key = False
        self.eye_btn = ctk.CTkButton(
            key_row, text="Show", width=64, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._toggle_key,
        )
        self.eye_btn.pack(side="left", padx=(8, 0))

        # Download folder ----------------------------------------------------
        ctk.CTkLabel(self, text="Default download folder", text_color=COL_TEXT_DIM).pack(anchor="w", **pad)
        dir_row = ctk.CTkFrame(self, fg_color="transparent")
        dir_row.pack(fill="x", padx=20)
        self.dir_entry = ctk.CTkEntry(dir_row, fg_color=COL_PANEL_2, border_color=COL_BORDER)
        self.dir_entry.insert(0, config["download_dir"])
        self.dir_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            dir_row, text="Browse", width=80, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._browse_dir,
        ).pack(side="left", padx=(8, 0))

        # Default model ------------------------------------------------------
        ctk.CTkLabel(self, text="Default model", text_color=COL_TEXT_DIM).pack(anchor="w", **pad)
        self.model_menu = ctk.CTkOptionMenu(
            self, values=MODEL_LABELS, fg_color=COL_PANEL_2,
            button_color=COL_RED, button_hover_color=COL_RED_HOVER,
        )
        self.model_menu.set(MODEL_BY_ID.get(config["default_model"], MODELS[0])["label"])
        self.model_menu.pack(anchor="w", padx=20, fill="x")

        # Buttons ------------------------------------------------------------
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=20, side="bottom")
        ctk.CTkButton(
            btn_row, text="Cancel", fg_color=COL_PANEL_2, hover_color=COL_BORDER,
            command=self.destroy,
        ).pack(side="right")
        ctk.CTkButton(
            btn_row, text="Save", fg_color=COL_RED, hover_color=COL_RED_HOVER,
            command=self._save,
        ).pack(side="right", padx=(0, 10))

    def _toggle_key(self) -> None:
        self._show_key = not self._show_key
        self.api_entry.configure(show="" if self._show_key else "•")
        self.eye_btn.configure(text="Hide" if self._show_key else "Show")

    def _browse_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.dir_entry.get() or str(Path.home()))
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def _save(self) -> None:
        self.config_ref["api_key"] = self.api_entry.get().strip()
        self.config_ref["download_dir"] = self.dir_entry.get().strip()
        self.config_ref["default_model"] = MODEL_BY_LABEL[self.model_menu.get()]["id"]
        self.config_ref.save()
        self.master.apply_defaults()  # type: ignore[attr-defined]
        self.destroy()


# --------------------------------------------------------------------------- #
#  SVGO feature list (collapsible groups) and settings dialog
# --------------------------------------------------------------------------- #

def _switch(parent, text: str, var, command, tip: str = "",
            small: bool = False, padx: int = 18) -> ctk.CTkFrame:
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=padx, pady=1)
    sw = ctk.CTkSwitch(
        row, text=text, variable=var, command=command,
        progress_color=COL_RED, button_color=COL_RED,
        button_hover_color=COL_RED_HOVER, fg_color=COL_BORDER,
        text_color=COL_TEXT, switch_width=38, switch_height=18,
        font=ctk.CTkFont(size=12 if small else 13),
    )
    sw.pack(side="left", fill="x", expand=True)
    if tip:
        Tooltip(sw, tip)
    return row


class PluginGroup:
    """A named, collapsible block of plugin switches."""

    def __init__(self, parent, key: str, label: str, blurb: str,
                 dialog: "SvgoSettingsDialog") -> None:
        self.key = key
        self.label = label
        self.dialog = dialog
        self.collapsed = False
        self.rows: list[tuple[str, str, ctk.CTkFrame]] = []

        self.container = ctk.CTkFrame(parent, fg_color="transparent")
        self.container.pack(fill="x", padx=8, pady=(6, 0))

        self.header = ctk.CTkFrame(self.container, fg_color=COL_PANEL_2, corner_radius=6)
        self.header.pack(fill="x")
        self.chevron = ctk.CTkLabel(
            self.header, text="▾", text_color=COL_RED, width=16,
            font=ctk.CTkFont(size=12, weight="bold"), cursor="hand2",
        )
        self.chevron.pack(side="left", padx=(8, 2), pady=5)
        self.title = ctk.CTkLabel(
            self.header, text=label, text_color=COL_TEXT, anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"), cursor="hand2",
        )
        self.title.pack(side="left", fill="x", expand=True)
        self.count = ctk.CTkLabel(
            self.header, text="", text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11), cursor="hand2",
        )
        self.count.pack(side="right", padx=(4, 10))

        for widget in (self.header, self.chevron, self.title, self.count):
            widget.bind("<Button-1>", lambda _e: self.toggle())
        Tooltip(self.header, f"{blurb}\n\nClick to collapse or expand.")

        self.body = ctk.CTkFrame(self.container, fg_color="transparent")
        self.body.pack(fill="x")

    def add_plugin(self, pid: str, label: str, var, help_text: str) -> None:
        row = _switch(
            self.body, label, var,
            lambda p=pid, v=var: self.dialog.set_plugin(p, v.get()),
            f"{pid}\n\n{help_text}", small=True, padx=8,
        )
        self.rows.append((pid, f"{label} {pid} {self.label}".lower(), row))

    def toggle(self) -> None:
        self.set_collapsed(not self.collapsed)
        self.dialog.persist_group_state()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.chevron.configure(text="▸" if collapsed else "▾")
        if collapsed:
            self.body.pack_forget()
        elif not self.body.winfo_ismapped():
            self.body.pack(fill="x")

    def apply_filter(self, needle: str) -> int:
        """Show only rows matching ``needle``; return how many matched."""
        for _pid, _haystack, row in self.rows:
            row.pack_forget()
        matches = [r for r in self.rows if not needle or needle in r[1]]
        for _pid, _haystack, row in matches:
            row.pack(fill="x", padx=8, pady=1)

        # pack() appends, so the caller re-packs the groups in catalogue order.
        self.container.pack_forget()
        if not matches:
            return 0
        self.container.pack(fill="x", padx=8, pady=(6, 0))
        self.set_collapsed(False if needle else self.collapsed)
        return len(matches)

    def update_count(self, settings_plugins: dict) -> tuple[int, int]:
        enabled = sum(1 for pid, _h, _r in self.rows if settings_plugins.get(pid))
        total = len(self.rows)
        self.count.configure(
            text=f"{enabled}/{total}",
            text_color=COL_TEXT_DIM if enabled else COL_BORDER,
        )
        return enabled, total


class SvgoSettingsDialog(ctk.CTkToplevel):
    """Live SVGO configuration: global options + grouped, filterable features.

    Any change writes straight into ``master.minify_settings`` and asks the app
    to re-optimize (debounced), so the preview updates live.
    """

    def __init__(self, master: "App") -> None:
        super().__init__(master)
        self.app = master
        self.settings = master.minify_settings  # live reference

        self.title("SVGO settings")
        self.geometry("440x780")
        self.minsize(420, 480)
        self.configure(fg_color=COL_PANEL)
        self.transient(master)  # stays above the app, but no grab -> preview visible
        try:
            self.geometry(f"+{master.winfo_rootx() + 24}+{master.winfo_rooty() + 40}")
        except Exception:
            pass

        self._plugin_vars: dict[str, ctk.BooleanVar] = {}
        self._groups: list[PluginGroup] = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Header ------------------------------------------------------------
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))
        ctk.CTkLabel(
            head, text="FEATURES", text_color=COL_RED,
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(side="left")
        reset = ctk.CTkButton(
            head, text="Reset", width=60, height=26, fg_color=COL_PANEL_2,
            hover_color=COL_BORDER, command=self._reset,
        )
        reset.pack(side="right")
        Tooltip(reset, "Back to SVGO's preset-default")

        sub = ctk.CTkFrame(self, fg_color="transparent")
        sub.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.features_count = ctk.CTkLabel(
            sub, text="", text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11), anchor="w",
        )
        self.features_count.pack(side="left", fill="x", expand=True)
        self.fold_btn = ctk.CTkButton(
            sub, text="Collapse all", width=88, height=24, fg_color="transparent",
            hover_color=COL_PANEL_2, text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11), command=self._toggle_all_groups,
        )
        self.fold_btn.pack(side="right")

        self.filter_entry = ctk.CTkEntry(
            self, placeholder_text="Filter features…", fg_color=COL_PANEL_2,
            border_color=COL_BORDER, height=30,
        )
        self.filter_entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.filter_entry.bind("<KeyRelease>", lambda _e: self._apply_filter())

        # Global options ----------------------------------------------------
        opts = ctk.CTkFrame(self, fg_color=COL_PANEL_2, corner_radius=8)
        opts.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.multipass_var = ctk.BooleanVar(value=bool(self.settings["multipass"]))
        _switch(opts, "Multipass", self.multipass_var,
                lambda: self._set_global("multipass", self.multipass_var.get()),
                "Run the whole plugin chain repeatedly until the file stops "
                "shrinking. Slower, usually a little smaller.", padx=10)
        self.pretty_var = ctk.BooleanVar(value=bool(self.settings["pretty"]))
        _switch(opts, "Prettify markup", self.pretty_var,
                lambda: self._set_global("pretty", self.pretty_var.get()),
                "Indent the output instead of putting it on one line. "
                "Readable, but larger.", padx=10)
        self.float_slider, self.float_value = self._precision_row(
            opts, "Number precision", "floatPrecision",
            "Decimal places kept for coordinates and lengths. Lower is smaller "
            "but coarser; 3 is a good default.")
        self.transform_slider, self.transform_value = self._precision_row(
            opts, "Transform precision", "transformPrecision",
            "Decimal places kept inside transform() lists. Rounding these too "
            "hard visibly shifts shapes.")
        ctk.CTkFrame(opts, fg_color="transparent", height=6).pack()

        # Feature groups ----------------------------------------------------
        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.grid(row=4, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        collapsed = set(self.app.config_ref["collapsed_groups"] or [])
        for key, label, blurb, members in engine.plugins_by_category():
            group = PluginGroup(body, key, label, blurb, self)
            for pid, plugin_label, _default, help_text, _cat in members:
                var = ctk.BooleanVar(value=bool(self.settings["plugins"].get(pid, False)))
                self._plugin_vars[pid] = var
                group.add_plugin(pid, plugin_label, var, help_text)
            if key in collapsed:
                group.set_collapsed(True)
            self._groups.append(group)

        # Footer ------------------------------------------------------------
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=5, column=0, sticky="ew", padx=16, pady=(6, 14))
        self.result_label = ctk.CTkLabel(
            footer, text="", text_color=COL_TEXT_DIM, anchor="w",
            font=ctk.CTkFont(size=11), wraplength=290, justify="left",
        )
        self.result_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            footer, text="Close", width=80, fg_color=COL_RED,
            hover_color=COL_RED_HOVER, command=self._close,
        ).pack(side="right")

        self._refresh_precision_labels()
        self._update_group_counts()
        self.persist_group_state(save=False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(60, self.focus)

    # ---- builders ------------------------------------------------------- #
    def _precision_row(self, parent, label: str, key: str,
                       tip: str) -> tuple[ctk.CTkSlider, ctk.CTkLabel]:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(10, 0))
        name = ctk.CTkLabel(row, text=label, text_color=COL_TEXT_DIM)
        name.pack(side="left")
        value_label = ctk.CTkLabel(
            row, text="", text_color=COL_TEXT, width=20,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        value_label.pack(side="right")
        Tooltip(name, tip)
        slider = ctk.CTkSlider(
            parent, from_=engine.PRECISION_MIN, to=engine.PRECISION_MAX,
            number_of_steps=engine.PRECISION_MAX - engine.PRECISION_MIN,
            progress_color=COL_RED, button_color=COL_RED,
            button_hover_color=COL_RED_HOVER, fg_color=COL_BORDER,
            command=lambda v, k=key, lbl=value_label: self._on_precision(k, lbl, v),
        )
        slider.set(int(self.settings[key]))
        slider.pack(fill="x", padx=18, pady=(2, 2))
        return slider, value_label

    # ---- change handlers ------------------------------------------------ #
    def _on_precision(self, key: str, value_label, value: float) -> None:
        iv = int(round(value))
        if self.settings[key] == iv:
            return
        self.settings[key] = iv
        value_label.configure(text=str(iv))
        self.app.schedule_minify()

    def _set_global(self, key: str, value) -> None:
        self.settings[key] = bool(value)
        self.app.schedule_minify()

    def set_plugin(self, pid: str, value) -> None:
        self.settings["plugins"][pid] = bool(value)
        self._update_group_counts()
        self.app.schedule_minify()

    def _refresh_precision_labels(self) -> None:
        self.float_value.configure(text=str(int(self.settings["floatPrecision"])))
        self.transform_value.configure(text=str(int(self.settings["transformPrecision"])))

    def _update_group_counts(self) -> None:
        enabled = total = 0
        for group in self._groups:
            group_on, group_total = group.update_count(self.settings["plugins"])
            enabled += group_on
            total += group_total
        self.features_count.configure(text=f"{enabled} of {total} features enabled")

    def _toggle_all_groups(self) -> None:
        collapse = any(not group.collapsed for group in self._groups)
        for group in self._groups:
            group.set_collapsed(collapse)
        self.persist_group_state()

    def persist_group_state(self, save: bool = True) -> None:
        collapsed = [g.key for g in self._groups if g.collapsed]
        self.fold_btn.configure(
            text="Expand all" if len(collapsed) == len(self._groups) else "Collapse all")
        if save:
            self.app.config_ref["collapsed_groups"] = collapsed
            self.app.config_ref.save()

    def _apply_filter(self) -> None:
        needle = self.filter_entry.get().strip().lower()
        matches = sum(group.apply_filter(needle) for group in self._groups)
        if needle:
            noun = "feature" if matches == 1 else "features"
            self.features_count.configure(text=f"{matches} {noun} match “{needle}”.")
        else:
            self._update_group_counts()

    def _reset(self) -> None:
        defaults = engine.default_settings()
        self.settings.clear()
        self.settings.update(defaults)
        self.float_slider.set(int(defaults["floatPrecision"]))
        self.transform_slider.set(int(defaults["transformPrecision"]))
        self.multipass_var.set(defaults["multipass"])
        self.pretty_var.set(defaults["pretty"])
        for pid, var in self._plugin_vars.items():
            var.set(bool(defaults["plugins"][pid]))
        self._refresh_precision_labels()
        self._update_group_counts()
        self.app.schedule_minify()

    def set_result(self, text: str) -> None:
        try:
            self.result_label.configure(text=text)
        except Exception:
            pass

    def _close(self) -> None:
        self.app.persist_minify_settings()
        self.app._svgo_dialog = None
        self.destroy()


# --------------------------------------------------------------------------- #
#  Main application
# --------------------------------------------------------------------------- #

# Enable native file drag & drop when tkinterdnd2 is available by mixing in its
# wrapper. Falls back to a plain CTk window otherwise.
_AppBase = (ctk.CTk, TkinterDnD.DnDWrapper) if _DND_AVAILABLE else (ctk.CTk,)


class App(*_AppBase):  # type: ignore[misc]
    PREVIEW_W = 620
    PREVIEW_H = 560

    def __init__(self) -> None:
        super().__init__()
        self.config_ref = Config()

        self.title(APP_NAME)
        self.geometry("1120x780")
        self.minsize(980, 700)
        self.configure(fg_color=COL_BG)

        self._dnd_ready = False
        if _DND_AVAILABLE:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
                self._dnd_ready = True
            except Exception:
                self._dnd_ready = False

        # State
        self.input_path: Path | None = None
        self.original_img: Image.Image | None = None      # full-res source
        self.sent_img: Image.Image | None = None           # possibly resized
        self.sent_bytes_len: int | None = None
        self.svg_raw: bytes | None = None                  # unmodified Recraft SVG
        self.svg_min: bytes | None = None                  # SVGO-optimized SVG
        self.svg_bytes: bytes | None = None                # what is shown / saved
        self.svg_pil: Image.Image | None = None
        self.showing_svg = False
        self.view = "Original"
        self._preview_cache: ctk.CTkImage | None = None

        # SVG minify (SVGO) state
        self.minify_settings: dict = engine.normalize_settings(
            self.config_ref["minify_settings"]
        )
        # svgo-py is pure Python and would hold the GIL (freezing the UI), so
        # it runs in a single reusable worker process.
        self._svgo_pool: ProcessPoolExecutor | None = None
        self._svgo_dialog: "SvgoSettingsDialog | None" = None
        self._minify_after: str | None = None
        self._minify_seq = 0

        self._build_layout()
        self.apply_defaults()
        self._update_preview()
        self._setup_dnd()

    def _setup_dnd(self) -> None:
        """Register the window (and preview) as a file drop target."""
        if not self._dnd_ready:
            return
        try:
            for target in (self, self.preview_frame, self.preview_label):
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        try:
            paths = self.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        if paths:
            self._load_image_path(paths[0])

    # ---- layout --------------------------------------------------------- #
    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=400)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_preview()

    def _build_sidebar(self) -> None:
        side = ctk.CTkScrollableFrame(self, fg_color=COL_PANEL, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(side, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 4))
        ctk.CTkLabel(
            header, text="●", text_color=COL_RED,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            header, text=" Recraft Vectorizer",
            font=ctk.CTkFont(size=20, weight="bold"), text_color=COL_TEXT,
        ).pack(side="left")
        self._gear_img = load_svg_icon(ICON_SETTINGS_PATH, size=(24, 24), color=COL_TEXT_DIM)
        self._gear_hover_img = load_svg_icon(ICON_SETTINGS_PATH, size=(24, 24), color=COL_TEXT)
        gear = ctk.CTkButton(
            header,
            text="" if self._gear_img else "⚙",
            image=self._gear_img,
            width=30,
            height=30,
            font=ctk.CTkFont(size=18),
            fg_color="transparent",
            hover_color=COL_PANEL_2,
            text_color=COL_TEXT_DIM,
            command=self._open_settings,
        )
        if self._gear_img and self._gear_hover_img:
            gear.bind("<Enter>", lambda _e: gear.configure(image=self._gear_hover_img), add="+")
            gear.bind("<Leave>", lambda _e: gear.configure(image=self._gear_img), add="+")
        gear.pack(side="right", padx=(0, 0))
        Tooltip(gear, "Settings")

        self._section(side, "1 · Input image")
        ctk.CTkButton(
            side, text="Choose image…", fg_color=COL_RED, hover_color=COL_RED_HOVER,
            height=40, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._choose_image,
        ).pack(fill="x", padx=18, pady=(0, 8))

        self.input_info = ctk.CTkLabel(
            side, text="No image selected.", text_color=COL_TEXT_DIM,
            justify="left", anchor="w",
        )
        self.input_info.pack(fill="x", padx=18)

        # Resize --------------------------------------------------------------
        self._section(side, "2 · Downscale before upload")
        self.resize_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            side, text="Reduce resolution first",
            variable=self.resize_var, command=self._on_resize_toggle,
            fg_color=COL_RED, hover_color=COL_RED_HOVER, text_color=COL_TEXT,
        ).pack(anchor="w", padx=18, pady=(0, 6))

        edge_row = ctk.CTkFrame(side, fg_color="transparent")
        edge_row.pack(fill="x", padx=18)
        ctk.CTkLabel(edge_row, text="Max. long edge (px):", text_color=COL_TEXT_DIM).pack(side="left")
        self.edge_entry = ctk.CTkEntry(
            edge_row, width=90, fg_color=COL_PANEL_2, border_color=COL_BORDER,
        )
        self.edge_entry.insert(0, "1024")
        self.edge_entry.pack(side="right")
        self.edge_entry.bind("<KeyRelease>", lambda _e: self._recompute_sent())

        self.resize_info = ctk.CTkLabel(
            side, text="", text_color=COL_TEXT_DIM, justify="left", anchor="w",
        )
        self.resize_info.pack(fill="x", padx=18, pady=(4, 0))

        # Model ---------------------------------------------------------------
        self._section(side, "3 · Vector model")
        label_row = ctk.CTkFrame(side, fg_color="transparent")
        label_row.pack(fill="x", padx=18)
        ctk.CTkLabel(label_row, text="Model", text_color=COL_TEXT_DIM).pack(side="left")
        info_icon = ctk.CTkLabel(
            label_row, text=" ⓘ", text_color=COL_RED, cursor="hand2",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        info_icon.pack(side="left")
        self._model_info_icon = info_icon
        self._model_tooltip = Tooltip(info_icon, self._models_tooltip_text())

        self.model_menu = ctk.CTkOptionMenu(
            side, values=MODEL_LABELS, command=self._on_model_change,
            fg_color=COL_PANEL_2, button_color=COL_RED, button_hover_color=COL_RED_HOVER,
        )
        self.model_menu.pack(fill="x", padx=18, pady=(2, 2))

        self.model_caption = ctk.CTkLabel(
            side, text="", text_color=COL_TEXT_DIM, justify="left", anchor="w",
            font=ctk.CTkFont(size=11), wraplength=330,
        )
        self.model_caption.pack(fill="x", padx=18, pady=(0, 8))

        # Minify (SVGO) -------------------------------------------------------
        self._section(side, "4 · Minify SVG (SVGO)")
        self.minify_var = ctk.BooleanVar(value=bool(self.config_ref["minify_enabled"]))
        min_row = ctk.CTkFrame(side, fg_color="transparent")
        min_row.pack(fill="x", padx=18, pady=(0, 2))
        self.minify_check = ctk.CTkCheckBox(
            min_row, text="Optimize with SVGO",
            variable=self.minify_var, command=self._on_minify_toggle,
            fg_color=COL_RED, hover_color=COL_RED_HOVER, text_color=COL_TEXT,
        )
        self.minify_check.pack(side="left")
        self._svgo_gear_img = load_svg_icon(ICON_SETTINGS_PATH, size=(20, 20), color=COL_TEXT_DIM)
        self._svgo_gear_hover = load_svg_icon(ICON_SETTINGS_PATH, size=(20, 20), color=COL_TEXT)
        self.svgo_cfg_btn = ctk.CTkButton(
            min_row, text="" if self._svgo_gear_img else "⚙", image=self._svgo_gear_img,
            width=30, height=30, fg_color="transparent", hover_color=COL_PANEL_2,
            text_color=COL_TEXT_DIM, command=self._open_svgo_settings,
        )
        if self._svgo_gear_img and self._svgo_gear_hover:
            self.svgo_cfg_btn.bind("<Enter>", lambda _e: self.svgo_cfg_btn.configure(image=self._svgo_gear_hover), add="+")
            self.svgo_cfg_btn.bind("<Leave>", lambda _e: self.svgo_cfg_btn.configure(image=self._svgo_gear_img), add="+")
        self.svgo_cfg_btn.pack(side="right")
        Tooltip(self.svgo_cfg_btn, "SVGO settings")

        self.minify_info = ctk.CTkLabel(
            side, text="", text_color=COL_TEXT_DIM, justify="left", anchor="w",
            font=ctk.CTkFont(size=11), wraplength=330,
        )
        self.minify_info.pack(fill="x", padx=18, pady=(2, 0))

        # Convert -------------------------------------------------------------
        self._section(side, "5 · Convert")
        self.convert_btn = ctk.CTkButton(
            side, text="Convert to SVG", fg_color=COL_RED, hover_color=COL_RED_HOVER,
            text_color_disabled="#FFFFFF",
            height=46, font=ctk.CTkFont(size=15, weight="bold"),
            command=self._convert,
        )
        self.convert_btn.pack(fill="x", padx=18, pady=(0, 6))

        self.progress = ctk.CTkProgressBar(side, progress_color=COL_RED, mode="determinate")
        self.progress.pack(fill="x", padx=18)
        self.progress.set(0)

        self.status = ctk.CTkLabel(side, text="Ready.", text_color=COL_TEXT_DIM, anchor="w")
        self.status.pack(fill="x", padx=18, pady=(4, 0))

        self.save_btn = ctk.CTkButton(
            side, text="Save SVG as…", fg_color=COL_PANEL_2, hover_color=COL_BORDER,
            height=38, command=self._save_svg, state="disabled",
        )
        self.save_btn.pack(fill="x", padx=18, pady=(10, 18))

    def _build_preview(self) -> None:
        right = ctk.CTkFrame(self, fg_color=COL_BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        # Toggle bar
        bar = ctk.CTkFrame(right, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        self.toggle_btn = ctk.CTkSegmentedButton(
            bar, values=["Original", "SVG", "Minified"], command=self._on_toggle,
            selected_color=COL_RED, selected_hover_color=COL_RED_HOVER,
            unselected_color=COL_PANEL_2, fg_color=COL_PANEL,
        )
        self.toggle_btn.set("Original")
        self.toggle_btn.pack(side="left")

        self.dims_label = ctk.CTkLabel(bar, text="", text_color=COL_TEXT_DIM)
        self.dims_label.pack(side="right")

        # Preview canvas
        self.preview_frame = ctk.CTkFrame(
            right, fg_color=COL_PANEL, corner_radius=12, border_width=1,
            border_color=COL_BORDER,
        )
        self.preview_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=8)
        self.preview_frame.grid_propagate(False)
        self.preview_frame.grid_columnconfigure(0, weight=1)
        self.preview_frame.grid_rowconfigure(0, weight=1)

        self.preview_label = ctk.CTkLabel(self.preview_frame, text="", fg_color="transparent")
        self.preview_label.grid(row=0, column=0)

        # Size comparison bar
        info = ctk.CTkFrame(right, fg_color=COL_PANEL, corner_radius=12)
        info.grid(row=2, column=0, sticky="ew", padx=24, pady=(8, 20))
        info.grid_columnconfigure((0, 1, 2), weight=1)
        self.size_in = self._stat(info, 0, "Input")
        self.size_sent = self._stat(info, 1, "Uploaded")
        self.size_out = self._stat(info, 2, "Output SVG")

    def _stat(self, parent, col: int, title: str) -> ctk.CTkLabel:
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="nsew", padx=16, pady=14)
        ctk.CTkLabel(
            cell, text=title.upper(), text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(anchor="w")
        val = ctk.CTkLabel(
            cell, text="–", text_color=COL_TEXT,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        val.pack(anchor="w")
        return val

    def _section(self, parent, text: str) -> None:
        ctk.CTkLabel(
            parent, text=text.upper(), text_color=COL_RED,
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).pack(fill="x", padx=18, pady=(16, 6))

    # ---- model helpers -------------------------------------------------- #
    @staticmethod
    def _models_tooltip_text() -> str:
        lines = ["Vector models\n"]
        for m in MODELS:
            lines.append(f"• {m['label']} — {m['price']} / image")
            lines.append(f"   {m['desc']}\n")
        return "\n".join(lines).rstrip()

    def _on_model_change(self, label: str) -> None:
        m = MODEL_BY_LABEL.get(label, MODELS[0])
        self.model_caption.configure(text=f"{m['price']} per image · {m['desc']}")

    # ---- defaults ------------------------------------------------------- #
    def apply_defaults(self) -> None:
        m = MODEL_BY_ID.get(self.config_ref["default_model"], MODELS[0])
        self.model_menu.set(m["label"])
        self._on_model_change(m["label"])

    # ---- image selection ------------------------------------------------ #
    def _choose_image(self) -> None:
        path = filedialog.askopenfilename(title="Choose image", filetypes=SUPPORTED_INPUT)
        if path:
            self._load_image_path(path)

    def _load_image_path(self, path: str) -> None:
        p = Path(str(path).strip().strip("{}"))
        try:
            img = Image.open(p)
            img.load()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open image:\n{p}\n\n{exc}")
            return

        self.input_path = p
        self.original_img = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img
        self.svg_raw = None
        self.svg_min = None
        self.svg_bytes = None
        self.svg_pil = None
        self.showing_svg = False
        self.view = "Original"
        self.minify_info.configure(text="", text_color=COL_TEXT_DIM)
        self.save_btn.configure(state="disabled")
        self.toggle_btn.set("Original")
        self.size_out.configure(text="–")

        in_size = self.input_path.stat().st_size
        w, h = self.original_img.size
        self.input_info.configure(
            text=f"{self.input_path.name}\n{w} × {h} px · {human_size(in_size)}"
        )
        self.size_in.configure(text=human_size(in_size))
        self._recompute_sent()
        self._update_preview()
        self.status.configure(text="Image loaded. Ready to convert.")

    def _on_resize_toggle(self) -> None:
        self._recompute_sent()

    def _recompute_sent(self) -> None:
        """Compute the (possibly downscaled) image that will be uploaded."""
        if self.original_img is None:
            self.resize_info.configure(text="")
            self.size_sent.configure(text="–")
            self.sent_img = None
            self.sent_bytes_len = None
            return

        img = self.original_img
        if self.resize_var.get():
            try:
                max_edge = max(MIN_EDGE, int(self.edge_entry.get()))
            except ValueError:
                max_edge = 1024
            w, h = img.size
            long_edge, short_edge = max(w, h), min(w, h)
            if long_edge > max_edge:
                scale = max_edge / long_edge
                # Keep the shorter edge >= MIN_EDGE (Recraft's minimum).
                if short_edge * scale < MIN_EDGE:
                    scale = MIN_EDGE / short_edge
                scale = min(scale, 1.0)
                if scale < 1.0:
                    new = (max(1, round(w * scale)), max(1, round(h * scale)))
                    img = self.original_img.resize(new, Image.LANCZOS)

        self.sent_img = img
        # Encode to PNG to measure upload size.
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        self.sent_bytes_len = buf.tell()

        w, h = img.size
        self.size_sent.configure(text=human_size(self.sent_bytes_len))
        if min(w, h) < MIN_EDGE:
            self.resize_info.configure(
                text=f"⚠ {w} × {h} px — shorter edge is below {MIN_EDGE} px; "
                     f"Recraft may reject this image.",
                text_color=COL_RED,
            )
        elif self.resize_var.get():
            self.resize_info.configure(
                text=f"Will upload {w} × {h} px  ({human_size(self.sent_bytes_len)}).",
                text_color=COL_TEXT_DIM,
            )
        else:
            self.resize_info.configure(text=f"No downscaling · {w} × {h} px", text_color=COL_TEXT_DIM)

    # ---- conversion ----------------------------------------------------- #
    def _convert(self) -> None:
        if self.original_img is None:
            messagebox.showinfo(APP_NAME, "Please choose an image first.")
            return
        if not self.config_ref["api_key"].strip():
            messagebox.showwarning(APP_NAME, "No API key set.\nOpen Settings and add your Recraft API key.")
            self._open_settings()
            return

        self._recompute_sent()
        img = self.sent_img or self.original_img
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        image_bytes = buf.getvalue()
        filename = (self.input_path.stem if self.input_path else "image") + ".png"
        model = MODEL_BY_LABEL[self.model_menu.get()]["id"]

        self.convert_btn.configure(state="disabled", text="Converting…")
        self.save_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()
        self.status.configure(text="Contacting Recraft…")

        def worker() -> None:
            try:
                client = RecraftClient(self.config_ref["api_key"])
                svg = client.vectorize(image_bytes, filename, model)
                self.after(0, lambda: self._on_success(svg))
            except RecraftError as exc:
                self.after(0, lambda m=str(exc): self._on_error(m))
            except Exception:  # pragma: no cover
                tb = traceback.format_exc()
                self.after(0, lambda: self._on_error(tb))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_progress(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress.set(0)

    def _on_success(self, svg: bytes) -> None:
        self._reset_progress()
        self.convert_btn.configure(state="normal", text="Convert to SVG")
        self.svg_raw = svg
        self.svg_min = None
        self.svg_bytes = svg
        self.save_btn.configure(state="normal")

        self.showing_svg = True
        self.view = "SVG"
        self.toggle_btn.set("SVG")

        if self.minify_var.get():
            # Optimize first; the preview is refreshed once SVGO returns.
            self.status.configure(text="Done. Optimizing with SVGO…")
            self._render_current_svg()
            self._apply_minify()
        else:
            self._render_current_svg()
            self.minify_info.configure(
                text=f"Original SVG · {human_size(len(svg))}", text_color=COL_TEXT_DIM
            )
            if self.svg_pil is None:
                self.status.configure(text="Done. SVG ready (preview could not be rendered).")
            else:
                self.status.configure(text="Done. SVG ready.")

    def _on_error(self, msg: str) -> None:
        self._reset_progress()
        self.convert_btn.configure(state="normal", text="Convert to SVG")
        self.status.configure(text="Conversion failed.")
        messagebox.showerror(APP_NAME, msg)

    # ---- minify (SVGO) -------------------------------------------------- #
    def _render_current_svg(self) -> None:
        """Rasterize ``self.svg_bytes`` for the preview and update the size stat."""
        if self.svg_bytes is None:
            self.svg_pil = None
        else:
            try:
                self.svg_pil = render_svg_to_pil(self.svg_bytes)
            except Exception:
                self.svg_pil = None
            self.size_out.configure(text=human_size(len(self.svg_bytes)))
        self._update_preview()

    def _on_minify_toggle(self) -> None:
        enabled = self.minify_var.get()
        self.config_ref["minify_enabled"] = enabled
        self.config_ref.save()
        if self.svg_raw is None:
            return
        if enabled:
            self._apply_minify()
        else:
            self.svg_min = None
            if self.view == "Minified":
                self.toggle_btn.set("SVG")
                self._show_variant("SVG")
            self.minify_info.configure(
                text=f"Original SVG · {human_size(len(self.svg_raw))}",
                text_color=COL_TEXT_DIM,
            )
            self.status.configure(text="Minify off.")

    def _open_svgo_settings(self) -> None:
        if self._svgo_dialog is not None and self._svgo_dialog.winfo_exists():
            self._svgo_dialog.focus()
            return
        # Opening the settings implies wanting to see the optimized result.
        if not self.minify_var.get():
            self.minify_var.set(True)
            self._on_minify_toggle()
        self._svgo_dialog = SvgoSettingsDialog(self)

    def schedule_minify(self, delay: int = 250) -> None:
        """Debounced re-optimize, used by the live settings dialog."""
        if self._minify_after is not None:
            try:
                self.after_cancel(self._minify_after)
            except Exception:
                pass
        self._minify_after = self.after(delay, self._apply_minify)

    def _apply_minify(self) -> None:
        self._minify_after = None
        if self.svg_raw is None:
            return
        if not self.minify_var.get():
            return
        self._minify_seq += 1
        seq = self._minify_seq
        settings = json.loads(json.dumps(self.minify_settings))  # snapshot
        raw = self.svg_raw
        self.minify_info.configure(text="Optimizing with SVGO…", text_color=COL_TEXT_DIM)

        if self._svgo_pool is None:
            self._svgo_pool = ProcessPoolExecutor(max_workers=1)
        pool = self._svgo_pool

        def worker() -> None:
            try:
                out = pool.submit(engine.optimize_job, raw, settings).result()
                self.after(0, lambda: self._on_minify_done(seq, out))
            except engine.SvgoError as exc:
                self.after(0, lambda m=str(exc): self._on_minify_error(seq, m))
            except Exception:
                if isinstance(sys.exc_info()[1], BrokenProcessPool):
                    self._svgo_pool = None  # recreated on the next run
                tb = traceback.format_exc()
                self.after(0, lambda: self._on_minify_error(seq, tb))

        threading.Thread(target=worker, daemon=True).start()

    def _on_minify_done(self, seq: int, out: bytes) -> None:
        if seq != self._minify_seq:
            return  # superseded by a newer request
        self.svg_min = out
        self.showing_svg = True
        self.toggle_btn.set("Minified")
        self._show_variant("Minified")
        raw_n = len(self.svg_raw) if self.svg_raw else 0
        out_n = len(out)
        pct = (1 - out_n / raw_n) * 100 if raw_n else 0.0
        sign = "−" if pct >= 0 else "+"
        msg = (f"SVGO · {human_size(out_n)}   "
               f"{sign}{abs(pct):.1f}%  (was {human_size(raw_n)})")
        self.minify_info.configure(text=msg, text_color=COL_TEXT)
        self.status.configure(text="Done. Optimized SVG ready.")
        if self._svgo_dialog is not None and self._svgo_dialog.winfo_exists():
            self._svgo_dialog.set_result(msg)

    def _on_minify_error(self, seq: int, msg: str) -> None:
        if seq != self._minify_seq:
            return
        self.svg_min = None
        self.toggle_btn.set("SVG")
        self._show_variant("SVG")  # fall back to the unmodified SVG
        self.minify_info.configure(
            text="⚠ SVGO failed — showing original SVG.", text_color=COL_RED
        )
        self.status.configure(text="SVGO error.")
        if self._svgo_dialog is not None and self._svgo_dialog.winfo_exists():
            self._svgo_dialog.set_result("Error: " + msg.splitlines()[-1][:140])

    def persist_minify_settings(self) -> None:
        self.config_ref["minify_settings"] = self.minify_settings
        self.config_ref.save()

    # ---- preview -------------------------------------------------------- #
    def _on_toggle(self, value: str) -> None:
        if value == "Original":
            self.view = value
            self.showing_svg = False
            self._update_preview()
        else:
            self._show_variant(value)

    def _show_variant(self, name: str) -> None:
        self.view = name
        self.showing_svg = True
        if self.svg_raw is None:
            self.svg_bytes = None
        elif name == "Minified":
            self.svg_bytes = self.svg_min
        else:
            self.svg_bytes = self.svg_raw
        self._render_current_svg()

    def _update_preview(self) -> None:
        img = self.svg_pil if self.showing_svg else self.original_img

        if img is not None:
            disp = self._fit(img, self.PREVIEW_W, self.PREVIEW_H)
        elif self.showing_svg and self.view == "Minified" and self.svg_min is None:
            disp = self._placeholder_image("No minified SVG.\nEnable 'Optimize with SVGO'.")
        elif self.showing_svg and self.svg_bytes is not None:
            disp = self._placeholder_image("SVG converted successfully.\n(Preview could not be rendered.)")
        elif self.showing_svg:
            disp = self._placeholder_image("Convert an image to see the SVG here.")
        else:
            disp = self._placeholder_image("Choose an image, or drag && drop\none onto this area.")

        # Always hand the label a real image so it never references a freed one.
        self._preview_cache = ctk.CTkImage(light_image=disp, dark_image=disp, size=disp.size)
        self.preview_label.configure(image=self._preview_cache, text="")

        if self.showing_svg and self.svg_bytes is not None:
            w, h = svg_dimensions(self.svg_bytes)
            self.dims_label.configure(
                text=(f"SVG · {w} × {h}" if w and h else "SVG") + f" · {human_size(len(self.svg_bytes))}"
            )
        elif img is not None:
            w, h = img.size
            self.dims_label.configure(text=f"Original · {w} × {h} px")
        else:
            self.dims_label.configure(text="")

    def _placeholder_image(self, text: str) -> Image.Image:
        """Render centered placeholder text onto a panel-coloured canvas."""
        w, h = self.PREVIEW_W, self.PREVIEW_H
        bg = tuple(int(COL_PANEL[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        fg = tuple(int(COL_TEXT_DIM[i:i + 2], 16) for i in (1, 3, 5)) + (255,)
        img = Image.new("RGBA", (w, h), bg)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("segoeui.ttf", 20)
        except Exception:
            font = ImageFont.load_default()
        lines = text.replace("&&", "&").split("\n")
        line_h = (font.getbbox("Ag")[3] - font.getbbox("Ag")[1]) + 8
        total = line_h * len(lines)
        y = (h - total) // 2
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            lw = bbox[2] - bbox[0]
            draw.text(((w - lw) // 2, y), line, font=font, fill=fg)
            y += line_h
        return img

    @staticmethod
    def _fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
        w, h = img.size
        scale = min(max_w / w, max_h / h, 1.0)
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        return img

    # ---- save ----------------------------------------------------------- #
    def _save_svg(self) -> None:
        if not self.svg_bytes:
            return
        stem = self.input_path.stem if self.input_path else "vectorized"
        optimized = self.minify_var.get() and self.svg_bytes is self.svg_min
        default_name = stem + (".min.svg" if optimized else ".svg")
        path = filedialog.asksaveasfilename(
            title="Save SVG as",
            defaultextension=".svg",
            initialfile=default_name,
            initialdir=self.config_ref["download_dir"] or str(Path.home()),
            filetypes=[("SVG file", "*.svg"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_bytes(self.svg_bytes)
            self.status.configure(text=f"Saved: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save file:\n{exc}")

    # ---- settings ------------------------------------------------------- #
    def _open_settings(self) -> None:
        SettingsDialog(self, self.config_ref)


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    ctk.set_appearance_mode("dark")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
