# Juice WRLD Library Manager

Desktop app (CustomTkinter) for managing a local Juice WRLD library from a CSV catalog.

The app scans your local MP3 folder, fuzzy-matches tracks against `juice_unreleased_final.csv`, shows what you have or are missing, and can auto-update ID3 tags.

## Features

- Sidebar workflow:
  - `Scan Library`
  - `Show All Local Songs`
  - `Show Missing Tracks`
  - `Auto-Organize All`
- Scrollable main view for large song lists.
- Robust matching (`_norm_key` + fuzzy ratio), so names like `Rental - Leaked.mp3` can match `Rental`.
- Auto-organizer updates:
  - `Artist` -> `Juice WRLD`
  - `Album` -> `Era` from CSV
  - `Title` -> `Song Title` from CSV
- Per-song edit window:
  - apply title/artist/album
  - embed cover art (`.jpg`, `.jpeg`, `.png`)
- Live status bar progress (example: `Organized 45/501 tracks`).

## Project Files

- `app.py` - main GUI app
- `juice_unreleased_final.csv` - cleaned unreleased catalog
- `requirements.txt` - Python dependencies

## CSV Requirements

The app expects `juice_unreleased_final.csv` in the same folder as `app.py`.

Required columns:
- `Song Title`
- `Era`

Column names are matched case-insensitively.

## Setup

1. Install Python 3.10+.
2. Create and activate a virtual environment (recommended).
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Ensure `juice_unreleased_final.csv` sits next to `app.py`.
5. Run:

```bash
python app.py
```

## Usage

1. Click `Scan Library` and pick your music folder.
2. Click `Show All Local Songs` to view every local MP3 and current CSV match.
3. Click `Show Missing Tracks` to list catalog songs with no local fuzzy match.
4. Click `Auto-Organize All` to write ID3 tags to matched local tracks.
5. In song lists, click `Edit` to:
   - apply title/artist/album for that track
   - embed cover art into that MP3

## Matching Logic

For each local MP3 filename stem, the app computes similarity against each CSV `Song Title`:

- exact normalized match -> strongest
- substring containment (both directions) -> very strong
- `difflib.SequenceMatcher` ratio + token overlap -> fallback fuzzy score

Matches above threshold are treated as found.

## ID3 Behavior

Auto-organize and manual tag updates:
- create ID3 header if missing
- write `artist`, `album`, `title` via `mutagen.easyid3.EasyID3`

Cover art:
- writes APIC frame using `mutagen.id3.ID3`
- replaces existing cover art (current behavior)

## Troubleshooting

- **"CSV must contain 'Song Title' and 'Era' columns."**
  - Confirm headers exist in `juice_unreleased_final.csv`.
- **No songs appear after scan**
  - Current app scans `.mp3` files only.
- **Low-quality matches**
  - Rename files closer to canonical song names or adjust matching threshold in `app.py`.
- **Cover art fails**
  - Use `.jpg/.jpeg/.png` files and verify file permissions.

## Notes

- This tool modifies metadata in your local files.
- Back up your library before running `Auto-Organize All` on a large collection.
