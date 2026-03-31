import os
import queue
import re
import shutil
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import customtkinter as ctk
import pandas as pd
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_TRACKER_CSV = _SCRIPT_DIR / "juice_unreleased_final.csv"
_ERROR_LOG = _SCRIPT_DIR / "error_log.txt"
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac"}
_EXT_QUALITY = {".mp3": 1, ".m4a": 2, ".wav": 3, ".flac": 4}
_EXACT_THRESHOLD = 0.95
_PROBABLE_THRESHOLD = 0.80
_BATCH_SIZE = 50


def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return name or "untitled"


def _short_path(path: str, max_len: int = 38) -> str:
    if len(path) <= max_len:
        return path
    keep = max_len - 3
    left = keep // 2
    right = keep - left
    return f"{path[:left]}...{path[-right:]}"


def _log_error(context: str, exc: Exception | str) -> None:
    try:
        with _ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{context}] {exc}\n")
    except Exception:
        pass


def sanitize_title(title: str) -> str:
    """Remove immediate repeated phrases and extra symbols/spaces."""
    if not title:
        return ""
    t = str(title)
    t = re.sub(r"\s*\|\s*", " ", t)
    t = re.sub(r"[^\w\s\-\(\)\[\]]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    words = t.split()
    if len(words) >= 2:
        half = len(words) // 2
        if len(words) % 2 == 0 and words[:half] == words[half:]:
            t = " ".join(words[:half])
    return t.strip()


def _normalize_part_variants(s: str) -> str:
    s = re.sub(r"\bpt\.?\s*(\d+)\b", r"part\1", s, flags=re.I)
    s = re.sub(r"\bpart\s*(\d+)\b", r"part\1", s, flags=re.I)
    return s


def comparison_key(title: str) -> str:
    """Lowercase, no spaces, no bracketed text, normalized part variants."""
    t = sanitize_title(title).lower()
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = _normalize_part_variants(t)
    t = re.sub(r"[^a-z0-9]", "", t)
    return t


def _norm_key(s: str) -> str:
    if not s:
        return ""
    s = sanitize_title(s).lower()
    s = _normalize_part_variants(s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _mime_for_image(path: Path) -> str:
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


@dataclass
class CatalogSong:
    title: str
    era: str
    norm: str
    cmp_key: str


@dataclass
class MatchInfo:
    song: CatalogSong | None
    score: float
    status: str  # Matched | Probable Match | Missing


@dataclass
class RowItem:
    title: str
    era: str
    score_text: str
    status: str
    local_path: Path | None = None
    matched_song: CatalogSong | None = None
    can_link: bool = False
    file_path_text: str = ""
    local_display: str = ""
    score_value: float = 0.0


def _truncate_text(text: str, max_len: int = 38) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _match_score(file_name: str, song: CatalogSong) -> float:
    file_norm = _norm_key(file_name)
    file_cmp = comparison_key(file_name)
    if not file_norm or not song.norm:
        return 0.0
    if file_cmp and song.cmp_key and file_cmp == song.cmp_key:
        return 1.0
    if song.cmp_key and song.cmp_key in file_cmp:
        return 0.97
    if file_cmp and file_cmp in song.cmp_key:
        return 0.94
    ratio1 = SequenceMatcher(None, file_norm, song.norm).ratio()
    ratio2 = SequenceMatcher(None, file_cmp, song.cmp_key).ratio() if file_cmp and song.cmp_key else 0.0
    file_tokens = set(file_norm.split())
    song_tokens = set(song.norm.split())
    overlap = len(file_tokens & song_tokens) / max(1, len(song_tokens))
    return max(ratio1, ratio2, overlap * 0.95)


def best_catalog_match(local_file: Path, catalog: list[CatalogSong]) -> MatchInfo:
    stem = local_file.stem
    best_song = None
    best = 0.0
    for song in catalog:
        score = _match_score(stem, song)
        if score > best:
            best = score
            best_song = song
    if best_song is None:
        return MatchInfo(song=None, score=0.0, status="Missing")
    if best >= _EXACT_THRESHOLD:
        return MatchInfo(song=best_song, score=best, status="Matched")
    if best >= _PROBABLE_THRESHOLD:
        return MatchInfo(song=best_song, score=best, status="Probable Match")
    return MatchInfo(song=None, score=best, status="Missing")


def ensure_easyid3(path: Path) -> EasyID3:
    try:
        return EasyID3(str(path))
    except ID3NoHeaderError:
        EasyID3().save(str(path))
        return EasyID3(str(path))


def write_core_tags(path: Path, title: str, era: str) -> None:
    audio = ensure_easyid3(path)
    audio["artist"] = "Juice WRLD"
    audio["album"] = era or "Unknown Era"
    audio["title"] = title
    audio.save()


def embed_cover_art(path: Path, image_path: Path) -> None:
    data = image_path.read_bytes()
    mime = _mime_for_image(image_path)
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(str(path))


class LibraryManagerApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        ctk.set_widget_scaling(1.08)
        self.title("Juice WRLD Library Manager")
        self.geometry("1220x760")
        self.configure(fg_color="#111418")

        self.csv_path = _DEFAULT_TRACKER_CSV
        self.catalog: list[CatalogSong] = []
        self.local_songs: list[Path] = []
        self.match_map: dict[Path, MatchInfo] = {}
        self.confirmed_links: dict[Path, CatalogSong] = {}
        self.output_folder: Path | None = None

        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._busy = False
        self._current_view = "all"
        self._all_rows: list[RowItem] = []
        self._filtered_rows: list[RowItem] = []
        self._render_index = 0
        self._items_per_page = 50
        self._current_page = 1
        self._total_pages = 1
        self._sort_az_ascending = True
        self._sort_status_missing_first = False
        self._pulse_tick = 0
        self._thumb_cache: dict[str, ctk.CTkImage] = {}

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0, fg_color="#171b22")
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)

        ctk.CTkLabel(self.sidebar, text="Library Manager", font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=16, pady=(24, 14), anchor="w"
        )
        self.scan_btn = ctk.CTkButton(self.sidebar, text="Scan Library", command=self.scan_library)
        self.scan_btn.pack(fill="x", padx=16, pady=6)
        self.show_all_btn = ctk.CTkButton(self.sidebar, text="Show All Local Songs", command=self.show_all_local_songs)
        self.show_all_btn.pack(fill="x", padx=16, pady=6)
        self.missing_btn = ctk.CTkButton(self.sidebar, text="Show Missing Tracks", command=self.show_missing_tracks)
        self.missing_btn.pack(fill="x", padx=16, pady=6)
        self.organize_btn = ctk.CTkButton(self.sidebar, text="Auto-Organize All", command=self.auto_organize_all)
        self.organize_btn.pack(fill="x", padx=16, pady=6)
        self.output_btn = ctk.CTkButton(self.sidebar, text="Select Output Folder", command=self.select_output_folder)
        self.output_btn.pack(fill="x", padx=16, pady=6)
        self.export_btn = ctk.CTkButton(self.sidebar, text="Export Organized Library", command=self.export_organized_library)
        self.export_btn.pack(fill="x", padx=16, pady=6)
        self.sync_btn = ctk.CTkButton(self.sidebar, text="Sync All Match Titles", command=self.sync_all_match_titles)
        self.sync_btn.pack(fill="x", padx=16, pady=6)
        self.edit_csv_btn = ctk.CTkButton(self.sidebar, text="Edit Source CSV", command=self.open_csv_editor)
        self.edit_csv_btn.pack(fill="x", padx=16, pady=6)
        for btn in (
            self.scan_btn,
            self.show_all_btn,
            self.missing_btn,
            self.organize_btn,
            self.output_btn,
            self.export_btn,
            self.sync_btn,
            self.edit_csv_btn,
        ):
            btn.configure(fg_color="#2b5fff", hover_color="#4677ff")
        ctk.CTkLabel(
            self.sidebar,
            text=f"CSV source:\n{self.csv_path.name}",
            justify="left",
            anchor="w",
            wraplength=210,
        ).pack(fill="x", padx=16, pady=(14, 8))

        self.main = ctk.CTkFrame(self, fg_color="#111418")
        self.main.grid(row=0, column=1, sticky="nsew", padx=(8, 12), pady=(10, 6))
        self.main.grid_rowconfigure(3, weight=1)
        self.main.grid_columnconfigure(0, weight=1)

        self.view_title = ctk.CTkLabel(self.main, text="Welcome", font=ctk.CTkFont(size=18, weight="bold"))
        self.view_title.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        self.progress = ctk.CTkProgressBar(self.main, mode="indeterminate")
        self.progress.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.progress.grid_remove()

        self.controls = ctk.CTkFrame(self.main)
        self.controls.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.controls.grid_columnconfigure(5, weight=1)

        self.sort_var = ctk.StringVar(value="Sort by: A-Z")
        self.sort_menu = ctk.CTkOptionMenu(
            self.controls,
            values=[
                "Sort by: A-Z",
                "Sort by: Z-A",
                "Sort by: Era",
                "Status: Matched Top",
                "Status: Missing Top",
                "Match Score (High to Low)",
                "Match Score (Low to High)",
            ],
            variable=self.sort_var,
            command=self.on_sort_selected,
            width=170,
        )
        self.sort_menu.grid(row=0, column=0, padx=6, pady=8)
        self.missing_mode_var = ctk.StringVar(value="Mismatched")
        self.missing_mode_switch = ctk.CTkSegmentedButton(
            self.controls,
            values=["Mismatched", "Strictly Missing"],
            variable=self.missing_mode_var,
            command=self._apply_filter_and_sort,
            width=220,
        )
        self.missing_mode_switch.grid(row=0, column=1, padx=6, pady=8)
        self.missing_mode_switch.grid_remove()
        ctk.CTkLabel(self.controls, text="Search:").grid(row=0, column=2, padx=(10, 2), pady=8)
        self.search_var = ctk.StringVar(value="")
        self.search_entry = ctk.CTkEntry(self.controls, textvariable=self.search_var, width=340)
        self.search_entry.grid(row=0, column=3, padx=(0, 8), pady=8, sticky="w")
        self.search_entry.bind("<KeyRelease>", self._on_search_changed)

        self.song_list = ctk.CTkScrollableFrame(self.main, label_text="Songs")
        self.song_list.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 8))

        self.list_center = ctk.CTkFrame(self.song_list, fg_color="transparent")
        self.list_center.pack(fill="both", expand=True)

        self.pagination = ctk.CTkFrame(self.main)
        self.pagination.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.pagination.grid_columnconfigure(4, weight=1)
        self.first_btn = ctk.CTkButton(self.pagination, width=70, text="First", command=self.go_first_page)
        self.prev_btn = ctk.CTkButton(self.pagination, width=70, text="Prev", command=self.go_prev_page)
        self.next_btn = ctk.CTkButton(self.pagination, width=70, text="Next", command=self.go_next_page)
        self.last_btn = ctk.CTkButton(self.pagination, width=70, text="Last", command=self.go_last_page)
        self.first_btn.grid(row=0, column=0, padx=4, pady=6)
        self.prev_btn.grid(row=0, column=1, padx=4, pady=6)
        self.next_btn.grid(row=0, column=2, padx=4, pady=6)
        self.last_btn.grid(row=0, column=3, padx=4, pady=6)
        

        self.spinner_overlay = ctk.CTkFrame(self.main, fg_color="transparent")
        self.spinner_overlay.grid(row=0, column=0, rowspan=5, sticky="nsew")
        self.spinner_overlay.grid_remove()
        self.spinner_overlay.configure(fg_color=("#111418", "#111418"))
        self.spinner_canvas = ctk.CTkCanvas(
            self.spinner_overlay, width=84, height=84, bg="#111418", highlightthickness=0
        )
        self.spinner_arc = self.spinner_canvas.create_arc(12, 12, 72, 72, start=0, extent=110, style="arc", width=6, outline="#4d7cff")
        self.spinner_status = ctk.CTkLabel(self.spinner_overlay, text="Processing...")
        self.spinner_canvas.place(relx=0.5, rely=0.46, anchor="center")
        self.spinner_status.place(relx=0.5, rely=0.54, anchor="center")

        self.status_var = ctk.StringVar(value="Ready")
        self.bottom_bar = ctk.CTkFrame(self, height=30, corner_radius=0)
        self.bottom_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.bottom_bar.grid_columnconfigure(0, weight=1)
        self.status_bar = ctk.CTkLabel(self.bottom_bar, textvariable=self.status_var, anchor="w")
        self.status_bar.grid(row=0, column=0, sticky="ew", padx=8)
        self.bottom_info = ctk.CTkLabel(self.bottom_bar, text="Page 1/1 | Total 0", anchor="e")
        self.bottom_info.grid(row=0, column=1, sticky="e", padx=8)

        self._load_catalog_safe()
        self._render_empty("Click 'Scan Library' to choose your music folder.")
        self._set_library_actions_enabled(False)
        self.after(100, self._drain_ui_queue)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in (
            self.scan_btn,
            self.show_all_btn,
            self.missing_btn,
            self.organize_btn,
            self.output_btn,
            self.export_btn,
            self.sync_btn,
        ):
            btn.configure(state=state)
        if busy:
            self.progress.grid()
            self.progress.start()
            self._animate_progress()
            self.song_list.grid_remove()
            self.pagination.grid_remove()
            self.spinner_overlay.grid()
            self.spinner_overlay.lift()
            self.spinner_overlay.bind("<Button-1>", lambda _e: "break")
            self._animate_spinner()
            if message:
                self._set_status(message)
        else:
            self.progress.stop()
            self.progress.grid_remove()
            self.spinner_overlay.grid_remove()
            self.song_list.grid()
            self.pagination.grid()

    def _animate_progress(self) -> None:
        if not self._busy:
            return
        colors = ["#4d7cff", "#6f8fff", "#8ea6ff", "#6f8fff"]
        self.progress.configure(progress_color=colors[self._pulse_tick % len(colors)])
        self._pulse_tick += 1
        self.after(220, self._animate_progress)

    def _animate_spinner(self) -> None:
        if not self._busy:
            return
        start = (self._pulse_tick * 6) % 360
        self.spinner_canvas.itemconfigure(self.spinner_arc, start=start)
        self.spinner_status.configure(text=self.status_var.get())
        self._pulse_tick += 1
        self.spinner_canvas.update_idletasks()
        self.after(10, self._animate_spinner)

    def _set_library_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (self.show_all_btn, self.missing_btn, self.organize_btn, self.export_btn, self.sync_btn, self.edit_csv_btn):
            btn.configure(state=state)

    def select_output_folder(self) -> None:
        folder = ctk.filedialog.askdirectory(title="Select export output folder")
        if not folder:
            return
        self.output_folder = Path(folder)
        self._set_status(f"Output folder set: {self.output_folder}")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.update_idletasks()

    def _run_background(self, target, *args) -> None:
        if self._busy:
            return
        self._set_busy(True, "Processing...")
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                msg = self._ui_queue.get_nowait()
                self._handle_ui_message(msg)
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def _handle_ui_message(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "status":
            self._set_status(msg[1])
        elif kind == "rows":
            self._set_busy(False)
            self._current_view = msg[4]
            self.view_title.configure(text=msg[1])
            self._all_rows = msg[2]
            if self._current_view == "missing":
                self.missing_mode_switch.grid()
            else:
                self.missing_mode_switch.grid_remove()
            self._apply_filter_and_sort()
            self._set_status(msg[3])
        elif kind == "scan_done":
            self._set_busy(False)
            self._set_library_actions_enabled(True)
            self.view_title.configure(text="Library Scan")
            self._render_empty(msg[1])
            self._set_status(msg[2])
        elif kind == "done":
            self._set_busy(False)
            self._set_status(msg[1])
        elif kind == "error":
            self._set_busy(False)
            self._set_status(msg[1])

    def _load_catalog_safe(self) -> None:
        if not self.csv_path.is_file():
            self._set_status(f"Missing CSV: {self.csv_path}")
            return
        try:
            df = pd.read_csv(self.csv_path, dtype=str)
            cols = {str(c).strip().lower(): c for c in df.columns}
            if "song title" not in cols or "era" not in cols:
                raise ValueError("CSV must contain 'Song Title' and 'Era' columns.")
            tcol = cols["song title"]
            ecol = cols["era"]
            out: list[CatalogSong] = []
            seen: set[str] = set()
            for _, row in df.iterrows():
                try:
                    title = str(row[tcol]).strip() if pd.notna(row[tcol]) else ""
                    era = str(row[ecol]).strip() if pd.notna(row[ecol]) else ""
                    if not title:
                        continue
                    norm = _norm_key(title)
                    cmp_key = comparison_key(title)
                    if not norm or cmp_key in seen:
                        continue
                    seen.add(cmp_key)
                    out.append(CatalogSong(title=sanitize_title(title), era=era, norm=norm, cmp_key=cmp_key))
                except Exception as row_exc:
                    _log_error("csv_row_skip", row_exc)
                    continue
            self.catalog = out
            self._set_status(f"Loaded catalog: {len(self.catalog)} songs")
        except Exception as exc:
            self.catalog = []
            _log_error("csv_load", exc)
            self._set_status(f"CSV error: {exc}")

    def _clear_song_list(self) -> None:
        for child in self.list_center.winfo_children():
            child.destroy()

    def _render_empty(self, message: str) -> None:
        self._clear_song_list()
        ctk.CTkLabel(self.list_center, text=message, justify="left", anchor="center", wraplength=850).pack(
            fill="x", padx=12, pady=12
        )
        self.bottom_info.configure(text="Page 1/1 | Total 0")

    def _apply_filter_and_sort(self, _=None) -> None:
        query = _norm_key(self.search_var.get())
        rows = self._all_rows[:]
        if query:
            rows = [
                r
                for r in rows
                if query in _norm_key(r.title) or query in _norm_key(r.era) or query in _norm_key(r.file_path_text)
            ]

        if self._current_view == "missing":
            mode = self.missing_mode_var.get()
            if mode == "Strictly Missing":
                rows = [r for r in rows if r.status == "Missing" and r.score_value == 0.0]
            else:
                rows = [r for r in rows if r.status == "Probable Match" and 0.80 <= r.score_value < 0.95]

        sort_mode = self.sort_var.get()
        if sort_mode == "Sort by: Z-A":
            rows.sort(key=lambda r: _norm_key(r.title), reverse=True)
        elif sort_mode == "Sort by: Era":
            rows.sort(key=lambda r: (_norm_key(r.era), _norm_key(r.title)))
        elif sort_mode == "Status: Missing Top":
            rows.sort(key=lambda r: {"Missing": 0, "Probable Match": 1, "Matched": 2}.get(r.status, 3))
        elif sort_mode == "Status: Matched Top":
            rows.sort(key=lambda r: {"Matched": 0, "Probable Match": 1, "Missing": 2}.get(r.status, 3))
        elif sort_mode == "Match Score (High to Low)":
            rows.sort(key=lambda r: r.score_value, reverse=True)
        elif sort_mode == "Match Score (Low to High)":
            rows.sort(key=lambda r: r.score_value)
        else:
            rows.sort(key=lambda r: _norm_key(r.title))

        self._filtered_rows = rows
        self._current_page = 1
        self._clear_song_list()
        if not rows:
            self._render_empty("No rows match current search/filter.")
            return
        self._total_pages = max(1, (len(rows) + self._items_per_page - 1) // self._items_per_page)
        self._render_page()

    def _render_page(self) -> None:
        self._clear_song_list()
        if not self._filtered_rows:
            self._render_empty("No rows match current search/filter.")
            return
        start = (self._current_page - 1) * self._items_per_page
        end = min(start + self._items_per_page, len(self._filtered_rows))
        page_rows = self._filtered_rows[start:end]
        available = max(1, self.song_list.winfo_width())
        cols = min(5, max(3, available // 240))
        for i, row in enumerate(page_rows):
            r = i // cols
            c = i % cols
            self._render_card(row, r, c)
        for c in range(cols):
            self.list_center.grid_columnconfigure(c, weight=1, uniform="cards")
        self.bottom_info.configure(text=f"Page {self._current_page}/{self._total_pages} | Total {len(self._filtered_rows)}")
        self.first_btn.configure(state="normal" if self._current_page > 1 else "disabled")
        self.prev_btn.configure(state="normal" if self._current_page > 1 else "disabled")
        self.next_btn.configure(state="normal" if self._current_page < self._total_pages else "disabled")
        self.last_btn.configure(state="normal" if self._current_page < self._total_pages else "disabled")
    def _get_card_image(self, row_data: RowItem) -> ctk.CTkImage | None:
        if Image is None:
            return None
        key = row_data.file_path_text + "/" + row_data.local_display
        img = None
        try:
            if row_data.local_path:
                tags = ID3(str(row_data.local_path))
                apics = tags.getall("APIC")
                if apics:
                    from io import BytesIO

                    pil = Image.open(BytesIO(apics[0].data)).convert("RGB").resize((120, 120))
                    img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(120, 120))
        except Exception:
            img = None
        if img is None:
            try:
                fallback = _SCRIPT_DIR / "missing.png"
                if fallback.is_file():
                    pil = Image.open(fallback).convert("RGB").resize((120, 120))
                    img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(120, 120))
            except Exception as exc:
                _log_error("missing_png_fallback", exc)
        if img:
            self._thumb_cache[key] = img
        return img

    def _render_card(self, row_data: RowItem, row: int, col: int) -> None:
        card = ctk.CTkFrame(self.list_center, width=220, height=280, corner_radius=12, border_width=1)
        card.grid(row=row, column=col, padx=10, pady=10, sticky="n")
        card.grid_propagate(False)
        art = self._get_card_image(row_data)
        if art:
            art_widget = ctk.CTkLabel(card, text="", image=art)
        else:
            art_widget = ctk.CTkLabel(card, text="♪", font=ctk.CTkFont(size=34, weight="bold"))
        art_widget.pack(pady=(10, 6))
        ctk.CTkLabel(card, text=f"Found: {_truncate_text(row_data.local_display or row_data.title, 30)}", wraplength=190).pack(padx=8)
        ctk.CTkLabel(
            card,
            text=f"Catalog: {_truncate_text(row_data.title, 30)}",
            wraplength=190,
            text_color="#79a6ff",
            font=ctk.CTkFont(weight="bold"),
        ).pack(padx=8, pady=(2, 0))
        badges = ctk.CTkFrame(card, fg_color="transparent")
        badges.pack(padx=8, pady=(4, 2))
        ctk.CTkLabel(
            badges,
            text=f"Era: {row_data.era}",
            fg_color="#2f3f58",
            corner_radius=8,
            padx=8,
            pady=2,
        ).pack(side="left", padx=3)
        if row_data.status == "Matched":
            score_color = "#2d7d46"
        elif row_data.status == "Probable Match":
            score_color = "#2a5f8a"
        else:
            score_color = "#7a3030"
        ctk.CTkLabel(badges, text=row_data.score_text, fg_color=score_color, corner_radius=8, padx=8, pady=2).pack(
            side="left", padx=3
        )
        if row_data.file_path_text:
            ctk.CTkLabel(
                card,
                text=f"Location: {_short_path(row_data.file_path_text)}",
                wraplength=190,
                text_color="gray60",
                font=ctk.CTkFont(size=11),
            ).pack(
                padx=8, pady=(0, 6)
            )
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(pady=(0, 8))
        if row_data.status == "Probable Match" and row_data.can_link and row_data.matched_song and row_data.local_path:
            ctk.CTkButton(
                actions, text="Update", width=72, command=lambda p=row_data.local_path, s=row_data.matched_song: self.update_match(p, s)
            ).pack(side="left", padx=4)
        if row_data.status == "Missing":
            ctk.CTkButton(actions, text="Register to CSV", width=110, command=lambda r=row_data: self.register_missing_track(r)).pack(
                side="left", padx=4
            )

        def _hover_on(_e=None):
            card.configure(border_color="#4d7cff")
            quick_edit.place(relx=0.92, rely=0.08, anchor="center")

        def _hover_off(_e=None):
            card.configure(border_color=("gray70", "gray30"))
            quick_edit.place_forget()

        quick_edit = ctk.CTkLabel(card, text="✎", width=24, height=24, fg_color="#3b6fff", corner_radius=12)

        card.bind("<Enter>", _hover_on)
        card.bind("<Leave>", _hover_off)
        if row_data.local_path:
            for w in (card, art_widget):
                w.bind("<Button-1>", lambda _e, p=row_data.local_path: self.open_song_editor(p))

    def _on_search_changed(self, _event=None) -> None:
        self._apply_filter_and_sort()

    def on_sort_selected(self, _choice: str) -> None:
        self._apply_filter_and_sort()

    def go_first_page(self) -> None:
        self._current_page = 1
        self._render_page()

    def go_prev_page(self) -> None:
        if self._current_page > 1:
            self._current_page -= 1
            self._render_page()

    def go_next_page(self) -> None:
        if self._current_page < self._total_pages:
            self._current_page += 1
            self._render_page()

    def go_last_page(self) -> None:
        self._current_page = self._total_pages
        self._render_page()

    def scan_library(self) -> None:
        folder = ctk.filedialog.askdirectory(title="Select your local music folder")
        if not folder:
            return
        self._run_background(self._worker_scan_library, Path(folder))

    def _worker_scan_library(self, folder: Path) -> None:
        try:
            best_by_stem: dict[str, Path] = {}
            for root, _, files in os.walk(folder):
                for name in files:
                    p = Path(root) / name
                    if name.startswith("._"):
                        continue
                    ext = p.suffix.lower()
                    if ext not in _AUDIO_EXTS:
                        continue
                    stem_key = _norm_key(p.stem)
                    current = best_by_stem.get(stem_key)
                    if current is None:
                        best_by_stem[stem_key] = p
                    else:
                        if _EXT_QUALITY.get(ext, 0) > _EXT_QUALITY.get(current.suffix.lower(), 0):
                            best_by_stem[stem_key] = p
            mp3s: list[Path] = list(best_by_stem.values())
            mp3s.sort(key=lambda p: p.name.lower())
            self.local_songs = mp3s
            self.match_map.clear()
            self.confirmed_links.clear()
            self._ui_queue.put(
                (
                    "scan_done",
                    f"Scanned {len(mp3s)} local MP3 files.\nNow choose a view from the sidebar.",
                    f"Scanned {len(mp3s)} tracks from {folder}",
                )
            )
        except Exception as exc:
            _log_error("scan_library", exc)
            self._ui_queue.put(("error", f"Scan failed: {exc}"))

    def _ensure_ready(self) -> bool:
        if not self.catalog:
            self._render_empty("Catalog CSV not loaded or missing required columns: Song Title and Era.")
            self._set_status("Catalog not loaded")
            return False
        if not self.local_songs:
            self._render_empty("No local songs loaded. Click 'Scan Library' first.")
            self._set_status("No local tracks loaded")
            return False
        return True

    def show_all_local_songs(self) -> None:
        if not self._ensure_ready():
            return
        self._run_background(self._worker_show_all)

    def _worker_show_all(self) -> None:
        try:
            rows: list[RowItem] = []
            for idx, path in enumerate(self.local_songs, start=1):
                if path in self.confirmed_links:
                    info = MatchInfo(song=self.confirmed_links[path], score=1.0, status="Matched")
                else:
                    info = best_catalog_match(path, self.catalog)
                self.match_map[path] = info
                song_title = info.song.title if info.song else "[No Match]"
                era = info.song.era if info.song else "[N/A]"
                rows.append(
                    RowItem(
                        title=song_title,
                        era=era,
                        score_text=f"{int(round(info.score * 100))}%",
                        status=info.status,
                        local_path=path,
                        matched_song=info.song,
                        can_link=info.status == "Probable Match" and info.song is not None,
                        file_path_text=str(path.parent),
                        local_display=path.name,
                        score_value=info.score,
                    )
                )
                if idx % 100 == 0:
                    self._ui_queue.put(("status", f"Matching local songs... {idx}/{len(self.local_songs)}"))
            self._ui_queue.put(("rows", f"All Local Songs ({len(rows)})", rows, f"Showing {len(rows)} local songs", "all"))
        except Exception as exc:
            _log_error("show_all_local_songs", exc)
            self._ui_queue.put(("error", f"Show all failed: {exc}"))

    def show_missing_tracks(self) -> None:
        if not self._ensure_ready():
            return
        self._run_background(self._worker_show_missing)

    def _worker_show_missing(self) -> None:
        try:
            matched_norms: set[str] = set()
            probable_rows: list[RowItem] = []
            for idx, path in enumerate(self.local_songs, start=1):
                if path in self.confirmed_links:
                    info = MatchInfo(song=self.confirmed_links[path], score=1.0, status="Matched")
                else:
                    info = best_catalog_match(path, self.catalog)
                self.match_map[path] = info
                if info.song and info.status in {"Matched", "Probable Match"}:
                    matched_norms.add(info.song.cmp_key)
                if info.status == "Probable Match" and info.song:
                    probable_rows.append(
                        RowItem(
                            title=info.song.title,
                            era=info.song.era or "[Unknown]",
                            score_text=f"{int(round(info.score * 100))}%",
                            status="Probable Match",
                            local_path=path,
                            matched_song=info.song,
                            can_link=True,
                            file_path_text=str(path.parent),
                            local_display=path.name,
                            score_value=info.score,
                        )
                    )
                if idx % 100 == 0:
                    self._ui_queue.put(("status", f"Computing missing tracks... {idx}/{len(self.local_songs)}"))

            missing_rows = [
                RowItem(
                    title=song.title,
                    era=song.era or "[Unknown]",
                    score_text="0%",
                    status="Missing",
                    score_value=0.0,
                )
                for song in self.catalog
                if song.cmp_key not in matched_norms
            ]
            rows = probable_rows + missing_rows
            self._ui_queue.put(("rows", f"Missing Tracks ({len(rows)})", rows, f"Missing tracks: {len(missing_rows)}", "missing"))
        except Exception as exc:
            _log_error("show_missing_tracks", exc)
            self._ui_queue.put(("error", f"Missing view failed: {exc}"))

    def update_match(self, local_path: Path, song: CatalogSong) -> None:
        try:
            # Rename local file to catalog title when possible.
            new_name = f"{_safe_filename(song.title)}{local_path.suffix.lower()}"
            new_path = local_path.with_name(new_name)
            final_path = local_path
            if new_path != local_path and not new_path.exists():
                local_path.rename(new_path)
                final_path = new_path
                for i, p in enumerate(self.local_songs):
                    if p == local_path:
                        self.local_songs[i] = final_path
                        break
                if local_path in self.match_map:
                    self.match_map[final_path] = self.match_map.pop(local_path)
                if local_path in self.confirmed_links:
                    self.confirmed_links[final_path] = self.confirmed_links.pop(local_path)

            write_core_tags(final_path, song.title, song.era)
            self.confirmed_links[final_path] = song
            self.match_map[final_path] = MatchInfo(song=song, score=1.0, status="Matched")
            self._set_status(f"Updated and synced: {final_path.name} -> {song.title} (100%)")
            if self._current_view == "all":
                self.show_all_local_songs()
            else:
                self.show_missing_tracks()
        except Exception as exc:
            _log_error("update_match", f"{local_path} :: {exc}")
            self._set_status(f"Update failed: {exc}")

    def register_missing_track(self, row: RowItem) -> None:
        try:
            df = pd.read_csv(self.csv_path, dtype=str)
            song_title = sanitize_title(row.title)
            if not song_title:
                self._set_status("Cannot register blank song title")
                return
            exists = False
            for _, r in df.iterrows():
                current = str(r.get("Song Title", "")).strip()
                if comparison_key(current) == comparison_key(song_title):
                    exists = True
                    break
            if exists:
                self._set_status(f"Already in CSV: {song_title}")
                return
            new_row = {c: "" for c in df.columns}
            if "Song Title" in new_row:
                new_row["Song Title"] = song_title
            if "Era" in new_row:
                new_row["Era"] = row.era if row.era and row.era != "[Unknown]" else "Unknown Era"
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.csv_path, index=False)
            self._load_catalog_safe()
            self._set_status(f"Registered to CSV (juice_unreleased_final.csv): {song_title}")
        except Exception as exc:
            _log_error("register_missing_track", exc)
            self._set_status(f"Register failed: {exc}")

    def open_csv_editor(self) -> None:
        if not self.csv_path.is_file():
            self._set_status(f"Missing CSV: {self.csv_path}")
            return
        try:
            df = pd.read_csv(self.csv_path, dtype=str).fillna("")
        except Exception as exc:
            _log_error("open_csv_editor", exc)
            self._set_status(f"CSV open failed: {exc}")
            return

        win = ctk.CTkToplevel(self)
        win.title("Edit Source CSV")
        win.geometry("860x640")
        win.grab_set()

        cols = list(df.columns)
        if "Song Title" not in cols:
            cols.append("Song Title")
            df["Song Title"] = ""
        if "Era" not in cols:
            cols.append("Era")
            df["Era"] = ""

        frame = ctk.CTkScrollableFrame(win, label_text="juice_unreleased_final.csv")
        frame.pack(fill="both", expand=True, padx=12, pady=12)

        ctk.CTkLabel(frame, text="Song Title", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(frame, text="Era", font=ctk.CTkFont(weight="bold")).grid(row=0, column=1, padx=6, pady=6, sticky="w")

        rows_vars: list[tuple[ctk.StringVar, ctk.StringVar]] = []
        max_rows = len(df)
        for i in range(max_rows):
            t_var = ctk.StringVar(value=str(df.iloc[i].get("Song Title", "")))
            e_var = ctk.StringVar(value=str(df.iloc[i].get("Era", "")))
            ctk.CTkEntry(frame, textvariable=t_var, width=500).grid(row=i + 1, column=0, padx=6, pady=2, sticky="ew")
            ctk.CTkEntry(frame, textvariable=e_var, width=220).grid(row=i + 1, column=1, padx=6, pady=2, sticky="ew")
            rows_vars.append((t_var, e_var))

        def add_row() -> None:
            idx = len(rows_vars) + 1
            t_var = ctk.StringVar(value="")
            e_var = ctk.StringVar(value="")
            ctk.CTkEntry(frame, textvariable=t_var, width=500).grid(row=idx, column=0, padx=6, pady=2, sticky="ew")
            ctk.CTkEntry(frame, textvariable=e_var, width=220).grid(row=idx, column=1, padx=6, pady=2, sticky="ew")
            rows_vars.append((t_var, e_var))

        def save_csv() -> None:
            try:
                new_rows = []
                for t_var, e_var in rows_vars:
                    title = sanitize_title(t_var.get())
                    era = e_var.get().strip()
                    if not title and not era:
                        continue
                    new_rows.append({"Song Title": title, "Era": era})
                out_df = pd.DataFrame(new_rows)
                out_df.to_csv(self.csv_path, index=False)
                self._load_catalog_safe()
                self._set_status(f"Saved CSV edits ({len(out_df)} rows)")
                win.destroy()
            except Exception as exc:
                _log_error("save_csv_editor", exc)
                self._set_status(f"Save failed: {exc}")

        actions = ctk.CTkFrame(win)
        actions.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(actions, text="Add Row", command=add_row).pack(side="left", padx=4)
        ctk.CTkButton(actions, text="Save CSV", command=save_csv).pack(side="left", padx=4)

    def export_organized_library(self) -> None:
        if not self._ensure_ready():
            return
        if self.output_folder is None:
            self._set_status("Pick an output folder first.")
            return
        self._run_background(self._worker_export_organized)

    def _worker_export_organized(self) -> None:
        try:
            total = len(self.local_songs)
            copied = 0
            for idx, path in enumerate(self.local_songs, start=1):
                info = self.match_map.get(path) or best_catalog_match(path, self.catalog)
                if path in self.confirmed_links:
                    info = MatchInfo(song=self.confirmed_links[path], score=1.0, status="Matched")
                if not info.song or info.status == "Missing":
                    continue
                era_folder = self.output_folder / _safe_filename(info.song.era or "Unknown Era")
                era_folder.mkdir(parents=True, exist_ok=True)
                dst = era_folder / f"{_safe_filename(info.song.title)}.mp3"
                try:
                    shutil.copy2(path, dst)
                    copied += 1
                except Exception as copy_exc:
                    _log_error("export_copy", f"{path} -> {dst} :: {copy_exc}")
                if idx % 25 == 0:
                    self._ui_queue.put(("status", f"Exported {idx}/{total} tracks..."))
            self._ui_queue.put(("done", f"Export complete: copied {copied} tracks to {self.output_folder}"))
        except Exception as exc:
            _log_error("export_organized_library", exc)
            self._ui_queue.put(("error", f"Export failed: {exc}"))

    def sync_all_match_titles(self) -> None:
        if not self._ensure_ready():
            return
        self._run_background(self._worker_sync_titles)

    def _worker_sync_titles(self) -> None:
        try:
            total = len(self.local_songs)
            renamed = 0
            for idx, path in enumerate(self.local_songs, start=1):
                info = self.match_map.get(path) or best_catalog_match(path, self.catalog)
                if path in self.confirmed_links:
                    info = MatchInfo(song=self.confirmed_links[path], score=1.0, status="Matched")
                if not info.song or info.score < 0.95:
                    continue
                new_name = f"{_safe_filename(info.song.title)}{path.suffix.lower()}"
                new_path = path.with_name(new_name)
                if new_path == path or new_path.exists():
                    continue
                try:
                    path.rename(new_path)
                    if path in self.match_map:
                        self.match_map[new_path] = self.match_map.pop(path)
                    if path in self.confirmed_links:
                        self.confirmed_links[new_path] = self.confirmed_links.pop(path)
                    for i, p in enumerate(self.local_songs):
                        if p == path:
                            self.local_songs[i] = new_path
                            break
                    renamed += 1
                except Exception as rename_exc:
                    _log_error("sync_rename", f"{path} -> {new_path} :: {rename_exc}")
                if idx % 50 == 0:
                    self._ui_queue.put(("status", f"Synced titles {idx}/{total}..."))
            self.local_songs.sort(key=lambda p: p.name.lower())
            self._ui_queue.put(("done", f"Sync complete: renamed {renamed} tracks"))
        except Exception as exc:
            _log_error("sync_all_match_titles", exc)
            self._ui_queue.put(("error", f"Sync failed: {exc}"))

    def auto_organize_all(self) -> None:
        if not self._ensure_ready():
            return
        self._run_background(self._worker_auto_organize)

    def _worker_auto_organize(self) -> None:
        try:
            total = len(self.local_songs)
            updated = 0
            skipped = 0
            for idx, path in enumerate(self.local_songs, start=1):
                info = self.match_map.get(path) or best_catalog_match(path, self.catalog)
                if path in self.confirmed_links:
                    info = MatchInfo(song=self.confirmed_links[path], score=1.0, status="Matched")
                if not info.song or info.status == "Missing":
                    skipped += 1
                    self._ui_queue.put(("status", f"Organized {idx}/{total} tracks"))
                    continue
                try:
                    if not os.access(path, os.W_OK):
                        raise PermissionError(f"Write denied: {path}")
                    write_core_tags(path, info.song.title, info.song.era)
                    updated += 1
                except Exception as file_exc:
                    skipped += 1
                    _log_error("auto_organize_file", f"{path} :: {file_exc}")
                self._ui_queue.put(("status", f"Organized {idx}/{total} tracks"))
            self._ui_queue.put(("done", f"Auto-organize complete: updated {updated}, skipped {skipped}, total {total}"))
        except Exception as exc:
            _log_error("auto_organize_all", exc)
            self._ui_queue.put(("error", f"Auto-organize failed: {exc}"))

    def open_song_editor(self, song_path: Path) -> None:
        win = ctk.CTkToplevel(self)
        win.title(f"Edit: {song_path.name}")
        win.geometry("620x280")
        win.grab_set()

        info = self.match_map.get(song_path)
        if info and info.song:
            matched_text = f"Matched CSV Song: {info.song.title}\nEra: {info.song.era or '[Unknown]'}"
        else:
            matched_text = "Matched CSV Song: [None]"

        ctk.CTkLabel(win, text=song_path.name, font=ctk.CTkFont(size=16, weight="bold"), anchor="w").pack(
            fill="x", padx=16, pady=(14, 8)
        )
        ctk.CTkLabel(win, text=matched_text, justify="left", anchor="w", wraplength=560).pack(
            fill="x", padx=16, pady=(0, 10)
        )

        edit_frame = ctk.CTkFrame(win)
        edit_frame.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(edit_frame, text="Title", width=70).grid(row=0, column=0, padx=6, pady=6)
        ctk.CTkLabel(edit_frame, text="Era", width=70).grid(row=1, column=0, padx=6, pady=6)
        title_var = ctk.StringVar(value=info.song.title if info and info.song else song_path.stem)
        era_var = ctk.StringVar(value=info.song.era if info and info.song else "")
        title_entry = ctk.CTkEntry(edit_frame, textvariable=title_var, width=420)
        era_entry = ctk.CTkEntry(edit_frame, textvariable=era_var, width=420)
        title_entry.grid(row=0, column=1, padx=6, pady=6, sticky="ew")
        era_entry.grid(row=1, column=1, padx=6, pady=6, sticky="ew")

        def apply_tags() -> None:
            try:
                title_text = sanitize_title(title_var.get())
                era_text = era_var.get().strip()
                if not title_text:
                    self._set_status("Title cannot be empty.")
                    return
                if not os.access(song_path, os.W_OK):
                    raise PermissionError(f"Write denied: {song_path}")
                write_core_tags(song_path, title_text, era_text)
                self._set_status(f"Updated tags for {song_path.name}")
            except Exception as exc:
                _log_error("manual_apply_tags", f"{song_path} :: {exc}")
                self._set_status(f"Tag update failed for {song_path.name}: {exc}")

        def choose_cover_art() -> None:
            image_file = ctk.filedialog.askopenfilename(
                title="Select cover art image",
                filetypes=[("Image files", "*.jpg *.jpeg *.png"), ("All files", "*.*")],
            )
            if not image_file:
                return
            try:
                if not os.access(song_path, os.W_OK):
                    raise PermissionError(f"Write denied: {song_path}")
                embed_cover_art(song_path, Path(image_file))
                self._set_status(f"Embedded cover art for {song_path.name}")
            except Exception as exc:
                _log_error("embed_cover_art", f"{song_path} :: {exc}")
                self._set_status(f"Cover art failed for {song_path.name}: {exc}")

        row = ctk.CTkFrame(win)
        row.pack(fill="x", padx=16, pady=10)
        ctk.CTkButton(row, text="Apply Title/Artist/Album", command=apply_tags).pack(side="left", padx=(0, 10))
        ctk.CTkButton(row, text="Set Cover Art", command=choose_cover_art).pack(side="left")


if __name__ == "__main__":
    app = LibraryManagerApp()
    app.mainloop()