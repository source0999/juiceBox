import argparse
import csv
import re
from pathlib import Path

from bs4 import BeautifulSoup


def _clean_cell_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _tr_to_cells(tr) -> list[str]:
    # We only care about direct td/th inside this tr.
    cells = []
    for td in tr.find_all(["td", "th"]):
        txt = _clean_cell_text(td.get_text(" ", strip=True))
        cells.append(txt)
    return cells


def extract_song_rows_to_csv(raw_html_path: str | Path, out_csv_path: str | Path) -> None:
    raw_html_path = Path(raw_html_path)
    out_csv_path = Path(out_csv_path)

    html_content = raw_html_path.read_text(encoding="utf-8", errors="replace")

    soup = BeautifulSoup(html_content, "html.parser")

    # Per request: ignore all style tags/content.
    for style_tag in soup.find_all("style"):
        style_tag.decompose()

    trs = soup.find_all("tr")
    if not trs:
        raise ValueError("No <tr> tags found in input HTML.")

    # Try to locate a header row containing at least "Song Title" and "Era".
    header_cells: list[str] | None = None
    for tr in trs:
        cells = _tr_to_cells(tr)
        if not cells:
            continue
        joined = " ".join(cells).lower()
        if "song title" in joined and "era" in joined:
            header_cells = cells
            break

    selected_rows: list[list[str]] = []
    for tr in trs:
        cells = _tr_to_cells(tr)
        if len(cells) < 3:
            continue

        joined = " ".join(cells).lower()

        # Filter heuristic: song rows tend to include track markers and/or notes.
        has_track_number = any(re.fullmatch(r"\d+", c) for c in cells if c)
        has_song_marker = (
            "song was originally called" in joined
            or "interlude" in joined
            or "freestyle" in joined
            or has_track_number
        )

        if not has_song_marker:
            continue

        # If we found a header, avoid re-selecting the header row.
        if header_cells is not None:
            header_joined = " ".join(header_cells).lower()
            if joined == header_joined:
                continue

        selected_rows.append(cells)

    # De-dupe identical rows while keeping order.
    seen = set()
    deduped: list[list[str]] = []
    for row in selected_rows:
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    if not deduped:
        # It's possible the pasted HTML only contains the wrapper page (iframe)
        # and not the actual spreadsheet grid.
        raise ValueError("No song-like <tr> rows found. The sheet iframe content may be missing.")

    # Write CSV
    with out_csv_path.open("w", newline="", encoding="utf-8") as f:
        if header_cells is not None:
            writer = csv.writer(f)
            writer.writerow([_clean_cell_text(c) for c in header_cells])
            for row in deduped:
                row = row[: len(header_cells)]
                if len(row) < len(header_cells):
                    row = row + [""] * (len(header_cells) - len(row))
                writer.writerow(row)
        else:
            max_cols = max(len(r) for r in deduped)
            writer = csv.writer(f)
            writer.writerow([f"col_{i}" for i in range(1, max_cols + 1)])
            for row in deduped:
                row = row[:max_cols]
                if len(row) < max_cols:
                    row = row + [""] * (max_cols - len(row))
                writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract song <tr> rows into juice_unreleased.csv")
    parser.add_argument("--input", default="raw_table.html", help="Path to raw_table.html")
    parser.add_argument("--output", default="juice_unreleased.csv", help="Output CSV path")
    args = parser.parse_args()

    extract_song_rows_to_csv(args.input, args.output)
    print(f"Saved CSV to {args.output}")


if __name__ == "__main__":
    main()