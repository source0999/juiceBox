import os
import re
from pathlib import Path

import customtkinter as ctk
import pandas as pd
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

# Default tracker HTML next to this script (embeds sheet HTML via iframe).
_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_TRACKER_HTML = _SCRIPT_DIR / "dataJuiceWrld.html"
_DEFAULT_TRACKER_CSV = _SCRIPT_DIR / "juice_unreleased_final.csv"

_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".wav"}


def _norm_key(s: str) -> str:
    if not s:
        return ""
    s = str(s).lower().strip()
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _resolve_sheet_html_paths(main_html: Path) -> list[Path]:
    """Main saved page plus any local iframe sheet (e.g. dataJuiceWrld_files/sheet.html)."""
    paths: list[Path] = [main_html]
    try:
        text = main_html.read_text(encoding="utf-8", errors="replace")[:200000]
    except OSError:
        return paths
    m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', text, re.I)
    if m:
        src = m.group(1).replace("\\", "/").lstrip("./")
        child = (main_html.parent / src).resolve()
        if child.is_file() and child not in paths:
            paths.append(child)
    return paths


def _song_title_column_from_dataframe(df: pd.DataFrame) -> tuple[list[str], bool]:
    """Read values under a 'Song Title' header (column name or first matching header cell)."""
    col_by_name = {str(c).strip().lower(): c for c in df.columns}
    if "song title" in col_by_name:
        col = df[col_by_name["song title"]]
        out: list[str] = []
        for x in col:
            if pd.isna(x):
                continue
            s = str(x).strip()
            if not s or s.lower() == "song title":
                continue
            out.append(s)
        return out, True

    max_r = min(60, len(df))
    for ri in range(max_r):
        for ci in range(len(df.columns)):
            val = df.iat[ri, ci]
            if pd.isna(val):
                continue
            if str(val).strip().lower() != "song title":
                continue
            col = df.iloc[:, ci]
            out = []
            for i in range(ri + 1, len(col)):
                x = col.iat[i]
                if pd.isna(x):
                    continue
                s = str(x).strip()
                if s and s.lower() != "song title":
                    out.append(s)
            return out, True
    return [], False


def _title_era_pairs_from_dataframe(df: pd.DataFrame) -> tuple[list[tuple[str, str]], bool]:
    """Pair each Song Title with Era (same row). Header via column names or grid cells."""
    col_by_name = {str(c).strip().lower(): c for c in df.columns}
    if "song title" in col_by_name and "era" in col_by_name:
        ct = df[col_by_name["song title"]]
        ce = df[col_by_name["era"]]
        pairs: list[tuple[str, str]] = []
        for i in range(len(df)):
            raw_t = ct.iat[i]
            if pd.isna(raw_t):
                continue
            title = str(raw_t).strip()
            if not title or title.lower() == "song title":
                continue
            raw_e = ce.iat[i]
            era = "" if pd.isna(raw_e) else str(raw_e).strip()
            pairs.append((title, era))
        return pairs, bool(pairs)

    max_r = min(60, len(df))
    for ri in range(max_r):
        ci_title = ci_era = None
        for ci in range(len(df.columns)):
            val = df.iat[ri, ci]
            if pd.isna(val):
                continue
            v = str(val).strip().lower()
            if v == "song title":
                ci_title = ci
            elif v == "era":
                ci_era = ci
        if ci_title is None or ci_era is None:
            continue
        col_t = df.iloc[:, ci_title]
        col_e = df.iloc[:, ci_era]
        pairs = []
        for i in range(ri + 1, len(df)):
            raw_t = col_t.iat[i]
            if pd.isna(raw_t):
                continue
            title = str(raw_t).strip()
            if not title or title.lower() == "song title":
                continue
            raw_e = col_e.iat[i]
            era = "" if pd.isna(raw_e) else str(raw_e).strip()
            pairs.append((title, era))
        return pairs, bool(pairs)
    return [], False


def load_title_era_pairs(html_path: str | Path) -> list[tuple[str, str]]:
    """Load (Song Title, Era) rows from saved tracker HTML (Unreleased tab)."""
    main = Path(html_path)
    for p in _resolve_sheet_html_paths(main):
        try:
            tables = pd.read_html(p, encoding="utf-8")
        except (ValueError, ImportError, OSError):
            continue
        for df in tables:
            pairs, ok = _title_era_pairs_from_dataframe(df)
            if ok:
                return pairs
    return []


def _norms_for_mp3(mp3_path: str | Path) -> set[str]:
    p = Path(mp3_path)
    norms = {_norm_key(p.stem)}
    try:
        tags = EasyID3(p)
        if "title" in tags:
            norms.add(_norm_key(tags["title"][0]))
    except Exception:
        pass
    return {n for n in norms if n}


def lookup_era_for_mp3(mp3_path: str | Path, pairs: list[tuple[str, str]]) -> str | None:
    """Return Era for the first spreadsheet row whose Song Title matches this file (stem / ID3 title)."""
    norms = _norms_for_mp3(mp3_path)
    if not norms:
        return None
    for title, era in pairs:
        if _title_matched_locally(title, norms):
            return era
    return None


def tag_mp3_with_juice_era(mp3_path: str | Path, html_path: str | Path) -> None:
    """
    Set ID3 artist to 'Juice WRLD' and album to the Era from the spreadsheet row
    that matches this track (by Song Title vs file name / ID3 title).
    """
    mp3_path = Path(mp3_path)
    if mp3_path.suffix.lower() != ".mp3":
        raise ValueError(f"Expected an .mp3 file, got: {mp3_path}")

    pairs = load_title_era_pairs(html_path)
    if not pairs:
        raise ValueError("No 'Song Title' and 'Era' columns found in spreadsheet HTML.")

    era = lookup_era_for_mp3(mp3_path, pairs)
    if era is None:
        raise ValueError(f"No spreadsheet row matched this file: {mp3_path}")

    path = str(mp3_path.resolve())
    try:
        audio = EasyID3(path)
    except ID3NoHeaderError:
        EasyID3().save(path)
        audio = EasyID3(path)

    audio["artist"] = "Juice WRLD"
    audio["album"] = era
    audio.save()


def load_unreleased_song_titles(html_path: str | Path) -> list[str]:
    """
    Load song titles from saved Google Sheets HTML (Unreleased tab).
    Expects a column labeled 'Song Title'. Follows iframe src to embedded sheet HTML.
    """
    main = Path(html_path)
    seen_lower: set[str] = set()
    ordered: list[str] = []
    for p in _resolve_sheet_html_paths(main):
        try:
            tables = pd.read_html(p, encoding="utf-8")
        except (ValueError, ImportError, OSError):
            continue
        for df in tables:
            titles, ok = _song_title_column_from_dataframe(df)
            if not ok or not titles:
                continue
            for t in titles:
                low = t.lower()
                if low in seen_lower:
                    continue
                seen_lower.add(low)
                ordered.append(t)
            return ordered
    return []


def _local_norms_for_folder(folder: str) -> set[str]:
    norms: set[str] = set()
    for dirpath, _, files in os.walk(folder):
        for name in files:
            suf = Path(name).suffix.lower()
            if suf not in _AUDIO_EXTS:
                continue
            full = os.path.join(dirpath, name)
            norms.add(_norm_key(Path(name).stem))
            try:
                tags = EasyID3(full)
                if "title" in tags:
                    norms.add(_norm_key(tags["title"][0]))
            except Exception:
                pass
    return norms


def _title_matched_locally(song_title: str, norms: set[str]) -> bool:
    sn = _norm_key(song_title)
    if not sn:
        return True
    if sn in norms:
        return True
    for n in norms:
        if len(sn) >= 4 and (sn in n or n in sn):
            return True
    return False


def _is_partial_title_match(song_title: str, file_stem: str) -> bool:
    """True when normalized song title and file stem partially overlap."""
    title_norm = _norm_key(song_title)
    stem_norm = _norm_key(file_stem)
    if not title_norm or not stem_norm:
        return False
    if title_norm == stem_norm:
        return True
    if len(title_norm) < 4 or len(stem_norm) < 4:
        return False
    return title_norm in stem_norm or stem_norm in title_norm


def load_title_era_pairs_from_csv(csv_path: str | Path) -> list[tuple[str, str]]:
    """
    Load (Song Title, Era) from a CSV file.
    Requires columns named 'Song Title' and 'Era'.
    """
    df = pd.read_csv(csv_path)
    col_map = {str(c).strip().lower(): c for c in df.columns}
    if "song title" not in col_map or "era" not in col_map:
        raise ValueError("CSV must contain 'Song Title' and 'Era' columns.")

    title_col = col_map["song title"]
    era_col = col_map["era"]

    pairs: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        raw_title = row[title_col]
        if pd.isna(raw_title):
            continue
        title = str(raw_title).strip()
        if not title:
            continue
        raw_era = row[era_col]
        era = "" if pd.isna(raw_era) else str(raw_era).strip()
        pairs.append((title, era))
    return pairs


def find_missing_unreleased_songs(html_path: str | Path, music_folder: str) -> list[str]:
    """Compare 'Song Title' entries from the tracker HTML to files under music_folder; return missing titles."""
    titles = load_unreleased_song_titles(html_path)
    if not titles:
        return []
    norms = _local_norms_for_folder(music_folder)
    return [t for t in titles if not _title_matched_locally(t, norms)]


class JuiceManager(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Juice WRLD Library Manager")
        self.geometry("700x520")

        self.label = ctk.CTkLabel(self, text="Juice WRLD Unreleased Sorter", font=("Arial", 20))
        self.label.pack(pady=(20, 10))

        self.btn_scan = ctk.CTkButton(self, text="Scan Music Folder", command=self.scan_folder)
        self.btn_scan.pack(pady=10)

        self.info_label = ctk.CTkLabel(
            self,
            text="Pick a music folder. Tracker data is read from juice_unreleased_final.csv next to this app.",
            wraplength=560,
            justify="left",
            anchor="w",
        )
        self.info_label.pack(fill="x", padx=24, pady=(0, 8))

        self.results_scroll = ctk.CTkScrollableFrame(self, width=540, height=300, label_text="Found tracks")
        self.results_scroll.pack(fill="both", expand=True, padx=24, pady=(0, 20))

    def _clear_missing_list(self) -> None:
        for child in self.results_scroll.winfo_children():
            child.destroy()

    def scan_folder(self):
        folder_path = ctk.filedialog.askdirectory()
        if not folder_path:
            return

        self._clear_missing_list()
        csv_path = _DEFAULT_TRACKER_CSV

        if not csv_path.is_file():
            self.info_label.configure(
                text=(
                    f"Music folder: {folder_path}\n\n"
                    f"Could not find {csv_path.name} in {_SCRIPT_DIR}.\n"
                    "Place the final CSV next to app.py."
                )
            )
            self.results_scroll.configure(label_text="Found tracks")
            return

        self.info_label.configure(
            text=f"Music folder: {folder_path}\nTracker CSV: {csv_path.name} (next to script)"
        )

        try:
            pairs = load_title_era_pairs_from_csv(csv_path)
        except (ValueError, pd.errors.ParserError) as exc:
            self.results_scroll.configure(label_text="Found tracks")
            self.info_label.configure(
                text=(
                    f"Music folder: {folder_path}\nTracker CSV: {csv_path.name}\n\n"
                    f"{exc}"
                )
            )
            return

        found: list[tuple[str, str, str]] = []
        for dirpath, _, files in os.walk(folder_path):
            for name in files:
                if Path(name).suffix.lower() not in _AUDIO_EXTS:
                    continue
                stem = Path(name).stem
                match_title = ""
                match_era = ""
                for title, era in pairs:
                    if _is_partial_title_match(title, stem):
                        match_title = title
                        match_era = era
                        break
                if match_title:
                    found.append((name, match_title, match_era))

        if not found:
            self.results_scroll.configure(label_text="Found tracks (0)")
            ctk.CTkLabel(
                self.results_scroll,
                text="No local filenames partially matched any 'Song Title' from the CSV.",
                wraplength=500,
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=8, pady=8)
            return

        self.results_scroll.configure(label_text=f"Found tracks ({len(found)})")
        for file_name, title, era in found:
            ctk.CTkLabel(
                self.results_scroll,
                text=f"Found: {file_name}  ->  {title}  |  Era: {era or '[Unknown]'}",
                wraplength=500,
                justify="left",
                anchor="w",
            ).pack(fill="x", padx=8, pady=4)

if __name__ == "__main__":
    app = JuiceManager()
    app.mainloop()