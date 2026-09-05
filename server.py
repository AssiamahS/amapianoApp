#!/usr/bin/env python3
"""Amapiano Music Library v2 - genre tagging, Spotify lookup, Serato crates, playlists."""

import json
import os
import re
import hashlib
import struct
import time
import base64
import subprocess
import threading
import urllib.request
import urllib.parse
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

app = Flask(__name__, static_folder="public")
CORS(app)

MUSIC_DIRS = [
    Path.home() / "Music" / "yt-dlp",
    Path.home() / "Music" / "Music" / "Media.localized" / "Music",
    Path.home() / "Downloads",
]
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".opus", ".ogg"}
DB_FILE = Path(__file__).parent / "library.json"
COVERS_DIR = Path(__file__).parent / "covers"
COVERS_DIR.mkdir(exist_ok=True)

MOBILE_DIR = Path(__file__).parent.parent / "amapiano-iphone"
SERATO_DIR = Path.home() / "Music" / "_Serato_" / "Subcrates"
SERATO_BACKUP = Path.home() / "Music" / "_Serato_Backup" / "Subcrates"

# Spotify credentials: env override, else spotdl's public client pair
# (the old hardcoded app was deleted upstream and returns invalid_client)
SPOTIFY_ID = os.environ.get("SPOTIFY_ID", "5f573c9620494bae87890c0f08a60293")
SPOTIFY_SECRET = os.environ.get("SPOTIFY_SECRET", "212476d9b0f3472eaa762d90b19b0ba8")
_spotify_token = {"token": None, "expires": 0}


def get_spotify_token():
    import time
    if _spotify_token["token"] and time.time() < _spotify_token["expires"]:
        return _spotify_token["token"]
    import urllib.request
    import base64
    auth = base64.b64encode(f"{SPOTIFY_ID}:{SPOTIFY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    _spotify_token["token"] = resp["access_token"]
    _spotify_token["expires"] = time.time() + resp["expires_in"] - 60
    return _spotify_token["token"]


def spotify_search(title, artist):
    """Search Spotify for track and return genre from artist."""
    import urllib.request, urllib.parse
    token = get_spotify_token()
    q = urllib.parse.quote(f"track:{title} artist:{artist}")
    req = urllib.request.Request(
        f"https://api.spotify.com/v1/search?q={q}&type=track&limit=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        tracks = data.get("tracks", {}).get("items", [])
        if not tracks:
            return None
        track = tracks[0]
        artist_id = track["artists"][0]["id"] if track["artists"] else None
        if not artist_id:
            return None
        # Get artist genres
        req2 = urllib.request.Request(
            f"https://api.spotify.com/v1/artists/{artist_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        artist_data = json.loads(urllib.request.urlopen(req2, timeout=10).read())
        genres = artist_data.get("genres", [])
        return genres[0].title() if genres else None
    except Exception:
        return None


def load_db():
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {"tracks": {}, "playlists": {}}


def save_db(db):
    DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False))


def file_id(path):
    return hashlib.md5(path.encode()).hexdigest()[:12]


def classify_title(title):
    """Detect video/lyrics/visualizer markers in title."""
    tl = title.lower()
    flags = []
    if "official" in tl and ("video" in tl or "music video" in tl):
        flags.append("video")
    if "lyric" in tl:
        flags.append("lyrics")
    if "visualizer" in tl:
        flags.append("visualizer")
    return flags


# ── Sanitation: audio_only > lyric_video > music_video > live ──
_SANITIZE_MUSIC_VIDEO_PATTERNS = [
    r'\bofficial\s*music\s*video\b',
    r'\bmusic\s*video\b',
    r'\bofficial\s*video\b',
    r'\[\s*video\s*\]',
    r'\(\s*video\s*\)',
    r'\bofficial\s*hd\s*video\b',
    r'\bhd\s*video\b',
    r'\bofficial\s*mv\b',
    r'\bmusic\s*vid\b',
    r'\bdance\s*video\b',
    r'\bvertical\s*video\b',
]
_SANITIZE_LIVE_PATTERNS = [
    r'\blive\s*at\b',
    r'\blive\s*performance\b',
    r'\blive\s*session\b',
    r'\bconcert\b',
    r'\bfestival\b',
    r'\btiny\s*desk\b',
    r'\bunplugged\b',
    r'\bacoustic\s*version\b',
    r'\blive\s*on\b',
    r'\bcolors\s*show\b',
]
_SANITIZE_LYRIC_PATTERNS = [
    r'\blyric\s*video\b',
    r'\(lyrics\)',
    r'\[lyrics\]',
    r'\blyrics\b',
    r'\bvisualizer\b',
    r'\bvisualiser\b',
    r'\blyric\s*visualizer\b',
]
_SANITIZE_AUDIO_PATTERNS = [
    r'\bofficial\s*audio\b',
    r'\(audio\)',
    r'\[audio\]',
    r'- topic\b',
    r'\baudio\s*only\b',
    r'\bofficial\s*album\s*audio\b',
]


def sanitize_classify(title):
    """Classify track source: audio_only | lyric_video | music_video | live | unknown."""
    if not title:
        return 'unknown'
    t = title.lower()
    for p in _SANITIZE_MUSIC_VIDEO_PATTERNS:
        if re.search(p, t):
            return 'music_video'
    for p in _SANITIZE_LIVE_PATTERNS:
        if re.search(p, t):
            return 'live'
    for p in _SANITIZE_LYRIC_PATTERNS:
        if re.search(p, t):
            return 'lyric_video'
    for p in _SANITIZE_AUDIO_PATTERNS:
        if re.search(p, t):
            return 'audio_only'
    return 'unknown'


def sanitize_clean_title(title):
    """Strip video/audio/lyric suffixes from a title to improve YouTube search matching."""
    # Remove anything in brackets/parens that contains video/audio/lyric keywords
    patterns_to_strip = [
        r'\s*[\[\(][^\]\)]*(?:official|music|lyric|audio|video|mv|hd|visualizer|visualiser|vertical|dance)[^\]\)]*[\]\)]\s*',
        r'\s*-?\s*official\s*(?:music\s*)?video\s*$',
        r'\s*-?\s*official\s*audio\s*$',
        r'\s*-?\s*lyric\s*video\s*$',
        r'\s*-?\s*lyrics\s*$',
        r'\s*-?\s*visualizer\s*$',
        r'\s*-?\s*audio\s*$',
    ]
    cleaned = title
    for p in patterns_to_strip:
        cleaned = re.sub(p, '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip(' -|·')


def extract_cover(filepath, fid):
    cover_path = COVERS_DIR / f"{fid}.jpg"
    if cover_path.exists():
        return f"/api/cover/{fid}"
    try:
        ext = Path(filepath).suffix.lower()
        if ext == ".mp3":
            tags = ID3(filepath)
            for key in tags:
                if key.startswith("APIC"):
                    cover_path.write_bytes(tags[key].data)
                    return f"/api/cover/{fid}"
        elif ext in (".m4a", ".mp4", ".aac"):
            mp4 = MP4(filepath)
            if "covr" in mp4.tags:
                cover_path.write_bytes(bytes(mp4.tags["covr"][0]))
                return f"/api/cover/{fid}"
    except Exception:
        pass
    return None


def _set_serato_cue1_at_zero(filepath):
    """Set Serato CUE point #1 (red) at position 0ms if no cue points exist."""
    try:
        from mutagen.id3 import ID3, GEOB
        try:
            tags = ID3(filepath)
        except Exception:
            return
        if "GEOB:Serato Markers2" in tags:
            return  # already has markers, don't overwrite
        # Build Serato Markers2 with CUE #1 at 0ms (red)
        cue_data = b'\x00\x00'  # padding + index 0
        cue_data += struct.pack('>I', 0)  # position 0ms
        cue_data += b'\x00'  # padding
        cue_data += bytes([0xCC, 0x00, 0x00])  # red
        cue_data += b'\x00\x00'  # padding
        cue_data += b'\x00'  # empty name
        entry = b'CUE\x00' + struct.pack('>I', len(cue_data)) + cue_data
        bpmlock = b'BPMLOCK\x00' + struct.pack('>I', 1) + b'\x00'
        payload = base64.b64encode(entry + bpmlock)
        marker_data = bytes([0x01, 0x01]) + payload
        tags.add(GEOB(encoding=0, mime='application/octet-stream',
                       desc='Serato Markers2', data=marker_data))
        tags.save(filepath)
    except Exception as e:
        print(f"[cue] Error setting cue for {filepath}: {e}", flush=True)


def scan_track(filepath):
    fid = file_id(filepath)
    name = Path(filepath).stem
    try:
        m = mutagen.File(filepath, easy=True)
        if m is None:
            return None
        title = (m.get("title") or [name])[0]
        artist = (m.get("artist") or ["Unknown"])[0]
        genre = (m.get("genre") or [""])[0]
        album = (m.get("album") or [""])[0]
        duration = m.info.length if m.info else 0

        if title == name and " - " in name:
            parts = name.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()

        cover = extract_cover(filepath, fid)
        flags = classify_title(title)
        _set_serato_cue1_at_zero(filepath)

        try:
            file_size = os.path.getsize(filepath)
        except OSError:
            file_size = 0

        return {
            "id": fid,
            "path": filepath,
            "title": title,
            "artist": artist,
            "genre": genre,
            "album": album,
            "duration": round(duration, 1),
            "file_size": file_size,
            "cover": cover,
            "custom_tags": [],
            "flags": flags,
        }
    except Exception:
        return None


# ── Serato crate parser ──
def parse_serato_crate(crate_path):
    """Parse a Serato .crate file and return list of file paths."""
    try:
        data = crate_path.read_bytes()
        paths = []
        # Find ptrk entries (UTF-16BE encoded file paths after 'ptrk' + 4-byte length)
        i = 0
        while i < len(data):
            idx = data.find(b"ptrk", i)
            if idx == -1:
                break
            length = struct.unpack(">I", data[idx + 4 : idx + 8])[0]
            path_bytes = data[idx + 8 : idx + 8 + length]
            try:
                path = path_bytes.decode("utf-16-be").strip("\x00")
                if path.startswith("/"):
                    paths.append(path)
                else:
                    paths.append("/" + path)
            except Exception:
                pass
            i = idx + 8 + length
        return paths
    except Exception:
        return []


# ── Routes ──

@app.route("/")
def index():
    return send_from_directory("public", "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("public", "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return send_from_directory("public", "sw.js", mimetype="application/javascript")


@app.route("/logo.png")
def logo():
    return send_from_directory("public", "logo.png")


@app.route("/downloads")
def downloads_page():
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Amapiano Downloads</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0a0a;color:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:24px;max-width:800px;margin:0 auto}
h1{font-size:24px;font-weight:700;color:#ff5500;margin-bottom:20px}
.input-group{display:flex;flex-direction:column;gap:10px;margin-bottom:24px;padding:16px;background:#111;border-radius:12px;border:1px solid #222}
input{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:10px 14px;color:#eee;font-size:14px;outline:none;width:100%}
input:focus{border-color:#ff5500}
.btn{background:#ff5500;color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:14px;font-weight:600;cursor:pointer}
.btn:hover{background:#e64d00}
.btn:disabled{background:#333;cursor:not-allowed;color:#666}
.dl-item{padding:14px;background:#111;border-radius:8px;border:1px solid #222;margin-bottom:8px;display:flex;align-items:center;gap:12px}
.dl-status{font-size:11px;padding:3px 8px;border-radius:6px;font-weight:600;flex-shrink:0}
.dl-status.queued{background:#333;color:#888}
.dl-status.downloading{background:#331a00;color:#ff5500}
.dl-status.done{background:#0a2a0a;color:#4c4}
.dl-status.error{background:#2a0a0a;color:#f44}
.dl-info{flex:1;min-width:0}
.dl-name{font-weight:600;font-size:14px}
.dl-url{font-size:11px;color:#555;font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dl-detail{font-size:11px;margin-top:3px}
.dl-detail.ok{color:#4c4}
.dl-detail.err{color:#f44}
.empty{color:#444;text-align:center;padding:40px;font-size:14px}
.resolving{font-size:12px;color:#ff5500}
</style></head><body>
<h1>Amapiano Downloads</h1>
<div class="input-group">
  <input id="urlInput" placeholder="Paste Spotify, SoundCloud, or YouTube URL" autocomplete="off">
  <div style="display:flex;gap:10px;align-items:center">
    <input id="nameInput" placeholder="Playlist name (auto-detected)">
    <span class="resolving" id="resolving" style="display:none">Fetching...</span>
  </div>
  <button class="btn" id="dlBtn" onclick="startDl()">Download</button>
</div>
<div id="list"><div class="empty">No downloads yet</div></div>
<script>
const API='/api';
let polling;
document.getElementById('urlInput').addEventListener('input',async e=>{
  const url=e.target.value.trim();
  if(url.includes('spotify.com')){
    document.getElementById('resolving').style.display='inline';
    try{
      const r=await fetch(API+'/resolve-name',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
      const d=await r.json();
      if(d.name&&!document.getElementById('nameInput').value)document.getElementById('nameInput').value=d.name;
    }catch(e){}
    document.getElementById('resolving').style.display='none';
  }
});
async function startDl(){
  const url=document.getElementById('urlInput').value.trim();
  if(!url)return;
  const name=document.getElementById('nameInput').value.trim()||'';
  document.getElementById('dlBtn').disabled=true;
  await fetch(API+'/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,name})});
  document.getElementById('urlInput').value='';
  document.getElementById('nameInput').value='';
  document.getElementById('dlBtn').disabled=false;
  poll();
}
async function poll(){
  try{
    const r=await fetch(API+'/downloads');
    const d=await r.json();
    const list=document.getElementById('list');
    if(!d.downloads.length){list.innerHTML='<div class="empty">No downloads yet</div>';return;}
    list.innerHTML=d.downloads.map(dl=>`<div class="dl-item">
      <span class="dl-status ${dl.status}">${dl.status}</span>
      <div class="dl-info">
        <div class="dl-name">${esc(dl.name)}</div>
        <div class="dl-url">${esc(dl.url)}</div>
        ${dl.status==='downloading'&&dl.progress?`<div class="dl-detail" style="color:#ff5500">Downloading track ${esc(dl.progress)}</div>`:''}
        ${dl.status==='done'&&dl.new_tracks?`<div class="dl-detail ok">${dl.new_tracks} tracks added</div>`:''}
        ${dl.error?`<div class="dl-detail err">${esc(dl.error).slice(0,200)}</div>`:''}
      </div>
    </div>`).join('');
  }catch(e){}
}
function esc(s){return s?s.replace(/&/g,'&amp;').replace(/</g,'&lt;'):''}
poll();
polling=setInterval(poll,3000);
</script></body></html>"""


def _try_audd(audio_bytes, mime):
    key = os.environ.get("AUDD_API_KEY", "").strip()
    if not key:
        return None
    try:
        import requests as _rq
        resp = _rq.post(
            "https://api.audd.io/",
            data={"api_token": key, "return": "spotify,apple_music"},
            files={"file": ("clip", audio_bytes, mime or "audio/webm")},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        print(f"[shazam] AudD error: {e}")
        return None
    if data.get("status") != "success" or not data.get("result"):
        return None
    r = data["result"]
    sp = r.get("spotify") or {}
    am = r.get("apple_music") or {}
    cover = None
    try:
        cover = (sp.get("album") or {}).get("images", [{}])[0].get("url")
    except Exception:
        pass
    if not cover:
        art = am.get("artwork") or {}
        cover = (art.get("url") or "").replace("{w}", "300").replace("{h}", "300")
    return {
        "source": "audd",
        "title": r.get("title"),
        "artist": r.get("artist"),
        "album": r.get("album"),
        "release_date": r.get("release_date"),
        "spotify_url": (sp.get("external_urls") or {}).get("spotify"),
        "apple_url": am.get("url") or r.get("song_link"),
        "cover": cover,
    }


def _try_rapidapi_shazam(audio_bytes, mime):
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if not key:
        return None
    try:
        import requests as _rq
        resp = _rq.post(
            "https://shazam-core.p.rapidapi.com/v1/tracks/recognize",
            headers={
                "X-RapidAPI-Key": key,
                "X-RapidAPI-Host": "shazam-core.p.rapidapi.com",
            },
            files={"upload_file": ("clip", audio_bytes, mime or "audio/webm")},
            timeout=25,
        )
        if resp.status_code != 200:
            print(f"[shazam] RapidAPI HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        print(f"[shazam] RapidAPI error: {e}")
        return None
    t = data.get("track")
    if not t and data.get("matches"):
        t = data["matches"][0]
    if not t:
        return None
    cover = (t.get("images") or {}).get("coverart") or (t.get("images") or {}).get("background")
    spotify_url = None
    apple_url = None
    for action in ((t.get("hub") or {}).get("actions") or []):
        uri = action.get("uri", "")
        if "spotify" in uri and not spotify_url:
            spotify_url = uri
        elif "music.apple.com" in uri and not apple_url:
            apple_url = uri
    if not apple_url:
        apple_url = (t.get("share") or {}).get("href")
    return {
        "source": "rapidapi",
        "title": t.get("title"),
        "artist": t.get("subtitle"),
        "album": None,
        "cover": cover,
        "spotify_url": spotify_url,
        "apple_url": apple_url,
    }


@app.route("/api/shazam", methods=["POST"])
def shazam_match():
    """Recognize a short audio clip. Tries AudD first, falls back to RapidAPI Shazam."""
    if "audio" not in request.files:
        return jsonify({"error": "No audio uploaded"}), 400
    audio_file = request.files["audio"]
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"error": "Empty audio clip"}), 400
    mime = audio_file.mimetype
    print(f"[shazam] Received {len(audio_bytes)} bytes, mime={mime}")
    try:
        import requests  # noqa: F401 - availability check
    except ImportError:
        return jsonify({"error": "Missing 'requests' — run: pip install requests"}), 500
    result = _try_audd(audio_bytes, mime)
    if result:
        print(f"[shazam] AudD matched: {result.get('title')} — {result.get('artist')}")
    else:
        print("[shazam] AudD no match, trying RapidAPI...")
        result = _try_rapidapi_shazam(audio_bytes, mime)
        if result:
            print(f"[shazam] RapidAPI matched: {result.get('title')} — {result.get('artist')}")
        else:
            print("[shazam] RapidAPI no match either")
    if not result:
        if not os.environ.get("AUDD_API_KEY") and not os.environ.get("RAPIDAPI_KEY"):
            return jsonify({"error": "No Shazam backend configured. Set AUDD_API_KEY or RAPIDAPI_KEY."}), 400
        return jsonify({"matched": False, "message": "No match found (tried AudD + RapidAPI)", "bytes_received": len(audio_bytes)})
    result["matched"] = True
    return jsonify(result)


@app.route("/mobile")
def mobile():
    return send_from_directory(str(MOBILE_DIR), "index.html")


@app.route("/mobile/<path:filename>")
def mobile_static(filename):
    return send_from_directory(str(MOBILE_DIR), filename)


@app.route("/api/scan", methods=["POST"])
def scan_library():
    db = load_db()
    found = new = 0
    for music_dir in MUSIC_DIRS:
        if not music_dir.exists():
            continue
        for root, dirs, files in os.walk(str(music_dir)):
            for f in files:
                if Path(f).suffix.lower() not in AUDIO_EXTS:
                    continue
                filepath = os.path.join(root, f)
                fid = file_id(filepath)
                if fid not in db["tracks"]:
                    track = scan_track(filepath)
                    if track:
                        db["tracks"][fid] = track
                        new += 1
                else:
                    # Update flags on existing
                    t = db["tracks"][fid]
                    if "flags" not in t:
                        t["flags"] = classify_title(t.get("title", ""))
                found += 1
    save_db(db)
    return jsonify({"found": found, "new": new, "total": len(db["tracks"])})


@app.route("/api/tracks")
def get_tracks():
    db = load_db()
    tracks = list(db["tracks"].values())
    q = request.args.get("q", "").lower()
    genre = request.args.get("genre", "")
    tag = request.args.get("tag", "")
    flag = request.args.get("flag", "")

    if q:
        words = q.split()
        tracks = [t for t in tracks if all(
            w in t["title"].lower() or w in t["artist"].lower() for w in words
        )]
    if genre == "__none__":
        tracks = [t for t in tracks if not t.get("genre")]
    elif genre:
        tracks = [t for t in tracks if t.get("genre", "").lower() == genre.lower()]
    if tag:
        tag_q = tag.lower()
        tracks = [t for t in tracks if any(tag_q in ct.lower() for ct in t.get("custom_tags", []))]
    if flag:
        tracks = [t for t in tracks if flag in t.get("flags", [])]

    # Backfill file_size for tracks missing it
    for t in tracks:
        if "file_size" not in t and t.get("path"):
            try:
                t["file_size"] = os.path.getsize(t["path"])
            except OSError:
                t["file_size"] = 0

    tracks.sort(key=lambda t: (t["artist"].lower(), t["title"].lower()))
    return jsonify({"tracks": tracks, "total": len(tracks)})


@app.route("/api/tracks/<track_id>", methods=["PATCH"])
def update_track(track_id):
    db = load_db()
    if track_id not in db["tracks"]:
        return jsonify({"error": "Track not found"}), 404

    data = request.json
    track = db["tracks"][track_id]

    if "genre" in data:
        track["genre"] = data["genre"]
        try:
            m = mutagen.File(track["path"], easy=True)
            if m is not None:
                m["genre"] = data["genre"]
                m.save()
        except Exception:
            pass
    if "custom_tags" in data:
        track["custom_tags"] = data["custom_tags"]
    if "title" in data:
        track["title"] = data["title"]
    if "artist" in data:
        track["artist"] = data["artist"]

    save_db(db)
    return jsonify(track)


@app.route("/api/tracks/batch-genre", methods=["POST"])
def batch_genre():
    """Auto-tag genres via Spotify for tracks missing genre."""
    db = load_db()
    data = request.json
    track_ids = data.get("ids", [])
    updated = 0

    for tid in track_ids:
        if tid not in db["tracks"]:
            continue
        t = db["tracks"][tid]
        if t.get("genre"):
            continue
        genre = spotify_search(t["title"], t["artist"])
        if genre:
            t["genre"] = genre
            try:
                m = mutagen.File(t["path"], easy=True)
                if m is not None:
                    m["genre"] = genre
                    m.save()
            except Exception:
                pass
            updated += 1

    save_db(db)
    return jsonify({"updated": updated, "total": len(track_ids)})


@app.route("/api/spotify-genre", methods=["POST"])
def spotify_genre_lookup():
    """Lookup genre for a single track via Spotify."""
    data = request.json
    genre = spotify_search(data.get("title", ""), data.get("artist", ""))
    return jsonify({"genre": genre})


@app.route("/api/tags")
def get_tags():
    db = load_db()
    genres = set()
    custom_tags = set()
    for t in db["tracks"].values():
        if t.get("genre"):
            genres.add(t["genre"])
        for ct in t.get("custom_tags", []):
            custom_tags.add(ct)
    return jsonify({"genres": sorted(genres), "custom_tags": sorted(custom_tags)})


@app.route("/api/cover/<fid>")
def serve_cover(fid):
    cover_path = COVERS_DIR / f"{fid}.jpg"
    if cover_path.exists():
        return send_file(str(cover_path), mimetype="image/jpeg")
    return "", 404


@app.route("/api/audio")
def serve_audio():
    filepath = request.args.get("path", "")
    if not filepath or not os.path.exists(filepath):
        return "Not found", 404
    return send_file(filepath)


@app.route("/api/audio/full")
def serve_audio_full():
    """Serve complete audio file for Web Audio API waveform decoding (no range requests)."""
    filepath = request.args.get("path", "")
    if not filepath or not os.path.exists(filepath):
        return "Not found", 404
    return send_file(filepath, conditional=False)


@app.route("/api/stats")
def stats():
    db = load_db()
    tracks = list(db["tracks"].values())
    genres = {}
    no_genre = 0
    video_count = 0
    lyrics_count = 0
    for t in tracks:
        g = t.get("genre", "")
        if g:
            genres[g] = genres.get(g, 0) + 1
        else:
            no_genre += 1
        flags = t.get("flags", [])
        if "video" in flags:
            video_count += 1
        if "lyrics" in flags or "visualizer" in flags:
            lyrics_count += 1

    artists = {}
    for t in tracks:
        a = t.get("artist", "Unknown")
        artists[a] = artists.get(a, 0) + 1

    return jsonify({
        "total": len(tracks),
        "no_genre": no_genre,
        "video_count": video_count,
        "lyrics_count": lyrics_count,
        "genres": dict(sorted(genres.items(), key=lambda x: -x[1])[:20]),
        "top_artists": dict(sorted(artists.items(), key=lambda x: -x[1])[:20]),
    })


# ── Playlists ──

@app.route("/api/playlists")
def list_playlists():
    db = load_db()
    playlists = []
    for pid, pl in db.get("playlists", {}).items():
        playlists.append({"id": pid, "name": pl["name"], "count": len(pl.get("track_ids", []))})
    return jsonify({"playlists": playlists})


@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    db = load_db()
    data = request.json
    pid = hashlib.md5(data["name"].encode()).hexdigest()[:10]
    if "playlists" not in db:
        db["playlists"] = {}
    db["playlists"][pid] = {"name": data["name"], "track_ids": data.get("track_ids", [])}
    save_db(db)
    return jsonify({"id": pid, "name": data["name"]})


@app.route("/api/playlists/<pid>")
def get_playlist(pid):
    db = load_db()
    pl = db.get("playlists", {}).get(pid)
    if not pl:
        return jsonify({"error": "Not found"}), 404
    tracks = [db["tracks"][tid] for tid in pl.get("track_ids", []) if tid in db["tracks"]]
    return jsonify({"id": pid, "name": pl["name"], "tracks": tracks})


@app.route("/api/playlists/<pid>", methods=["PATCH"])
def update_playlist(pid):
    db = load_db()
    if pid not in db.get("playlists", {}):
        return jsonify({"error": "Not found"}), 404
    data = request.json
    if "name" in data:
        db["playlists"][pid]["name"] = data["name"]
    if "track_ids" in data:
        db["playlists"][pid]["track_ids"] = data["track_ids"]
    if "add_track" in data:
        tid = data["add_track"]
        if tid not in db["playlists"][pid]["track_ids"]:
            db["playlists"][pid]["track_ids"].append(tid)
    save_db(db)
    return jsonify(db["playlists"][pid])


@app.route("/api/playlists/<pid>", methods=["DELETE"])
def delete_playlist(pid):
    db = load_db()
    db.get("playlists", {}).pop(pid, None)
    save_db(db)
    return jsonify({"deleted": True})


# ── Serato crates ──

@app.route("/api/serato/crates")
def list_serato_crates():
    crates = []
    for d in [SERATO_DIR, SERATO_BACKUP]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.crate")):
            name = f.stem.replace("%%", " > ")
            paths = parse_serato_crate(f)
            crates.append({"name": name, "path": str(f), "count": len(paths), "source": "backup" if "Backup" in str(d) else "live"})
    return jsonify({"crates": crates})


@app.route("/api/serato/crates/import", methods=["POST"])
def import_serato_crate():
    """Import a Serato crate as a playlist."""
    data = request.json
    crate_path = Path(data["path"])
    if not crate_path.exists():
        return jsonify({"error": "Crate not found"}), 404

    file_paths = parse_serato_crate(crate_path)
    db = load_db()

    track_ids = []
    for fp in file_paths:
        fid = file_id(fp)
        if fid in db["tracks"]:
            track_ids.append(fid)
        elif os.path.exists(fp):
            track = scan_track(fp)
            if track:
                db["tracks"][fid] = track
                track_ids.append(fid)

    name = data.get("name", crate_path.stem.replace("%%", " > "))
    pid = hashlib.md5(name.encode()).hexdigest()[:10]
    if "playlists" not in db:
        db["playlists"] = {}
    db["playlists"][pid] = {"name": f"[Serato] {name}", "track_ids": track_ids}
    save_db(db)

    return jsonify({"id": pid, "name": f"[Serato] {name}", "matched": len(track_ids), "total": len(file_paths)})


@app.route("/api/serato/export", methods=["POST"])
def export_to_serato():
    """Export a playlist as a Serato .crate file."""
    data = request.json
    pid = data.get("playlist_id")
    db = load_db()
    pl = db.get("playlists", {}).get(pid)
    if not pl:
        return jsonify({"error": "Playlist not found"}), 404

    tracks = [db["tracks"][tid] for tid in pl.get("track_ids", []) if tid in db["tracks"]]
    crate_name = pl["name"].replace("[Serato] ", "").replace(" > ", "%%")

    # Build Serato crate binary
    buf = bytearray()
    # Version header
    ver = "1.0/Serato ScratchLive Crate".encode("utf-16-be")
    buf += b"vrsn" + struct.pack(">I", len(ver)) + ver

    for t in tracks:
        path = t["path"]
        if path.startswith("/"):
            path = path[1:]  # Serato paths don't have leading /
        path_bytes = path.encode("utf-16-be")
        buf += b"otrk" + struct.pack(">I", len(path_bytes) + 8)
        buf += b"ptrk" + struct.pack(">I", len(path_bytes)) + path_bytes

    dest = SERATO_DIR / f"{crate_name}.crate"
    dest.write_bytes(bytes(buf))

    return jsonify({"exported": True, "path": str(dest), "tracks": len(tracks)})


# ── Direct Serato crate management ──

def _write_crate(name, track_paths):
    """Write a Serato .crate file from a list of file paths."""
    buf = bytearray()
    ver = "1.0/Serato ScratchLive Crate".encode("utf-16-be")
    buf += b"vrsn" + struct.pack(">I", len(ver)) + ver
    for path in track_paths:
        p = path[1:] if path.startswith("/") else path
        path_bytes = p.encode("utf-16-be")
        buf += b"otrk" + struct.pack(">I", len(path_bytes) + 8)
        buf += b"ptrk" + struct.pack(">I", len(path_bytes)) + path_bytes
    crate_name = name.replace(" > ", "%%")
    dest = SERATO_DIR / f"{crate_name}.crate"
    dest.write_bytes(bytes(buf))
    return str(dest)


@app.route("/api/serato/crates/<path:crate_name>/tracks", methods=["GET"])
def get_crate_tracks(crate_name):
    """Get tracks in a Serato crate with full metadata."""
    real_name = crate_name.replace(" > ", "%%")
    crate_path = SERATO_DIR / f"{real_name}.crate"
    if not crate_path.exists():
        return jsonify({"error": "Crate not found"}), 404
    file_paths = parse_serato_crate(crate_path)
    db = load_db()
    tracks = []
    for fp in file_paths:
        fid = file_id(fp)
        if fid in db["tracks"]:
            tracks.append(db["tracks"][fid])
        elif os.path.exists(fp):
            track = scan_track(fp)
            if track:
                db["tracks"][fid] = track
                tracks.append(track)
    save_db(db)
    return jsonify({"name": crate_name, "tracks": tracks})


@app.route("/api/serato/crates/<path:crate_name>/add", methods=["POST"])
def add_to_crate(crate_name):
    """Add a track to a Serato crate by track ID."""
    data = request.json
    tid = data.get("track_id")
    db = load_db()
    if tid not in db["tracks"]:
        return jsonify({"error": "Track not found"}), 404
    track_path = db["tracks"][tid]["path"]

    real_name = crate_name.replace(" > ", "%%")
    crate_path = SERATO_DIR / f"{real_name}.crate"
    existing = parse_serato_crate(crate_path) if crate_path.exists() else []

    if track_path not in existing:
        existing.append(track_path)
    _write_crate(crate_name, existing)
    return jsonify({"added": True, "tracks": len(existing)})


@app.route("/api/serato/crates/<path:crate_name>/remove", methods=["POST"])
def remove_from_crate(crate_name):
    """Remove a track from a Serato crate."""
    data = request.json
    tid = data.get("track_id")
    db = load_db()
    if tid not in db["tracks"]:
        return jsonify({"error": "Track not found"}), 404
    track_path = db["tracks"][tid]["path"]

    real_name = crate_name.replace(" > ", "%%")
    crate_path = SERATO_DIR / f"{real_name}.crate"
    existing = parse_serato_crate(crate_path) if crate_path.exists() else []
    existing = [p for p in existing if p != track_path]
    _write_crate(crate_name, existing)
    return jsonify({"removed": True, "tracks": len(existing)})


@app.route("/api/serato/crates/<path:crate_name>/reorder", methods=["POST"])
def reorder_crate(crate_name):
    """Reorder tracks in a crate. Expects {"track_ids": [...]} in new order."""
    data = request.json
    track_ids = data.get("track_ids", [])
    db = load_db()
    paths = []
    for tid in track_ids:
        if tid in db["tracks"]:
            paths.append(db["tracks"][tid]["path"])
    _write_crate(crate_name, paths)
    return jsonify({"reordered": True, "tracks": len(paths)})


@app.route("/api/serato/crates/<path:crate_name>/rename", methods=["POST"])
def rename_crate(crate_name):
    """Rename a Serato crate."""
    data = request.json
    new_name = data.get("name", "")
    if not new_name:
        return jsonify({"error": "Name required"}), 400

    real_name = crate_name.replace(" > ", "%%")
    crate_path = SERATO_DIR / f"{real_name}.crate"
    existing = parse_serato_crate(crate_path) if crate_path.exists() else []

    # Write new, delete old
    new_path = _write_crate(new_name, existing)
    if crate_path.exists() and str(crate_path) != new_path:
        crate_path.unlink()
    return jsonify({"renamed": True, "old": crate_name, "new": new_name})


@app.route("/api/serato/crates/create", methods=["POST"])
def create_crate():
    """Create a new empty Serato crate."""
    data = request.json
    name = data.get("name", "")
    if not name:
        return jsonify({"error": "Name required"}), 400
    _write_crate(name, [])
    return jsonify({"created": True, "name": name})


# ── Rekordbox XML export ──

REKORDBOX_XML = Path.home() / "Music" / "rekordbox-amapiano.xml"
REKORDBOX_KINDS = {
    ".mp3": "MP3 File", ".m4a": "M4A File", ".aac": "AAC File",
    ".wav": "WAV File", ".flac": "FLAC File", ".ogg": "OGG File", ".opus": "OGG File",
}


def _build_rekordbox_xml(groups, output_path):
    """Write a rekordbox-importable XML (DJ_PLAYLISTS 1.0.0).

    groups = [{"name": folder_name_or_None, "playlists": [{"name": str, "tracks": [track dicts]}]}]
    A group with name None puts its playlists at the root level.
    """
    import xml.etree.ElementTree as ET

    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.0.0", Company="AlphaTheta")

    collection = ET.SubElement(root, "COLLECTION")
    track_keys = {}  # file path -> TrackID
    for group in groups:
        for pl in group["playlists"]:
            for t in pl["tracks"]:
                path = t["path"]
                if path in track_keys or not os.path.exists(path):
                    continue
                tid = str(len(track_keys) + 1)
                track_keys[path] = tid
                ET.SubElement(collection, "TRACK", {
                    "TrackID": tid,
                    "Name": t.get("title") or Path(path).stem,
                    "Artist": t.get("artist") or "",
                    "Album": t.get("album") or "",
                    "Genre": t.get("genre") or "",
                    "Kind": REKORDBOX_KINDS.get(Path(path).suffix.lower(), "MP3 File"),
                    "Size": str(t.get("file_size") or 0),
                    "TotalTime": str(int(t.get("duration") or 0)),
                    "Location": "file://localhost" + urllib.parse.quote(path),
                })
    collection.set("Entries", str(len(track_keys)))

    playlists_el = ET.SubElement(root, "PLAYLISTS")
    root_node = ET.SubElement(playlists_el, "NODE", Type="0", Name="ROOT")

    def _add_playlist_node(parent, pl):
        keys = []
        seen = set()
        for t in pl["tracks"]:
            k = track_keys.get(t["path"])
            if k and k not in seen:
                seen.add(k)
                keys.append(k)
        node = ET.SubElement(parent, "NODE", Name=pl["name"], Type="1",
                             KeyType="0", Entries=str(len(keys)))
        for k in keys:
            ET.SubElement(node, "TRACK", Key=k)

    for group in groups:
        if group.get("name"):
            folder = ET.SubElement(root_node, "NODE", Type="0", Name=group["name"],
                                   Count=str(len(group["playlists"])))
            for pl in group["playlists"]:
                _add_playlist_node(folder, pl)
        else:
            for pl in group["playlists"]:
                _add_playlist_node(root_node, pl)
    root_node.set("Count", str(len(list(root_node))))

    if hasattr(ET, "indent"):
        ET.indent(root)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return {"tracks": len(track_keys), "playlists": sum(len(g["playlists"]) for g in groups)}


def _crate_tracks_for_export(crate_path, db):
    """Resolve a Serato crate's file paths to track dicts (stub for files not in the db)."""
    tracks = []
    for fp in parse_serato_crate(crate_path):
        t = db["tracks"].get(file_id(fp))
        if t:
            tracks.append(t)
        elif os.path.exists(fp):
            tracks.append({"path": fp, "title": Path(fp).stem, "artist": "",
                           "album": "", "genre": "", "duration": 0, "file_size": 0})
    return tracks


def _export_all_rekordbox(output_path=None):
    """Export every playlist and every live Serato crate into one rekordbox XML."""
    db = load_db()

    app_pls = []
    for pl in db.get("playlists", {}).values():
        tracks = [db["tracks"][tid] for tid in pl.get("track_ids", []) if tid in db["tracks"]]
        if tracks:
            app_pls.append({"name": pl["name"], "tracks": tracks})

    crate_pls = []
    if SERATO_DIR.exists():
        for f in sorted(SERATO_DIR.glob("*.crate")):
            tracks = _crate_tracks_for_export(f, db)
            if tracks:
                crate_pls.append({"name": f.stem.replace("%%", " > "), "tracks": tracks})

    groups = []
    if app_pls:
        groups.append({"name": "Playlists", "playlists": app_pls})
    if crate_pls:
        groups.append({"name": "Serato Crates", "playlists": crate_pls})

    dest = Path(output_path) if output_path else REKORDBOX_XML
    stats = _build_rekordbox_xml(groups, dest)
    return {"path": str(dest), **stats}


@app.route("/api/rekordbox/export", methods=["POST"])
def export_to_rekordbox():
    """Export a playlist, a Serato crate, or the whole library as rekordbox XML."""
    data = request.json or {}
    output_path = data.get("output_path")
    db = load_db()

    if data.get("playlist_id"):
        pl = db.get("playlists", {}).get(data["playlist_id"])
        if not pl:
            return jsonify({"error": "Playlist not found"}), 404
        tracks = [db["tracks"][tid] for tid in pl.get("track_ids", []) if tid in db["tracks"]]
        name = pl["name"].replace("[Serato] ", "")
        safe = re.sub(r"[^\w\s\-]", "", name).strip() or "playlist"
        dest = Path(output_path) if output_path else Path.home() / "Music" / f"rekordbox-{safe}.xml"
        stats = _build_rekordbox_xml([{"name": None, "playlists": [{"name": name, "tracks": tracks}]}], dest)
        return jsonify({"exported": True, "path": str(dest), **stats})

    if data.get("crate_name"):
        real_name = data["crate_name"].replace(" > ", "%%")
        crate_path = SERATO_DIR / f"{real_name}.crate"
        if not crate_path.exists():
            return jsonify({"error": "Crate not found"}), 404
        tracks = _crate_tracks_for_export(crate_path, db)
        name = data["crate_name"]
        safe = re.sub(r"[^\w\s\-]", "", name).strip() or "crate"
        dest = Path(output_path) if output_path else Path.home() / "Music" / f"rekordbox-{safe}.xml"
        stats = _build_rekordbox_xml([{"name": None, "playlists": [{"name": name, "tracks": tracks}]}], dest)
        return jsonify({"exported": True, "path": str(dest), **stats})

    return jsonify({"exported": True, **_export_all_rekordbox(output_path)})


# ── USB export: prepare a stick for Serato + rekordbox in one shot ──

_usb_jobs = {}
_usb_lock = threading.Lock()

_FAT32_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def _fat32_safe(name):
    return (_FAT32_BAD.sub("_", name).strip().rstrip(". ") or "untitled")[:120]


def _serato_field(tag, payload):
    return tag + struct.pack(">I", len(payload)) + payload


def _write_usb_serato_database(usb_root, rel_paths):
    """Write minimal _Serato_/database V2 — without it Serato won't mount the drive's crates."""
    buf = bytearray()
    buf += _serato_field(b"vrsn", "2.0/Serato Scratch LiveDatabase".encode("utf-16-be"))
    for rel in rel_paths:
        ext = Path(rel).suffix.lstrip(".").lower() or "mp3"
        inner = _serato_field(b"ttyp", ext.encode("utf-16-be"))
        inner += _serato_field(b"pfil", rel.encode("utf-16-be"))
        buf += _serato_field(b"otrk", inner)
    serato_dir = usb_root / "_Serato_"
    serato_dir.mkdir(exist_ok=True)
    (serato_dir / "database V2").write_bytes(bytes(buf))


def _write_usb_crate(usb_root, name, rel_paths):
    """Write a Serato .crate on the stick with volume-relative paths."""
    buf = bytearray()
    buf += _serato_field(b"vrsn", "1.0/Serato ScratchLive Crate".encode("utf-16-be"))
    for rel in rel_paths:
        pb = rel.encode("utf-16-be")
        buf += b"otrk" + struct.pack(">I", len(pb) + 8)
        buf += _serato_field(b"ptrk", pb)
    subcrates = usb_root / "_Serato_" / "Subcrates"
    subcrates.mkdir(parents=True, exist_ok=True)
    crate_file = _fat32_safe(name.replace(" > ", "%%")) + ".crate"
    (subcrates / crate_file).write_bytes(bytes(buf))


def _clean_appledouble(root):
    """Delete macOS ._ ghost files + .DS_Store — they make USB folders look empty/broken on players."""
    removed = 0
    for dirpath, _dirs, files in os.walk(str(root)):
        for f in files:
            if f.startswith("._") or f == ".DS_Store":
                try:
                    os.remove(os.path.join(dirpath, f))
                    removed += 1
                except OSError:
                    pass
    return removed


def _usb_prepare_worker(job_id, usb_root, selections):
    """Copy tracks + write Serato crates/database + rekordbox XML onto the stick."""
    job = _usb_jobs[job_id]
    try:
        music_root = usb_root / "Music"
        src_to_rel = {}   # source path -> USB-relative path (dedupe across playlists)
        plan = []         # (selection_name, [track dicts])

        for sel in selections:
            plan.append((sel["name"], sel["tracks"]))

        # Preflight: bytes that actually need copying vs free space
        to_copy = 0
        seen = set()
        for _name, tracks in plan:
            for t in tracks:
                src = t["path"]
                if src in seen or not os.path.exists(src):
                    continue
                seen.add(src)
                folder = music_root / _fat32_safe(_name)
                dest = folder / _fat32_safe(Path(src).name)
                if not (dest.exists() and dest.stat().st_size == os.path.getsize(src)):
                    to_copy += os.path.getsize(src)
        free = __import__("shutil").disk_usage(str(usb_root)).free
        if to_copy > free - 100 * 1024 * 1024:
            job["status"] = "error"
            job["error"] = f"Not enough space: need {to_copy // (1024*1024)} MB, only {free // (1024*1024)} MB free"
            return

        job["bytes_total"] = to_copy
        job["total_files"] = len(seen)
        job["status"] = "copying"

        import shutil
        for sel_name, tracks in plan:
            folder_name = _fat32_safe(sel_name)
            folder = music_root / folder_name
            for t in tracks:
                src = t["path"]
                if src in src_to_rel or not os.path.exists(src):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                fname = _fat32_safe(Path(src).name)
                dest = folder / fname
                size = os.path.getsize(src)
                if dest.exists() and dest.stat().st_size == size:
                    job["skipped"] += 1
                else:
                    # copyfile (not copy2): metadata copy on FAT32 spawns ._ AppleDouble ghosts
                    shutil.copyfile(src, dest)
                    job["bytes_done"] += size
                src_to_rel[src] = f"Music/{folder_name}/{fname}"
                job["copied"] += 1
                job["current"] = f"{t.get('artist', '')} - {t.get('title', fname)}"

        job["status"] = "writing"

        # Serato: one crate per selection + database V2 over everything
        for sel_name, tracks in plan:
            rels = [src_to_rel[t["path"]] for t in tracks if t["path"] in src_to_rel]
            if rels:
                _write_usb_crate(usb_root, sel_name, rels)
        _write_usb_serato_database(usb_root, sorted(set(src_to_rel.values())))

        # rekordbox: XML on the stick, locations pointing at the stick
        rb_groups = []
        rb_pls = []
        for sel_name, tracks in plan:
            rb_tracks = []
            for t in tracks:
                rel = src_to_rel.get(t["path"])
                if rel:
                    rb_tracks.append({**t, "path": str(usb_root / rel)})
            if rb_tracks:
                rb_pls.append({"name": sel_name, "tracks": rb_tracks})
        if rb_pls:
            rb_groups.append({"name": None, "playlists": rb_pls})
            _build_rekordbox_xml(rb_groups, usb_root / "rekordbox-import.xml")

        job["ghosts_removed"] = _clean_appledouble(music_root) + _clean_appledouble(usb_root / "_Serato_")
        job["crates"] = len(plan)
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/usb/volumes")
def usb_volumes():
    """List writable external volumes."""
    vols = []
    volumes_dir = Path("/Volumes")
    for v in sorted(volumes_dir.iterdir()):
        if v.is_symlink() or not v.is_dir():
            continue
        if not os.path.ismount(str(v)) or not os.access(str(v), os.W_OK):
            continue
        try:
            import shutil
            du = shutil.disk_usage(str(v))
            vols.append({"name": v.name, "path": str(v), "free_mb": du.free // (1024 * 1024),
                         "total_mb": du.total // (1024 * 1024),
                         "has_serato": (v / "_Serato_").exists()})
        except OSError:
            continue
    return jsonify({"volumes": vols})


@app.route("/api/usb/prepare", methods=["POST"])
def usb_prepare():
    """Prepare a USB stick: copy selected playlists/crates, write Serato database V2 +
    crates and a rekordbox XML. Body: {volume, playlist_ids: [], crate_names: []}."""
    data = request.json or {}
    volume = data.get("volume", "")
    usb_root = Path(volume)
    if not data.get("_test_dir") and (not volume.startswith("/Volumes/") or not os.path.ismount(volume)):
        return jsonify({"error": "volume must be a mounted drive under /Volumes"}), 400
    if not usb_root.exists() or not os.access(str(usb_root), os.W_OK):
        return jsonify({"error": "volume not found or not writable"}), 400

    db = load_db()
    selections = []
    for pid in data.get("playlist_ids", []):
        pl = db.get("playlists", {}).get(pid)
        if not pl:
            return jsonify({"error": f"playlist not found: {pid}"}), 404
        tracks = [db["tracks"][tid] for tid in pl.get("track_ids", []) if tid in db["tracks"]]
        if tracks:
            selections.append({"name": pl["name"].replace("[Serato] ", ""), "tracks": tracks})
    for cname in data.get("crate_names", []):
        crate_path = SERATO_DIR / (cname.replace(" > ", "%%") + ".crate")
        if not crate_path.exists():
            return jsonify({"error": f"crate not found: {cname}"}), 404
        tracks = _crate_tracks_for_export(crate_path, db)
        if tracks:
            selections.append({"name": cname, "tracks": tracks})
    if not selections:
        return jsonify({"error": "nothing selected (or all selections are empty)"}), 400

    job_id = hashlib.md5(f"{volume}{time.time()}".encode()).hexdigest()[:10]
    with _usb_lock:
        _usb_jobs[job_id] = {"id": job_id, "status": "starting", "volume": volume,
                             "copied": 0, "skipped": 0, "total_files": 0,
                             "bytes_done": 0, "bytes_total": 0, "current": "",
                             "crates": 0, "error": None}
    t = threading.Thread(target=_usb_prepare_worker, args=(job_id, usb_root, selections), daemon=True)
    t.start()
    return jsonify({"job_id": job_id, "selections": len(selections),
                    "tracks": sum(len(s["tracks"]) for s in selections)})


@app.route("/api/usb/jobs/<job_id>")
def usb_job_status(job_id):
    job = _usb_jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


# ── rekordbox device-export database (.pdb) parser ──
# Format is reverse-engineered (Deep Symmetry's rekordbox_pdb analysis).
# We only read what the converter needs: tracks (title + file path) and playlists.

_PDB_TRACKS, _PDB_PLAYLIST_TREE, _PDB_PLAYLIST_ENTRIES = 0, 5, 6


def _pdb_string(data, pos):
    """Read a DeviceSQL string. Short: 1 header byte, ascii. Long: kind, u2 len, pad, data."""
    kind = data[pos]
    if kind & 1:
        n = (kind >> 1) - 1
        return data[pos + 1 : pos + 1 + n].decode("ascii", "replace")
    length = struct.unpack_from("<H", data, pos + 1)[0]
    raw = data[pos + 4 : pos + length]
    if kind == 0x90:
        return raw.decode("utf-16-le", "replace")
    return raw.decode("ascii", "replace")


def _pdb_data_rows(data, page_len, first_page, last_page):
    """Yield absolute row positions for every present row in a table's page chain."""
    idx, seen = first_page, set()
    while idx and idx not in seen:
        seen.add(idx)
        page = idx * page_len
        if page + 40 > len(data):
            break
        next_page = struct.unpack_from("<I", data, page + 12)[0]
        num_rows_small = data[page + 24]
        page_flags = data[page + 27]
        num_rows_large = struct.unpack_from("<H", data, page + 34)[0]
        if (page_flags & 0x40) == 0:  # data page
            num_rows = num_rows_small
            if num_rows_large > num_rows_small and num_rows_large != 0x1FFF:
                num_rows = num_rows_large
            for i in range(num_rows):
                group, j = divmod(i, 16)
                base = page + page_len - group * 0x24
                flags = struct.unpack_from("<H", data, base - 4)[0]
                if not (flags >> j) & 1:
                    continue
                ofs = struct.unpack_from("<H", data, base - 6 - 2 * j)[0]
                yield page + 0x28 + ofs
        if idx == last_page:
            break
        idx = next_page


def parse_rekordbox_pdb(pdb_path):
    """Extract playlists (with folder paths) and track title/file-path from export.pdb."""
    data = Path(pdb_path).read_bytes()
    page_len = struct.unpack_from("<I", data, 4)[0]
    num_tables = struct.unpack_from("<I", data, 8)[0]

    tables = {}
    for i in range(num_tables):
        t, _empty, first, last = struct.unpack_from("<IIII", data, 28 + i * 16)
        tables[t] = (first, last)

    tracks = {}
    if _PDB_TRACKS in tables:
        for row in _pdb_data_rows(data, page_len, *tables[_PDB_TRACKS]):
            track_id = struct.unpack_from("<I", data, row + 74)[0]
            # 21 string-offset slots start at row+96; slot 17 = title, slot 20 = file path
            ofs_title = struct.unpack_from("<H", data, row + 96 + 17 * 2)[0]
            ofs_path = struct.unpack_from("<H", data, row + 96 + 20 * 2)[0]
            duration = struct.unpack_from("<H", data, row + 86)[0]
            path = _pdb_string(data, row + ofs_path)
            if path:
                tracks[track_id] = {"path": path, "title": _pdb_string(data, row + ofs_title),
                                    "duration": duration}

    nodes = {}
    if _PDB_PLAYLIST_TREE in tables:
        for row in _pdb_data_rows(data, page_len, *tables[_PDB_PLAYLIST_TREE]):
            parent_id, _u, sort_order, node_id, raw_is_folder = struct.unpack_from("<IIIII", data, row)
            nodes[node_id] = {"parent": parent_id, "sort": sort_order,
                              "is_folder": raw_is_folder != 0,
                              "name": _pdb_string(data, row + 20)}

    entries = {}
    if _PDB_PLAYLIST_ENTRIES in tables:
        for row in _pdb_data_rows(data, page_len, *tables[_PDB_PLAYLIST_ENTRIES]):
            entry_index, track_id, playlist_id = struct.unpack_from("<III", data, row)
            entries.setdefault(playlist_id, []).append((entry_index, track_id))

    def full_name(nid, depth=0):
        n = nodes.get(nid)
        if not n or depth > 10:
            return ""
        parent = full_name(n["parent"], depth + 1) if n["parent"] else ""
        return f"{parent} > {n['name']}" if parent else n["name"]

    playlists = []
    for nid, n in sorted(nodes.items(), key=lambda kv: kv[1]["sort"]):
        if n["is_folder"]:
            continue
        track_ids = [tid for _idx, tid in sorted(entries.get(nid, []))]
        if track_ids:
            playlists.append({"name": full_name(nid), "track_ids": track_ids})

    return {"tracks": tracks, "playlists": playlists}


# ── USB convert: make an existing Serato or rekordbox stick work in both ──

def _parse_usb_serato_db_paths(usb_root):
    """Existing database V2 pfil entries (USB-relative paths), if any."""
    db_file = usb_root / "_Serato_" / "database V2"
    if not db_file.exists():
        return set()
    data = db_file.read_bytes()
    paths, i = set(), 0
    while True:
        idx = data.find(b"pfil", i)
        if idx == -1:
            break
        ln = struct.unpack(">I", data[idx + 4 : idx + 8])[0]
        try:
            paths.add(data[idx + 8 : idx + 8 + ln].decode("utf-16-be").lstrip("/"))
        except Exception:
            pass
        i = idx + 8 + ln
    return paths


def _rekordbox_usb_to_serato(usb_root, job):
    """Read PIONEER/rekordbox/export.pdb and write Serato crates + database V2 for it."""
    parsed = parse_rekordbox_pdb(usb_root / "PIONEER" / "rekordbox" / "export.pdb")
    job["rb_playlists_found"] = len(parsed["playlists"])
    written, all_rels = 0, set()
    for pl in parsed["playlists"]:
        rels = []
        for tid in pl["track_ids"]:
            t = parsed["tracks"].get(tid)
            if not t:
                continue
            rel = t["path"].lstrip("/")
            if (usb_root / rel).exists():
                rels.append(rel)
                all_rels.add(rel)
        if rels:
            _write_usb_crate(usb_root, pl["name"], rels)
            written += 1
        job["serato_crates_written"] = written

    # database V2 must cover existing stick content too, or Serato drops those crates
    all_rels |= _parse_usb_serato_db_paths(usb_root)
    subcrates = usb_root / "_Serato_" / "Subcrates"
    if subcrates.exists():
        for f in subcrates.glob("*.crate"):
            all_rels |= {p.lstrip("/") for p in parse_serato_crate(f)}
    _write_usb_serato_database(usb_root, sorted(all_rels))
    job["database_tracks"] = len(all_rels)


def _serato_usb_to_rekordbox(usb_root, job):
    """Read the stick's Serato crates and write rekordbox-import.xml next to them."""
    meta_cache = {}

    def stick_track(rel):
        if rel in meta_cache:
            return meta_cache[rel]
        fp = usb_root / rel
        if not fp.exists():
            meta_cache[rel] = None
            return None
        t = {"path": str(fp), "title": fp.stem, "artist": "", "album": "", "genre": "",
             "duration": 0, "file_size": 0}
        try:
            m = mutagen.File(str(fp), easy=True)
            if m:
                t["title"] = (m.get("title") or [fp.stem])[0]
                t["artist"] = (m.get("artist") or [""])[0]
                t["album"] = (m.get("album") or [""])[0]
                t["genre"] = (m.get("genre") or [""])[0]
                t["duration"] = m.info.length if m.info else 0
            t["file_size"] = fp.stat().st_size
        except Exception:
            pass
        meta_cache[rel] = t
        return t

    crates = []
    crate_files = sorted((usb_root / "_Serato_" / "Subcrates").glob("*.crate"))
    for f in crate_files:
        tracks = []
        for p in parse_serato_crate(f):
            t = stick_track(p.lstrip("/"))
            if t:
                tracks.append(t)
        if tracks:
            crates.append({"name": f.stem.replace("%%", " > "), "tracks": tracks})
        job["current"] = f.stem
        job["xml_crates"] = len(crates)
    if crates:
        stats = _build_rekordbox_xml([{"name": None, "playlists": crates}],
                                     usb_root / "rekordbox-import.xml")
        job["xml_tracks"] = stats["tracks"]


def _usb_convert_worker(job_id, usb_root):
    job = _usb_jobs[job_id]
    try:
        did = []
        if (usb_root / "PIONEER" / "rekordbox" / "export.pdb").exists():
            job["status"] = "converting rekordbox → Serato crates"
            _rekordbox_usb_to_serato(usb_root, job)
            did.append("rekordbox→serato")
        if (usb_root / "_Serato_" / "Subcrates").exists():
            job["status"] = "writing rekordbox XML from Serato crates"
            _serato_usb_to_rekordbox(usb_root, job)
            did.append("serato→rekordbox")
        if not did:
            job["status"] = "error"
            job["error"] = "No Serato (_Serato_/Subcrates) or rekordbox (PIONEER/rekordbox/export.pdb) data found on this drive"
            return
        job["ghosts_removed"] = _clean_appledouble(usb_root / "_Serato_")
        job["converted"] = did
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/api/usb/convert", methods=["POST"])
def usb_convert():
    """Convert a plugged-in USB in place: a rekordbox stick gains Serato crates,
    a Serato stick gains a rekordbox XML. Additive — existing data untouched."""
    data = request.json or {}
    volume = data.get("volume", "")
    usb_root = Path(volume)
    if not data.get("_test_dir") and (not volume.startswith("/Volumes/") or not os.path.ismount(volume)):
        return jsonify({"error": "volume must be a mounted drive under /Volumes"}), 400
    if not usb_root.exists() or not os.access(str(usb_root), os.W_OK):
        return jsonify({"error": "volume not found or not writable"}), 400

    job_id = hashlib.md5(f"cv{volume}{time.time()}".encode()).hexdigest()[:10]
    with _usb_lock:
        _usb_jobs[job_id] = {"id": job_id, "status": "starting", "volume": volume,
                             "current": "", "error": None}
    threading.Thread(target=_usb_convert_worker, args=(job_id, usb_root), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/usb")
def usb_page():
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>USB Export — Amapiano</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#eee;font-family:-apple-system,sans-serif;padding:20px;max-width:900px;margin:0 auto}
h1{font-size:18px;margin-bottom:4px}
.sub{color:#888;font-size:12px;margin-bottom:18px}
a{color:#ff5500;text-decoration:none}
select,input[type=text]{background:#1a1a1a;color:#eee;border:1px solid #333;border-radius:6px;padding:8px 10px;font-size:13px;width:100%}
.row{display:flex;gap:10px;align-items:center;margin-bottom:14px}
.cols{display:flex;gap:14px;flex-wrap:wrap}
.col{flex:1;min-width:280px;background:#141414;border:1px solid #222;border-radius:8px;padding:12px}
.col h3{font-size:12px;color:#888;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;justify-content:space-between}
.col h3 span{cursor:pointer;color:#ff5500;font-size:11px;text-transform:none}
.list{max-height:300px;overflow-y:auto;margin-top:8px}
label.item{display:flex;gap:8px;align-items:center;padding:5px 4px;font-size:13px;cursor:pointer;border-radius:4px}
label.item:hover{background:#1e1e1e}
label.item .cnt{color:#666;font-size:11px;margin-left:auto}
button.go{background:#ff5500;color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:14px;font-weight:700;cursor:pointer;margin-top:16px}
button.go:disabled{background:#333;color:#777;cursor:default}
button.mini{background:#1a1a1a;color:#ccc;border:1px solid #333;border-radius:6px;padding:8px 12px;font-size:12px;cursor:pointer}
.bar{background:#1a1a1a;border-radius:6px;height:14px;overflow:hidden;margin:12px 0 6px}
.bar div{background:#ff5500;height:100%;width:0%;transition:width .5s}
#status{font-size:12px;color:#aaa;white-space:pre-line}
.done{background:#10231a;border:1px solid #1f4a33;border-radius:8px;padding:14px;margin-top:14px;font-size:13px;line-height:1.6;display:none}
.err{color:#ff6b6b}
.vol-meta{font-size:11px;color:#666}
</style></head><body>
<h1>⇪ USB Export</h1>
<div class="sub">Copies music onto the stick and writes Serato crates + database and a rekordbox XML — plug into any laptop with Serato or rekordbox. <a href="/">← back to library</a></div>

<div class="row">
  <select id="vol"></select>
  <button class="mini" onclick="loadVols()">↻ Refresh</button>
</div>
<div class="vol-meta" id="volMeta"></div>

<div class="cols" style="margin-top:14px">
  <div class="col">
    <h3>Playlists <span onclick="toggleAll('pl')">all / none</span></h3>
    <input type="text" placeholder="filter..." oninput="filterList('pl',this.value)">
    <div class="list" id="plList">loading…</div>
  </div>
  <div class="col">
    <h3>Serato Crates <span onclick="toggleAll('cr')">all / none</span></h3>
    <input type="text" placeholder="filter..." oninput="filterList('cr',this.value)">
    <div class="list" id="crList">loading…</div>
  </div>
</div>

<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <button class="go" id="goBtn" onclick="go()">Prepare USB</button>
  <button class="go" id="convBtn" onclick="convertUsb()" style="background:#1a1a1a;border:1px solid #ff5500;color:#ff5500">⇄ Convert plugged-in USB</button>
</div>
<div class="sub" style="margin-top:8px">Convert = already-made stick: a rekordbox USB gains Serato crates, a Serato USB gains a rekordbox XML. Nothing is deleted or moved.</div>
<div class="bar" id="barWrap" style="display:none"><div id="bar"></div></div>
<div id="status"></div>
<div class="done" id="doneBox"></div>

<script>
let vols=[];
async function loadVols(){
  const d=await (await fetch('/api/usb/volumes')).json();
  vols=d.volumes;
  const sel=document.getElementById('vol');
  sel.innerHTML=vols.length?vols.map(v=>`<option value="${v.path}">${v.name} — ${(v.free_mb/1024).toFixed(1)} GB free${v.has_serato?' (has Serato)':''}</option>`).join(''):'<option value="">No USB drive found — plug one in and refresh</option>';
  volMetaUpdate();
}
function volMetaUpdate(){
  const v=vols.find(x=>x.path===document.getElementById('vol').value);
  document.getElementById('volMeta').textContent=v?`${v.path} — ${(v.free_mb/1024).toFixed(1)} of ${(v.total_mb/1024).toFixed(1)} GB free`:'';
}
document.getElementById('vol').addEventListener('change',volMetaUpdate);
async function loadLists(){
  const pls=await (await fetch('/api/playlists')).json();
  document.getElementById('plList').innerHTML=pls.playlists.map(p=>
    `<label class="item" data-name="${p.name.toLowerCase()}"><input type="checkbox" class="pl" value="${p.id}">${p.name}<span class="cnt">${p.count}</span></label>`).join('')||'<div style="color:#555;font-size:12px">none</div>';
  const crs=await (await fetch('/api/serato/crates')).json();
  const live=crs.crates.filter(c=>c.source==='live'&&c.count>0);
  document.getElementById('crList').innerHTML=live.map(c=>
    `<label class="item" data-name="${c.name.toLowerCase()}"><input type="checkbox" class="cr" value="${c.name}">${c.name}<span class="cnt">${c.count}</span></label>`).join('')||'<div style="color:#555;font-size:12px">none</div>';
}
function filterList(cls,q){
  const box=cls==='pl'?'plList':'crList';
  document.querySelectorAll(`#${box} label.item`).forEach(l=>{
    l.style.display=l.dataset.name.includes(q.toLowerCase())?'flex':'none';
  });
}
function toggleAll(cls){
  const boxes=[...document.querySelectorAll(`input.${cls}`)].filter(b=>b.closest('label').style.display!=='none');
  const on=boxes.some(b=>!b.checked);
  boxes.forEach(b=>b.checked=on);
}
async function go(){
  const volume=document.getElementById('vol').value;
  if(!volume){alert('Plug in a USB drive first');return}
  const playlist_ids=[...document.querySelectorAll('input.pl:checked')].map(b=>b.value);
  const crate_names=[...document.querySelectorAll('input.cr:checked')].map(b=>b.value);
  if(!playlist_ids.length&&!crate_names.length){alert('Select at least one playlist or crate');return}
  const btn=document.getElementById('goBtn');btn.disabled=true;btn.textContent='Preparing…';
  document.getElementById('doneBox').style.display='none';
  const r=await (await fetch('/api/usb/prepare',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({volume,playlist_ids,crate_names})})).json();
  if(r.error){document.getElementById('status').innerHTML=`<span class="err">${r.error}</span>`;btn.disabled=false;btn.textContent='Prepare USB';return}
  document.getElementById('barWrap').style.display='block';
  poll(r.job_id);
}
async function poll(id){
  const j=await (await fetch('/api/usb/jobs/'+id)).json();
  const pct=j.bytes_total?Math.min(100,Math.round(j.bytes_done/j.bytes_total*100)):(j.status==='done'?100:0);
  document.getElementById('bar').style.width=pct+'%';
  const mb=x=>(x/1048576).toFixed(0);
  document.getElementById('status').textContent=
    `${j.status} — ${j.copied}/${j.total_files} files (${j.skipped} already on stick) — ${mb(j.bytes_done)}/${mb(j.bytes_total)} MB\\n${j.current||''}`;
  if(j.status==='done'){
    document.getElementById('bar').style.width='100%';
    const btn=document.getElementById('goBtn');btn.disabled=false;btn.textContent='Prepare USB';
    const box=document.getElementById('doneBox');box.style.display='block';
    box.innerHTML=`<b>✓ USB ready</b> — ${j.copied} tracks, ${j.crates} crates, ${j.ghosts_removed} ghost files cleaned.<br>
      <b>Serato:</b> eject, plug into any laptop — crates appear at the bottom of the crate panel under the drive name.<br>
      <b>rekordbox:</b> on the laptop, Preferences &gt; Advanced &gt; Database &gt; rekordbox xml → select <code>rekordbox-import.xml</code> on the stick, enable View &gt; Layout &gt; rekordbox xml, import from sidebar.<br>
      <span style="color:#888">Standalone CDJs need rekordbox's own "export to device" — do that from rekordbox after importing the xml.</span>`;
    return;
  }
  if(j.status==='error'){
    document.getElementById('status').innerHTML=`<span class="err">${j.error}</span>`;
    const btn=document.getElementById('goBtn');btn.disabled=false;btn.textContent='Prepare USB';
    return;
  }
  setTimeout(()=>poll(id),1500);
}
async function convertUsb(){
  const volume=document.getElementById('vol').value;
  if(!volume){alert('Plug in a USB drive first');return}
  const btn=document.getElementById('convBtn');btn.disabled=true;btn.textContent='Converting…';
  document.getElementById('doneBox').style.display='none';
  const r=await (await fetch('/api/usb/convert',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({volume})})).json();
  if(r.error){document.getElementById('status').innerHTML=`<span class="err">${r.error}</span>`;btn.disabled=false;btn.textContent='⇄ Convert plugged-in USB';return}
  pollConvert(r.job_id);
}
async function pollConvert(id){
  const j=await (await fetch('/api/usb/jobs/'+id)).json();
  document.getElementById('status').textContent=`${j.status}${j.current?' — '+j.current:''}`;
  const btn=document.getElementById('convBtn');
  if(j.status==='done'){
    btn.disabled=false;btn.textContent='⇄ Convert plugged-in USB';
    const box=document.getElementById('doneBox');box.style.display='block';
    let h=`<b>✓ USB converted</b> (${(j.converted||[]).join(', ')})<br>`;
    if(j.serato_crates_written!==undefined)h+=`<b>Serato:</b> ${j.serato_crates_written} crates written from ${j.rb_playlists_found} rekordbox playlists, database V2 covers ${j.database_tracks} tracks — plug into Serato and the crates appear under the drive.<br>`;
    if(j.xml_crates!==undefined)h+=`<b>rekordbox:</b> ${j.xml_crates} crates → rekordbox-import.xml (${j.xml_tracks||0} tracks) on the stick — point rekordbox prefs at that file to import.<br>`;
    box.innerHTML=h;
    return;
  }
  if(j.status==='error'){
    document.getElementById('status').innerHTML=`<span class="err">${j.error}</span>`;
    btn.disabled=false;btn.textContent='⇄ Convert plugged-in USB';
    return;
  }
  setTimeout(()=>pollConvert(id),1500);
}
loadVols();loadLists();
</script></body></html>"""


# ── Downloads via spotdl ──

import subprocess
import threading

_downloads = {}  # id -> {status, url, name, tracks: [], error}
_download_lock = threading.Lock()


def _spotify_embed_info(spotify_url):
    """Get track name+artist from Spotify embed endpoint (no auth needed)."""
    import re as _re
    try:
        m = _re.search(r'spotify\.com/track/([a-zA-Z0-9]+)', spotify_url)
        if not m:
            return None
        track_id = m.group(1)
        embed_url = f"https://open.spotify.com/embed/track/{track_id}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        req = urllib.request.Request(embed_url, headers=headers)
        html = urllib.request.urlopen(req, timeout=15).read().decode()
        import json as _json
        m2 = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html)
        if m2:
            data = _json.loads(m2.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            name = entity["name"]
            artists = ", ".join(a["name"] for a in entity.get("artists", []))
            # Spotify embed API: duration is raw int (ms) in new format, {"milliseconds": N} in old format
            dur_field = entity.get("duration", 0)
            if isinstance(dur_field, dict):
                duration_ms = dur_field.get("milliseconds", 0)
            else:
                duration_ms = dur_field or entity.get("duration_ms", 0)
            return {"name": name, "artists": artists, "duration_s": duration_ms / 1000 if duration_ms else 0}
        # Fallback: regex
        m3 = _re.search(r'"name":"([^"]+)".*?"artists":\[.*?"name":"([^"]+)"', html)
        if m3:
            return {"name": m3.group(1), "artists": m3.group(2), "duration_s": 0}
    except Exception as e:
        print(f"[embed] Error getting info for {spotify_url}: {e}")
    return None


def _scrape_spotify_track_urls(playlist_url):
    """Scrape track URLs from a Spotify playlist/album page using the embed endpoint."""
    try:
        import re as _re
        m = _re.search(r'spotify\.com/(playlist|album)/([a-zA-Z0-9]+)', playlist_url)
        if not m:
            return []
        resource_type, resource_id = m.group(1), m.group(2)

        embed_url = f"https://open.spotify.com/embed/{resource_type}/{resource_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(embed_url, headers=headers)
        html = urllib.request.urlopen(req, timeout=15).read().decode()

        track_ids = _re.findall(r'spotify:track:([a-zA-Z0-9]{22})', html)
        seen = set()
        unique_ids = []
        for tid in track_ids:
            if tid not in seen:
                seen.add(tid)
                unique_ids.append(tid)

        if unique_ids:
            print(f"[scrape] Found {len(unique_ids)} tracks from embed for {resource_type}/{resource_id}")
            return [f"https://open.spotify.com/track/{tid}" for tid in unique_ids]

        # Fallback: try the main page
        req2 = urllib.request.Request(playlist_url, headers=headers)
        html2 = urllib.request.urlopen(req2, timeout=15).read().decode()
        track_ids2 = _re.findall(r'spotify:track:([a-zA-Z0-9]{22})', html2)
        seen2 = set()
        unique_ids2 = []
        for tid in track_ids2:
            if tid not in seen2:
                seen2.add(tid)
                unique_ids2.append(tid)
        if unique_ids2:
            print(f"[scrape] Found {len(unique_ids2)} tracks from main page for {resource_type}/{resource_id}")
            return [f"https://open.spotify.com/track/{tid}" for tid in unique_ids2]

        print(f"[scrape] No tracks found for {playlist_url}")
        return []
    except Exception as e:
        print(f"[scrape] Error scraping {playlist_url}: {e}")
        return []


def _download_spotify_track_via_ytdlp(track_url, output_dir):
    """Download a Spotify track by looking up metadata via embed, then downloading from YouTube.
    Verifies duration to reject garbage results (podcasts, reaction vids, etc)."""
    info = _spotify_embed_info(track_url)
    if not info:
        return None, f"Could not get track info for {track_url}"
    search_query = f"{info['artists']} - {info['name']}"
    expected_dur = info.get("duration_s", 0)

    # Pick the OFFICIAL audio: prefer the label's "<Artist> - Topic" auto-upload (same master
    # as Spotify), then the artist's own channel "Official Audio"; must be within 12% (min 12s)
    # of Spotify's length. Falls back to the first length-matching result. (2026-08-26)
    chosen_url = None
    if expected_dur > 0:
        try:
            r = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s",
                 f"ytsearch8:{info['artists'].split(',')[0].strip()} {info['name']} \"Provided to YouTube\""],
                capture_output=True, text=True, timeout=60
            )
            slack = max(12.0, expected_dur * 0.12)
            _n = lambda x: re.sub(r'[^a-z0-9]', '', (x or '').lower())
            cands = []
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t", 3)
                if len(parts) < 4:
                    continue
                vid, d, ch, ti = parts
                try:
                    d = float(d)
                except ValueError:
                    continue
                if abs(d - expected_dur) <= slack:
                    cands.append((vid, d, ch, ti))
            primary_artist = _n(info['artists'].split(',')[0])
            # Label auto-upload = exact song title on the artist's channel (or "<x> - Topic"),
            # confirmed by a description starting "Provided to YouTube by ..."
            exact = [c for c in cands if _n(c[3]) == _n(info['name'])
                     and (primary_artist in _n(c[2]) or c[2].endswith(' - Topic'))]
            exact += [c for c in cands if c not in exact and c[2].endswith(' - Topic')]
            best = None
            for c in exact[:3]:
                try:
                    desc = subprocess.run(["yt-dlp", "--no-playlist", "--print", "%(description)s",
                                           f"https://www.youtube.com/watch?v={c[0]}"],
                                          capture_output=True, text=True, timeout=40).stdout
                    if desc.lstrip().lower().startswith("provided to youtube"):
                        best = c
                        break
                except Exception:
                    pass
            if not best:
                official = [c for c in cands if primary_artist and primary_artist in _n(c[2])
                            and not re.search(r'video|lyric|live|slowed|sped|remix|instrumental|clean', c[3], re.I)]
                best = (official or cands or [None])[0]
            if best:
                chosen_url = f"https://www.youtube.com/watch?v={best[0]}"
                print(f"[yt-dlp] pick {search_query}: {best[2]} | {best[3]} ({best[1]:.0f}s, expected {expected_dur:.0f}s)", flush=True)
            else:
                msg = f"No result within {slack:.0f}s of expected {expected_dur:.0f}s — skipping"
                print(f"[yt-dlp] REJECTED {search_query}: {msg}", flush=True)
                return None, msg
        except Exception as e:
            print(f"[yt-dlp] search failed for {search_query}: {e}", flush=True)

    output_template = str(output_dir / f"{info['artists']} - {info['name']}.%(ext)s")
    result = subprocess.run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
         "-o", output_template, "--no-playlist",
         chosen_url or f"ytsearch1:{search_query} audio"],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        print(f"[yt-dlp] Downloaded: {search_query} (expected {expected_dur:.0f}s)", flush=True)
    else:
        print(f"[yt-dlp] Failed: {search_query} — {result.stderr[:200]}", flush=True)
    return result, None


def _run_download(download_id, url, playlist_name, meta_name=None):
    """Run download in background thread. Uses yt-dlp for most, spotdl for Spotify.
    meta_name ("Artist - Title") forces a clean output filename for non-Spotify
    URLs, same as the Spotify branch does with embed info — otherwise the
    uploader name bleeds into the artist slot and wrecks Serato sorting."""
    safe_name = re.sub(r'[^\w\s\-]', '', playlist_name).strip() or "Downloads"
    output_dir = Path.home() / "Music" / "yt-dlp" / safe_name
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with _download_lock:
            _downloads[download_id]["status"] = "downloading"

        if "spotify.com" in url and "/track/" not in url:
            # Playlist/album: scrape track URLs, then download each via yt-dlp
            track_urls = _scrape_spotify_track_urls(url)
            if track_urls:
                errors = []
                for i, track_url in enumerate(track_urls):
                    with _download_lock:
                        _downloads[download_id]["progress"] = f"{i+1}/{len(track_urls)}"
                    r, err = _download_spotify_track_via_ytdlp(track_url, output_dir)
                    if err:
                        errors.append(err)
                    elif r and r.returncode != 0:
                        errors.append(r.stderr[:100] or r.stdout[:100])
                class _Result:
                    returncode = 0 if not errors else 1
                    stderr = "; ".join(errors[:3])
                    stdout = ""
                result = _Result()
            else:
                # Fallback: try yt-dlp with playlist URL directly
                result = subprocess.run(
                    ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
                     # Prefer real artist/title from ID3 tags, fall back to title only
                     "-o", str(output_dir / "%(artist,creator,uploader)s - %(track,title)s.%(ext)s"),
                     "--embed-metadata", "--parse-metadata", "title:%(title)s",
                     "--yes-playlist", url],
                    capture_output=True, text=True, timeout=1200
                )
        elif "spotify.com" in url:
            # Single Spotify track — resolve via embed, download via yt-dlp
            r, err = _download_spotify_track_via_ytdlp(url, output_dir)
            if err:
                class _Result:
                    returncode = 1
                    stderr = err
                    stdout = ""
                result = _Result()
            else:
                result = r
            print(f"[spotify-single] done, rc={result.returncode}", flush=True)
        else:
            # Use yt-dlp for YouTube, etc
            is_soundcloud = "soundcloud" in url.lower() or "on.soundcloud.com" in url.lower()

            if is_soundcloud:
                # NEVER hit SoundCloud servers — causes DataDome IP blocks.
                # Extract track name from URL, search YouTube instead.
                print(f"[soundcloud→youtube] Redirecting SoundCloud URL to YouTube search (protecting IP)", flush=True)
                with _download_lock:
                    _downloads[download_id].setdefault("warnings", [])
                    _downloads[download_id]["warnings"].append("SoundCloud URL redirected to YouTube (IP protection)")

                # Resolve short URLs (on.soundcloud.com) and extract artist/track from path
                import re as _re
                resolved_url = url
                if "on.soundcloud.com" in url.lower():
                    try:
                        req = urllib.request.Request(url, method="HEAD",
                            headers={"User-Agent": "Mozilla/5.0"})
                        req.get_method = lambda: "HEAD"
                        resp = urllib.request.urlopen(req, timeout=10)
                        resolved_url = resp.url
                    except Exception:
                        pass

                # Extract artist and track slug from soundcloud.com/artist/track-name
                m = _re.search(r'soundcloud\.com/([^/]+)/([^/?]+)', resolved_url)
                if m:
                    artist_slug = m.group(1).replace("-", " ")
                    track_slug = m.group(2).replace("-", " ")
                    search_query = f"{artist_slug} {track_slug}"
                else:
                    search_query = resolved_url  # last resort

                yt_out = str(output_dir / f"{search_query}.%(ext)s")
                result = subprocess.run(
                    ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
                     "-o", yt_out, "--no-playlist", "--embed-metadata",
                     f"ytsearch1:{search_query} audio"],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    print(f"[soundcloud→youtube] Downloaded via YouTube: {search_query}", flush=True)
                else:
                    print(f"[soundcloud→youtube] Failed: {result.stderr[:200]}", flush=True)
            else:
                if meta_name:
                    safe_track = re.sub(r'[\\/:*?"<>|]', '', meta_name).strip()
                    cmd = ["yt-dlp", "-x", "--audio-format", "mp3",
                         "--audio-quality", "0",
                         "-o", str(output_dir / f"{safe_track}.%(ext)s"),
                         "--no-playlist", url]
                else:
                    cmd = ["yt-dlp", "-x", "--audio-format", "mp3",
                         "--audio-quality", "0",
                         "-o", str(output_dir / "%(artist,creator,uploader)s - %(track,title)s.%(ext)s"),
                         "--embed-metadata",
                         "--no-playlist" if "/track" in url else "--yes-playlist",
                         url]
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=900
                )

        # Scan new files in this playlist folder
        db = load_db()
        new_tracks = []
        for root, dirs, files in os.walk(str(output_dir)):
            for f in files:
                if Path(f).suffix.lower() not in AUDIO_EXTS:
                    continue
                filepath = os.path.join(root, f)
                fid = file_id(filepath)
                if fid not in db["tracks"]:
                    track = scan_track(filepath)
                    if track:
                        db["tracks"][fid] = track
                        new_tracks.append(fid)

        # Reject preview/snippet files (under 35s — SoundCloud Go+ previews are ~30s)
        MIN_TRACK_DURATION = 35
        rejected = []
        for fid in new_tracks[:]:
            track = db["tracks"].get(fid)
            if track and 0 < track.get("duration", 0) < MIN_TRACK_DURATION:
                rejected.append(track)
                new_tracks.remove(fid)
                del db["tracks"][fid]
                try:
                    os.remove(track["path"])
                    print(f"[preview-guard] Deleted preview: {track['artist']} - {track['title']} ({track['duration']}s)", flush=True)
                except OSError:
                    pass
        if rejected:
            names = [f"{t['artist']} - {t['title']} ({t['duration']}s)" for t in rejected]
            with _download_lock:
                _downloads[download_id].setdefault("warnings", [])
                _downloads[download_id]["warnings"].append(
                    f"Rejected {len(rejected)} preview(s) under {MIN_TRACK_DURATION}s: {'; '.join(names)}")

        # Create playlist from downloaded tracks if we got any
        if new_tracks and playlist_name:
            pid = hashlib.md5(playlist_name.encode()).hexdigest()[:10]
            if "playlists" not in db:
                db["playlists"] = {}
            if pid in db["playlists"]:
                # Add to existing
                existing = db["playlists"][pid].get("track_ids", [])
                for tid in new_tracks:
                    if tid not in existing:
                        existing.append(tid)
                db["playlists"][pid]["track_ids"] = existing
            else:
                db["playlists"][pid] = {"name": playlist_name, "track_ids": new_tracks}
            # Auto-export to Serato
            tracks = [db["tracks"][tid] for tid in db["playlists"][pid]["track_ids"] if tid in db["tracks"]]
            crate_name = playlist_name.replace(" > ", "%%")
            _write_crate(playlist_name, [t["path"] for t in tracks])

        save_db(db)

        # Keep rekordbox XML in sync with the library + Serato crates
        if new_tracks:
            try:
                rb = _export_all_rekordbox()
                print(f"[rekordbox] synced {rb['tracks']} tracks / {rb['playlists']} playlists -> {rb['path']}", flush=True)
            except Exception as e:
                print(f"[rekordbox] auto-export failed: {e}", flush=True)

        with _download_lock:
            _downloads[download_id]["status"] = "done"
            _downloads[download_id]["new_tracks"] = len(new_tracks)
            if result.returncode != 0:
                _downloads[download_id]["error"] = result.stderr[:500]
    except Exception as e:
        with _download_lock:
            _downloads[download_id]["status"] = "error"
            _downloads[download_id]["error"] = str(e)


def _fetch_playlist_name(url):
    """Get playlist/album name from any URL by scraping page title or yt-dlp."""
    # Spotify — scrape page title (works for ALL playlists including editorial)
    _ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    if "spotify.com" in url or "soundcloud.com" in url or "on.soundcloud.com" in url:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _ua, "Accept-Language": "en-US"})
            html = urllib.request.urlopen(req, timeout=10).read().decode()
            start = html.find("<title>") + 7
            end = html.find("</title>")
            if start > 6 and end > start:
                title = html[start:end]
                # Strip common suffixes
                # SoundCloud: "Stream X | Listen to Y playlist online..." → Y
                if "Listen to " in title and " playlist" in title:
                    title = title.split("Listen to ")[1].split(" playlist")[0]
                else:
                    for sep in [" | Spotify Playlist", " | Spotify", " - playlist by ",
                                " - Album by ", " - song and lyrics", " - Single by ",
                                " | Free Listening", " | SoundCloud", " on SoundCloud",
                                "Stream ", " | Listen to "]:
                        if sep in title:
                            title = title.split(sep)[-1] if sep == "Stream " else title.split(sep)[0]
                title = title.strip()
                if title:
                    return title
        except Exception as e:
            print(f"[resolve-name] scrape error for {url}: {e}", flush=True)

    # YouTube / everything else — yt-dlp
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--print", "playlist_title", "-I", "1", url],
            capture_output=True, text=True, timeout=15
        )
        name = result.stdout.strip().split("\n")[0].strip()
        if name and name != "NA":
            return name
    except Exception:
        pass

    return ""


@app.route("/api/resolve-name", methods=["POST"])
def resolve_name():
    """Resolve playlist/album name from URL."""
    data = request.json
    url = data.get("url", "").strip()
    name = _fetch_playlist_name(url)
    return jsonify({"name": name})


# ── Sanitation endpoints ──
_sanitize_jobs = {}
_sanitize_lock = threading.Lock()


@app.route("/api/sanitize/report")
def sanitize_report():
    """Library quality report: music videos, lyric videos, audio-only, short previews, low bitrate."""
    db = load_db()
    buckets = {
        'music_video': [],
        'live': [],
        'lyric_video': [],
        'audio_only': [],
        'unknown': [],
        'short_preview': [],  # < 35s = SoundCloud Go+ preview signature
        'low_bitrate': [],    # < 128 kbps
    }
    for tid, t in db['tracks'].items():
        duration = t.get('duration', 0)
        if 0 < duration < 35:
            buckets['short_preview'].append({'id': tid, 'artist': t['artist'], 'title': t['title'], 'duration': duration, 'path': t.get('path', '')})
            continue
        # Bitrate check
        file_size = t.get('file_size', 0)
        if file_size and duration > 0:
            bitrate_kbps = (file_size * 8) / (duration * 1000)
            if bitrate_kbps < 128:
                buckets['low_bitrate'].append({'id': tid, 'artist': t['artist'], 'title': t['title'], 'duration': duration, 'bitrate_kbps': round(bitrate_kbps, 1)})
        # Source classification
        cls = sanitize_classify(t.get('title', ''))
        buckets[cls].append({'id': tid, 'artist': t['artist'], 'title': t['title'], 'duration': duration})

    counts = {k: len(v) for k, v in buckets.items()}
    return jsonify({
        'total_tracks': len(db['tracks']),
        'counts': counts,
        'buckets': buckets,
        'quality_score': round(100 * counts['audio_only'] / max(1, len(db['tracks'])), 1),
    })


@app.route("/api/sanitize/classify/<track_id>")
def sanitize_classify_one(track_id):
    """Classify a single track's source."""
    db = load_db()
    t = db['tracks'].get(track_id)
    if not t:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'id': track_id,
        'artist': t['artist'],
        'title': t['title'],
        'classification': sanitize_classify(t.get('title', '')),
        'clean_title': sanitize_clean_title(t.get('title', '')),
        'duration': t.get('duration', 0),
    })


def _sanitize_download_replacement(artist, title, output_dir, prefer_audio=True):
    """Download audio replacement for a track. Tries audio → lyrics → bare search.
    Returns (new_filepath, None) on success, (None, error_msg) on failure."""
    clean_title = sanitize_clean_title(title)
    attempts = []
    if prefer_audio:
        attempts.append(f"{artist} {clean_title} official audio")
        attempts.append(f"{artist} {clean_title} audio")
    attempts.append(f"{artist} {clean_title} lyrics")
    attempts.append(f"{artist} {clean_title}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w\s\-\.]', '', f"{artist} - {clean_title}").strip()[:120]
    output_template = str(output_dir / f"{safe}.%(ext)s")

    for attempt in attempts:
        result = subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
             "-o", output_template, "--no-playlist", "--embed-metadata",
             f"ytsearch1:{attempt}"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            continue
        # Find downloaded file
        import glob as _glob
        candidates = _glob.glob(str(output_dir / f"{safe}.*"))
        if not candidates:
            continue
        newpath = candidates[0]
        # Validate duration (must be > 60s, reject previews + broken files)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", newpath],
                capture_output=True, text=True, timeout=10
            )
            dur = float(probe.stdout.strip())
            if dur < 60:
                os.remove(newpath)
                continue
            return newpath, None
        except (ValueError, FileNotFoundError):
            try: os.remove(newpath)
            except: pass
            continue
    return None, f"All YouTube search attempts failed for {artist} - {title}"


@app.route("/api/sanitize/replace/<track_id>", methods=["POST"])
def sanitize_replace(track_id):
    """Replace a single track with an audio-only version from YouTube.
    Deletes the old video/preview file, adds new track to DB."""
    import subprocess as _sp
    db = load_db()
    track = db['tracks'].get(track_id)
    if not track:
        return jsonify({'error': 'track not found'}), 404

    old_path = track.get('path', '')
    if not old_path or not os.path.exists(old_path):
        return jsonify({'error': 'old file missing'}), 404

    output_dir = Path(old_path).parent
    newpath, err = _sanitize_download_replacement(
        track['artist'], track['title'], output_dir, prefer_audio=True
    )
    if err:
        return jsonify({'error': err}), 500

    # Delete old file if new one is at a different path
    if newpath != old_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass

    # Re-index the new file
    new_track = scan_track(newpath)
    if not new_track:
        return jsonify({'error': 'could not scan replacement'}), 500

    # Replace old track in DB (preserving playlist membership if possible)
    if track_id in db['tracks']:
        del db['tracks'][track_id]
    db['tracks'][new_track['id']] = new_track
    # Update playlists that contained the old track ID
    for pid, plist in db.get('playlists', {}).items():
        ids = plist.get('track_ids', [])
        if track_id in ids:
            plist['track_ids'] = [new_track['id'] if i == track_id else i for i in ids]
    save_db(db)

    return jsonify({
        'success': True,
        'old': {'id': track_id, 'artist': track['artist'], 'title': track['title'], 'duration': track.get('duration', 0)},
        'new': {'id': new_track['id'], 'artist': new_track['artist'], 'title': new_track['title'], 'duration': new_track['duration']},
    })


def _sanitize_fix_all_worker(job_id, target_classes, limit):
    """Background worker that replaces all tracks matching target_classes."""
    try:
        db = load_db()
        targets = []
        for tid, t in db['tracks'].items():
            duration = t.get('duration', 0)
            if 'short_preview' in target_classes and 0 < duration < 35:
                targets.append(tid)
                continue
            cls = sanitize_classify(t.get('title', ''))
            if cls in target_classes:
                targets.append(tid)
        if limit and limit > 0:
            targets = targets[:limit]

        with _sanitize_lock:
            _sanitize_jobs[job_id]['total'] = len(targets)
            _sanitize_jobs[job_id]['status'] = 'running'

        for i, tid in enumerate(targets):
            with _sanitize_lock:
                _sanitize_jobs[job_id]['progress'] = i
            db = load_db()
            track = db['tracks'].get(tid)
            if not track:
                continue
            old_path = track.get('path', '')
            if not old_path or not os.path.exists(old_path):
                with _sanitize_lock:
                    _sanitize_jobs[job_id]['skipped'].append(f"{track['artist']} - {track['title']} (file missing)")
                continue
            output_dir = Path(old_path).parent
            newpath, err = _sanitize_download_replacement(
                track['artist'], track['title'], output_dir, prefer_audio=True
            )
            if err:
                with _sanitize_lock:
                    _sanitize_jobs[job_id]['failed'].append(f"{track['artist']} - {track['title']}: {err[:80]}")
                continue
            if newpath != old_path and os.path.exists(old_path):
                try: os.remove(old_path)
                except OSError: pass
            new_track = scan_track(newpath)
            if new_track:
                if tid in db['tracks']:
                    del db['tracks'][tid]
                db['tracks'][new_track['id']] = new_track
                for pid, plist in db.get('playlists', {}).items():
                    ids = plist.get('track_ids', [])
                    if tid in ids:
                        plist['track_ids'] = [new_track['id'] if i == tid else i for i in ids]
                save_db(db)
                with _sanitize_lock:
                    _sanitize_jobs[job_id]['fixed'].append(f"{new_track['artist']} - {new_track['title']} ({new_track['duration']:.0f}s)")
            time.sleep(0.5)

        with _sanitize_lock:
            _sanitize_jobs[job_id]['status'] = 'done'
            _sanitize_jobs[job_id]['progress'] = len(targets)
    except Exception as e:
        with _sanitize_lock:
            _sanitize_jobs[job_id]['status'] = 'error'
            _sanitize_jobs[job_id]['error'] = str(e)


@app.route("/api/sanitize/fix", methods=["POST"])
def sanitize_fix_all():
    """Start a background job replacing all targeted tracks."""
    data = request.get_json() or {}
    target_classes = data.get('classes', ['music_video', 'live', 'short_preview'])
    limit = data.get('limit', 0)
    job_id = hashlib.md5(f"sanitize{time.time()}".encode()).hexdigest()[:10]
    with _sanitize_lock:
        _sanitize_jobs[job_id] = {
            'id': job_id, 'status': 'queued', 'total': 0, 'progress': 0,
            'classes': target_classes, 'fixed': [], 'failed': [], 'skipped': []
        }
    thread = threading.Thread(
        target=_sanitize_fix_all_worker,
        args=(job_id, target_classes, limit),
        daemon=True,
    )
    thread.start()
    return jsonify({'job_id': job_id, 'status': 'queued', 'classes': target_classes})


@app.route("/api/sanitize/jobs/<job_id>")
def sanitize_job_status(job_id):
    with _sanitize_lock:
        job = _sanitize_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify(job)


@app.route("/api/sanitize/jobs")
def sanitize_jobs_list():
    with _sanitize_lock:
        return jsonify({'jobs': list(_sanitize_jobs.values())})


@app.route("/api/download", methods=["POST"])
def start_download():
    """Start a spotdl download. Accepts {url, name, meta_name}."""
    data = request.json
    url = data.get("url", "").strip()
    name = data.get("name", "").strip()
    meta_name = (data.get("meta_name") or "").strip() or None

    # Auto-fetch playlist name if not provided
    if not name:
        name = _fetch_playlist_name(url)
    if not name:
        # Last resort: use the uploader/channel from URL, not a timestamp
        try:
            r = subprocess.run(
                ["yt-dlp", "--print", "uploader", "-I", "1", url],
                capture_output=True, text=True, timeout=10
            )
            uploader = r.stdout.strip().split("\n")[0]
            if uploader and uploader != "NA":
                name = f"{uploader} tracks"
        except Exception:
            pass
    if not name:
        name = "Unsorted"

    if not url:
        return jsonify({"error": "URL required"}), 400

    download_id = hashlib.md5(f"{url}{time.time()}".encode()).hexdigest()[:10]
    with _download_lock:
        _downloads[download_id] = {
            "id": download_id,
            "status": "queued",
            "url": url,
            "name": name,
            "new_tracks": 0,
            "error": None,
        }

    thread = threading.Thread(target=_run_download, args=(download_id, url, name, meta_name))
    thread.daemon = True
    thread.start()

    return jsonify({"id": download_id, "status": "queued", "name": name})


@app.route("/api/download/<download_id>")
def download_status(download_id):
    with _download_lock:
        dl = _downloads.get(download_id)
    if not dl:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dl)


@app.route("/api/downloads")
def list_downloads():
    with _download_lock:
        return jsonify({"downloads": list(_downloads.values())})


def _startup_resync():
    """On startup, ensure every folder in yt-dlp has tracks in library, a playlist, and a Serato crate."""
    music_dir = Path.home() / "Music" / "yt-dlp"
    if not music_dir.exists():
        return
    db = load_db()
    synced = 0
    for folder in sorted(music_dir.iterdir()):
        if not folder.is_dir():
            continue
        files = [f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTS]
        if not files:
            continue
        # Scan any missing tracks into library
        new_ids = []
        for f in files:
            fid = file_id(str(f))
            if fid not in db["tracks"]:
                track = scan_track(str(f))
                if track:
                    db["tracks"][fid] = track
                    new_ids.append(fid)
            else:
                new_ids.append(fid)
        if not new_ids:
            # All tracks already known, just collect IDs
            new_ids = [file_id(str(f)) for f in files if file_id(str(f)) in db["tracks"]]
        # Ensure playlist exists with track IDs
        pid = hashlib.md5(folder.name.encode()).hexdigest()[:10]
        if "playlists" not in db:
            db["playlists"] = {}
        if pid not in db["playlists"] or len(db["playlists"][pid].get("track_ids", [])) == 0:
            db["playlists"][pid] = {"name": folder.name, "track_ids": new_ids}
            synced += 1
        # Ensure Serato crate exists
        crate_path = SERATO_DIR / f"{folder.name}.crate"
        # Only create a crate if NO crate of that name exists at ANY nesting level —
        # the user parents crates under genre folders (HIP HOP%%..., SETS%%...), and
        # writing a flat copy for every folder duplicated ~230 crates on 2026-08-26.
        _n = lambda x: re.sub(r'[^a-z0-9]', '', x.lower())
        nested_exists = any(_n(p.stem.split('%%')[-1]) == _n(folder.name)
                            for p in SERATO_DIR.glob("*%%*.crate"))
        if not crate_path.exists() and not nested_exists:
            _write_crate(folder.name, [str(f) for f in sorted(files)])
            synced += 1
    if synced:
        save_db(db)
        print(f"[startup] Resynced {synced} playlists/crates")
    else:
        print("[startup] All playlists/crates in sync")


if __name__ == "__main__":
    _startup_resync()
    print("Amapiano Music Library v2 at http://localhost:8766")
    app.run(host="0.0.0.0", port=8766, debug=False)
