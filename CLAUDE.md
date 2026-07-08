# Amapiano Music Library

## Quick Start
- Server: `cd /Users/djsly/amapiano && source .venv/bin/activate && python server.py &`
- URL: http://localhost:8766
- Open: `open http://localhost:8766`

## Stack
- Python/Flask backend (`server.py`), port 8766
- Static frontend in `public/index.html`
- Library data in `library.json`, cover art in `covers/`
- Features: genre tagging, Spotify lookup, Serato crate import/export, rekordbox XML export

## Rekordbox
- `POST /api/rekordbox/export` with `{}` = full sync (all playlists + all live Serato crates) → `~/Music/rekordbox-amapiano.xml`; with `playlist_id` or `crate_name` = single export to its own `~/Music/rekordbox-<name>.xml`
- Downloads auto-regenerate the full XML after creating the Serato crate, so Serato and rekordbox stay in sync
- MCP tool: `export_to_rekordbox` (in `~/.claude/mcp-servers/amapiano/server.mjs`); the rekordbox MCP's xml path points at the generated file
- In rekordbox: Preferences > Advanced > Database > rekordbox xml → `~/Music/rekordbox-amapiano.xml`, enable View > Layout > rekordbox xml, import playlists from the sidebar tree

## USB Export (`/usb` page, "⇪ USB Export" in sidebar)
- `GET /api/usb/volumes`, `POST /api/usb/prepare` `{volume, playlist_ids, crate_names}` (background job), `GET /api/usb/jobs/<id>`
- Copies tracks to `<usb>/Music/<name>/`, writes `_Serato_/Subcrates/*.crate` with USB-relative paths + minimal `_Serato_/database V2` (required or Serato won't mount the drive's crates), plus `rekordbox-import.xml` on the stick
- Uses `shutil.copyfile` (never copy2 — metadata copy on FAT32 spawns `._` AppleDouble ghosts) and cleans `._`/`.DS_Store` after
- Standalone CDJs need rekordbox's own "export to device" (proprietary .pdb) — laptop Serato/rekordbox both work directly from the stick
