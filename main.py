import os
import sys
import threading
import time
import webbrowser
import subprocess
import urllib.request
import json
import gc
import logging
import re
import requests
from bs4 import BeautifulSoup
from functools import lru_cache
from flask import Flask, request, render_template, jsonify, redirect
from waitress import serve

# --- Lazy-loaded YTMusic (saves ~50MB startup RAM) ---
_yt_instance = None
_yt_lock = threading.Lock()

def get_yt():
    global _yt_instance
    if _yt_instance is None:
        with _yt_lock:
            if _yt_instance is None:
                from ytmusicapi import YTMusic
                _yt_instance = YTMusic()
    return _yt_instance

# ==========================================
#      Lyrics & Cache Helpers (Consolidated)
# ==========================================

CACHE_DIR = "data/lyrics_cache"

def _safe_filename(artist, title):
    """Generate a sanitized filename for caching."""
    name = f"{artist}_{title}" if artist else title
    # Remove invalid characters
    name = re.sub(r'[\\/\:\*\?\"\<\>\|]', "", name)
    name = name.strip().replace(" ", "_")
    return name + ".json"

def clean_song_query(title, artist=""):
    """
    Proper query cleaning: strip regex patterns like round brackets,
    square brackets, lyrical, video song, full song, official, from movie, etc.
    case-insensitively before searching.
    """
    title = title or ""
    artist = artist or ""

    # Strip bracketed expressions: (), [], {}
    t = re.sub(r'[\(\[\{][^\)\]\}]*[\)\]\}]', ' ', title, flags=re.IGNORECASE)

    # Strip pipe / slash suffixes if present (e.g., 'Title | Movie | Cast')
    if '|' in t:
        parts = t.split('|')
        if parts[0].strip():
            t = parts[0]

    # Strip noisy keywords case-insensitively
    noise_patterns = [
        r'\b(official\s*(music|lyric|audio|video)?\s*(video|audio|track)?)\b',
        r'\b(lyrical(\s+video)?|lyrics|lyric\s+video)\b',
        r'\b(video\s+song|full\s+song|audio\s+song|full\s+video(\s+song)?)\b',
        r'\b(from\s+[\"\'\w\s]+)\b',
        r'\b(hd|4k|8k|remix|slowed(\s+and\s+reverb)?)\b',
    ]
    for pat in noise_patterns:
        t = re.sub(pat, ' ', t, flags=re.IGNORECASE)

    # Strip leading/trailing dashes, colons, hyphens and normalize whitespace
    t = re.sub(r'[\s\-_:]+$', '', t)
    t = re.sub(r'^[\s\-_:]+', '', t)
    t = re.sub(r'\s+', ' ', t).strip()

    if not t:
        t = title.strip()

    # Clean artist: strip "- Topic", brackets, etc.
    a = re.sub(r'\s*-\s*Topic\b', '', artist, flags=re.IGNORECASE)
    a = re.sub(r'[\(\[\{][^\)\]\}]*[\)\]\}]', ' ', a)
    a = re.sub(r'\s+', ' ', a).strip()

    # Extract primary artist (first artist before comma, &, /, feat, ft)
    primary_parts = re.split(r'[,&/;]|(\bfeat\.?\b)|(\bft\.?\b)', a, flags=re.IGNORECASE)
    primary_a = primary_parts[0].strip() if primary_parts and primary_parts[0] else a

    return t, a, primary_a

def normalize_lyrics_response(data):
    """
    Ensure response format contains BOTH modern format and backwards-compatible format:
      type: 'synced', 'plain', or 'none'
      lyrics: list of lines for synced, string for plain, null for none
      synced: boolean
      lines: list of {text, time}
      plain: string of lyrics text
    """
    if not data or not isinstance(data, dict):
        return {
            'type': 'none',
            'lyrics': None,
            'synced': False,
            'lines': [],
            'plain': ''
        }

    raw_type = data.get('type')
    lyrics = data.get('lyrics')
    synced = data.get('synced')
    lines = data.get('lines')
    plain = data.get('plain')

    # Case 1: Synced lyrics
    if raw_type == 'synced' or synced is True or (isinstance(lyrics, list) and len(lyrics) > 0) or (isinstance(lines, list) and len(lines) > 0):
        raw_lines = lines if (isinstance(lines, list) and len(lines) > 0) else lyrics
        if not isinstance(raw_lines, list):
            raw_lines = []
        parsed_lines = []
        for item in raw_lines:
            if isinstance(item, dict):
                parsed_lines.append({
                    'text': str(item.get('text', '')),
                    'time': float(item.get('time', 0.0))
                })
        plain_text = plain if (isinstance(plain, str) and plain.strip()) else '\n'.join(l['text'] for l in parsed_lines)
        return {
            'type': 'synced',
            'lyrics': parsed_lines,
            'synced': True,
            'lines': parsed_lines,
            'plain': plain_text
        }

    # Case 2: Plain lyrics
    plain_text = plain or (lyrics if isinstance(lyrics, str) else '') or ''
    if raw_type == 'plain' or (plain_text and plain_text.strip()):
        return {
            'type': 'plain',
            'lyrics': plain_text.strip(),
            'synced': False,
            'lines': [],
            'plain': plain_text.strip()
        }

    # Case 3: None
    return {
        'type': 'none',
        'lyrics': None,
        'synced': False,
        'lines': [],
        'plain': ''
    }

def get_cached_lyrics(artist, title, video_id=None):
    """Retrieve lyrics from local cache if available."""
    if not os.path.exists(CACHE_DIR):
        return None

    # Check by video_id
    if video_id:
        v_file = os.path.join(CACHE_DIR, f"vid_{video_id}.json")
        if os.path.exists(v_file):
            try:
                with open(v_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    norm = normalize_lyrics_response(data)
                    if norm.get("type") != "none" and norm.get("lyrics"):
                        return norm
            except Exception:
                pass

    # Check by artist and title variations
    candidates = []
    if title:
        candidates.append(_safe_filename(artist or "", title))
        if artist:
            candidates.append(_safe_filename("", title))

    for filename in candidates:
        file_path = os.path.join(CACHE_DIR, filename)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    norm = normalize_lyrics_response(data)
                    if norm.get("type") != "none" and norm.get("lyrics"):
                        return norm
            except Exception:
                pass

    return None

def save_cached_lyrics(artist, title, data, video_id=None):
    """Save lyrics to local cache."""
    if not data or data.get("type") == "none" or not data.get("lyrics"):
        return

    try:
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR, exist_ok=True)

        norm = normalize_lyrics_response(data)

        if title:
            file_path = os.path.join(CACHE_DIR, _safe_filename(artist or "", title))
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(norm, f, ensure_ascii=False, indent=2)

        if video_id:
            v_file = os.path.join(CACHE_DIR, f"vid_{video_id}.json")
            with open(v_file, "w", encoding="utf-8") as f:
                json.dump(norm, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving cache for {artist} - {title}: {e}")

def parse_lrc(lrc_text):
    """
    Parses LRC formatted text into a structured JSON list of lines with timestamps.
    Example: "[00:12.34] Lyrics text" -> {"time": 12.34, "text": "Lyrics text"}
    """
    lines = lrc_text.split('\n')
    lyrics = []

    # Matches [mm:ss.xx] or [mm:ss:xx] or [mm:ss]
    pattern = re.compile(r'\[(\d+):(\d+\.?\d*)\](.*)')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        match = pattern.match(line)
        if match:
            minutes = int(match.group(1))
            seconds = float(match.group(2))
            text = match.group(3).strip()

            time_in_seconds = (minutes * 60) + seconds

            lyrics.append({
                "time": round(time_in_seconds, 2),
                "text": text
            })

    # Sort by time just in case the LRC is out of order
    lyrics.sort(key=lambda x: x["time"])
    return lyrics

def fetch_lrclib(clean_artist, clean_title, primary_artist):
    """
    Tier 2: Fetch lyrics from LRCLIB API.
    First /api/get with clean artist & clean title.
    Then /api/search?q=... with clean title & primary artist.
    Returns (synced_dict or None, plain_fallback_str or None).
    """
    headers = {"User-Agent": "SoundWave/1.0 (https://github.com/Maddy0057)"}
    plain_fallback = None

    # Step 1: /api/get
    artists_to_try = [a for a in [clean_artist, primary_artist] if a]
    for art in artists_to_try:
        try:
            url = "https://lrclib.net/api/get"
            params = {"artist_name": art, "track_name": clean_title}
            response = requests.get(url, params=params, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data and data.get("syncedLyrics"):
                    return {"type": "synced", "lyrics": parse_lrc(data["syncedLyrics"])}, None
                elif data and data.get("plainLyrics") and not plain_fallback:
                    plain_fallback = data["plainLyrics"]
        except Exception as e:
            print(f"LRCLIB /api/get error ({art} - {clean_title}): {e}")

    # Step 2: /api/search
    search_queries = []
    if clean_title and primary_artist:
        search_queries.append(f"{clean_title} {primary_artist}".strip())
    if clean_title and clean_title not in search_queries:
        search_queries.append(clean_title)

    for q in search_queries:
        try:
            url = "https://lrclib.net/api/search"
            params = {"q": q}
            response = requests.get(url, params=params, headers=headers, timeout=5)
            if response.status_code == 200:
                items = response.json()
                if isinstance(items, list):
                    # Check for synced lyrics first
                    for item in items:
                        if item.get("syncedLyrics"):
                            return {"type": "synced", "lyrics": parse_lrc(item["syncedLyrics"])}, None
                    # Fallback to plain lyrics
                    for item in items:
                        if item.get("plainLyrics") and not plain_fallback:
                            plain_fallback = item["plainLyrics"]
                            break
        except Exception as e:
            print(f"LRCLIB /api/search error (q={q}): {e}")

    return None, plain_fallback

def fetch_ytmusic_lyrics(video_id=None, clean_title="", primary_artist=""):
    """
    Tier 3: ytmusicapi official lyrics.
    If video_id is provided, call yt.get_watch_playlist(videoId=videoId).
    If lyrics browseId exists, call yt.get_lyrics(browseId).
    If no video_id or if it fails, search yt.search(clean_query, filter='songs')
    and check the top match's get_watch_playlist lyrics.
    """
    try:
        yt = get_yt()
    except Exception as e:
        print(f"Failed to get YTMusic instance: {e}")
        return None

    # Try direct videoId if provided
    if video_id:
        try:
            watch = yt.get_watch_playlist(videoId=video_id)
            if watch and watch.get("lyrics"):
                browse_id = watch["lyrics"]
                data = yt.get_lyrics(browseId=browse_id)
                if data and data.get("lyrics") and data["lyrics"].strip():
                    return {"type": "plain", "lyrics": data["lyrics"].strip()}
        except Exception as e:
            print(f"ytmusicapi get_watch_playlist error (videoId={video_id}): {e}")

    # Search songs fallback
    query = f"{clean_title} {primary_artist}".strip() or clean_title
    if query:
        try:
            songs = yt.search(query, filter="songs")
            if songs and isinstance(songs, list):
                for s in songs[:3]:
                    vid = s.get("videoId")
                    if vid and vid != video_id:
                        try:
                            watch = yt.get_watch_playlist(videoId=vid)
                            if watch and watch.get("lyrics"):
                                browse_id = watch["lyrics"]
                                data = yt.get_lyrics(browseId=browse_id)
                                if data and data.get("lyrics") and data["lyrics"].strip():
                                    return {"type": "plain", "lyrics": data["lyrics"].strip()}
                        except Exception:
                            continue
        except Exception as e:
            print(f"ytmusicapi search error for '{query}': {e}")

    return None

def scrape_genius(artist, title):
    """
    Tier 4: Search Genius for the artist and title, and scrape the plain lyrics text.
    Fallback when LRCLIB and ytmusicapi have no lyrics.
    """
    try:
        clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
        query = f"{artist} {clean_title}".strip()
        if not query:
            return None

        search_url = "https://genius.com/api/search/multi"
        params = {"q": query}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        response = requests.get(search_url, params=params, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()

        song_url = None
        for section in data.get("response", {}).get("sections", []):
            if section.get("type") == "song":
                hits = section.get("hits", [])
                if hits:
                    song_url = hits[0].get("result", {}).get("url")
                    break

        if not song_url:
            return None

        page = requests.get(song_url, headers=headers, timeout=5)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")

        lyrics_divs = soup.find_all("div", attrs={"data-lyrics-container": "true"})
        if not lyrics_divs:
            return None

        lyrics_text_parts = []
        for div in lyrics_divs:
            for br in div.find_all("br"):
                br.replace_with("\n")
            text = div.get_text()
            lyrics_text_parts.append(text)

        final_lyrics = "\n".join(lyrics_text_parts).strip()
        final_lyrics = re.sub(r'\n{3,}', '\n\n', final_lyrics)
        return final_lyrics

    except Exception as e:
        print(f"Genius Scraper Error ({artist} - {title}): {e}")
        return None

def get_lyrics(artist, title, videoId=None):
    """
    Main entry point for fetching lyrics.
    Tier 1: Local cache check (get_cached_lyrics)
    Tier 2: LRCLIB (synced lyrics prioritized, plain as fallback)
    Tier 3: ytmusicapi official lyrics (direct videoId or song search)
    Tier 4: Genius scraping fallback
    """
    # 1. Local Cache check
    cached = get_cached_lyrics(artist, title, video_id=videoId)
    if cached and cached.get("type") != "none" and cached.get("lyrics"):
        return cached

    clean_t, clean_a, primary_a = clean_song_query(title, artist)

    if (clean_t != title or clean_a != artist) and clean_t:
        cached = get_cached_lyrics(clean_a, clean_t, video_id=videoId)
        if cached and cached.get("type") != "none" and cached.get("lyrics"):
            return cached

    # 2. LRCLIB (Synced lyrics check)
    synced_result, lrclib_plain = fetch_lrclib(clean_a, clean_t, primary_a)
    if synced_result:
        normalized = normalize_lyrics_response(synced_result)
        save_cached_lyrics(artist, title, normalized, video_id=videoId)
        return normalized

    # 3. ytmusicapi official lyrics
    yt_result = fetch_ytmusic_lyrics(video_id=videoId, clean_title=clean_t, primary_artist=primary_a)
    if yt_result:
        normalized = normalize_lyrics_response(yt_result)
        save_cached_lyrics(artist, title, normalized, video_id=videoId)
        return normalized

    # Fallback to LRCLIB plain if available
    if lrclib_plain and lrclib_plain.strip():
        normalized = normalize_lyrics_response({"type": "plain", "lyrics": lrclib_plain})
        save_cached_lyrics(artist, title, normalized, video_id=videoId)
        return normalized

    # 4. Genius Fallback
    genius_lyrics = scrape_genius(clean_a or artist, clean_t or title)
    if genius_lyrics and genius_lyrics.strip():
        normalized = normalize_lyrics_response({"type": "plain", "lyrics": genius_lyrics})
        save_cached_lyrics(artist, title, normalized, video_id=videoId)
        return normalized

    # No lyrics found anywhere
    return normalize_lyrics_response({"type": "none", "lyrics": None})

# ==========================================

# --- Suppress verbose logging for lighter operation ---
logging.getLogger('waitress').setLevel(logging.ERROR)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# --- Configuration ---
YTDLP_FILENAME = "yt-dlp.exe"
YTDLP_DOWNLOAD_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
INACTIVITY_TIMEOUT = 600  # 10 minutes
PLAYLISTS_FILE = "playlists.json"

# --- Global Timer ---
last_activity_time = time.time()
playlist_lock = threading.Lock()

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def get_app_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

template_folder = resource_path('templates')
app = Flask(__name__, template_folder=template_folder)
app.logger.setLevel(logging.ERROR)

@app.before_request
def update_activity_time():
    global last_activity_time
    last_activity_time = time.time()

def get_playlists_file_path():
    try:
        base_path = get_app_path()
        return os.path.join(base_path, PLAYLISTS_FILE)
    except:
        return PLAYLISTS_FILE

def load_playlists():
    file_path = get_playlists_file_path()
    try:
        if os.path.exists(file_path):
            with playlist_lock:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
    except Exception as e:
        print(f"Error loading playlists: {e}")
    return {}

def save_playlists(playlists):
    if not isinstance(playlists, dict):
        return False
    file_path = get_playlists_file_path()
    try:
        with playlist_lock:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(playlists, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving playlists: {e}")
        return False

# --- Helper: Get Full HD Thumbnail ---
def get_hd_thumbnail(video_id, thumbnails_list):
    # Priority 1: Force MaxResDefault (Full HD)
    # This is usually 1280x720 or 1920x1080
    return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

# ===== Routes =====

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/heartbeat", methods=["POST", "GET"])
def api_heartbeat():
    # Will automatically update last_activity_time via @app.before_request
    return jsonify({"status": "alive"})

@app.route("/api/lyrics")
def api_lyrics():
    artist = request.args.get("artist", "").strip()
    title = request.args.get("title", "").strip()
    video_id = request.args.get("videoId", "").strip() or request.args.get("video_id", "").strip() or None

    if not title and not video_id:
        return jsonify(normalize_lyrics_response(None)), 200

    result = get_lyrics(artist, title, videoId=video_id)
    return jsonify(result), 200

@lru_cache(maxsize=30)
def cached_search(query):
    return get_yt().search(query, filter="songs")[:30]

@app.route("/api/search")
def api_search():
    query = request.args.get("query", "")
    shuffle_param = request.args.get("shuffle", "false").lower() == "true"
    results = []
    if query:
        try:
            songs = list(cached_search(query))
            if shuffle_param:
                import random
                random.shuffle(songs)
                
            for s in songs:
                if s.get("videoId") and s.get("title"):
                    # Use Full HD Thumbnail logic
                    thumbnail_url = get_hd_thumbnail(s["videoId"], s.get("thumbnails"))
                    
                    results.append({
                        "title": s["title"],
                        "artist": s["artists"][0]["name"] if s.get("artists") else "Unknown",
                        "videoId": s["videoId"],
                        "thumbnail": thumbnail_url
                    })
                if len(results) >= 15:
                    break
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(results)

# --- RECOMMENDATIONS (UPDATED) ---
@app.route("/api/recommendations")
def api_recommendations():
    video_id = request.args.get("videoId")
    if not video_id:
        return jsonify([])
    
    try:
        # Fetch 20 items for the autoplay queue
        watch_playlist = get_yt().get_watch_playlist(videoId=video_id, limit=20)
        tracks = watch_playlist.get('tracks', [])
        results = []
        
        for s in tracks:
            if s.get("videoId") and s.get("videoId") != video_id:
                # Use Full HD Thumbnail logic
                thumbnail_url = get_hd_thumbnail(s["videoId"], s.get("thumbnails"))
                
                results.append({
                    "title": s["title"],
                    "artist": s["artists"][0]["name"] if s.get("artists") else "Unknown",
                    "videoId": s["videoId"],
                    "thumbnail": thumbnail_url
                })
        return jsonify(results)
    except Exception as e:
        print(f"Recommendation Error: {e}")
        return jsonify([])

@app.route("/api/trending")
def trending():
    category = request.args.get("category", "").strip()
    try:
        yt = get_yt()
        tracks = []
        if category == "Trending India" or category == "":
            charts = yt.get_charts('IN')
            videos = charts.get('videos') or []
            browse_id = videos[0].get('playlistId') if videos else None
            if browse_id:
                pl = yt.get_playlist(browse_id, limit=20)
                for t in pl.get('tracks', []) or []:
                    thumbs = t.get('thumbnails') or []
                    thumb = thumbs[-1].get('url') if thumbs else ''
                    artists = t.get('artists') or []
                    artist = ', '.join([a.get('name', '') for a in artists if a.get('name')]) or 'Unknown'
                    if t.get('videoId'):
                        tracks.append({'title': t.get('title'), 'artist': artist, 'videoId': t.get('videoId'), 'thumbnail': thumb})
            return jsonify(tracks)
        query = {
            'Top 10 Telugu': 'Telugu top songs',
            'Top 10 Hindi': 'Hindi top songs',
            'Top 10 Tamil': 'Tamil top songs',
            'Top 10 English': 'English top songs',
        }.get(category, category + ' top songs')
        results = yt.search(query, filter='songs')[:10]
        for s in results:
            if s.get('videoId'):
                artists = s.get('artists') or []
                artist = ', '.join([a.get('name', '') for a in artists if a.get('name')]) or 'Unknown'
                thumbs = s.get('thumbnails') or []
                thumb = thumbs[-1].get('url', '') if thumbs else ''
                tracks.append({'title': s.get('title'), 'artist': artist, 'videoId': s.get('videoId'), 'thumbnail': thumb})
        return jsonify(tracks)
    except Exception as e:
        return jsonify([]), 200

@app.route("/stream/<videoId>")
def stream(videoId):
    url = f"https://www.youtube.com/watch?v={videoId}"
    
    ytdlp_path = os.path.join(get_app_path(), YTDLP_FILENAME)
    if not os.path.exists(ytdlp_path):
        if os.path.exists(YTDLP_FILENAME): ytdlp_path = YTDLP_FILENAME
        else: return "Error: yt-dlp.exe is missing.", 500

    try:
        cmd = [ytdlp_path, "-f", "bestaudio[ext=m4a]/bestaudio", "-g", "--no-playlist", "--no-warnings", "--quiet", url]
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        audio_url = subprocess.check_output(cmd, stderr=subprocess.PIPE, text=True, startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).strip()
        
        return redirect(audio_url)
    except Exception as e:
        return "Error starting stream", 500

# ===== Playlist Routes =====

@app.route("/api/playlists", methods=["GET"])
def get_playlists():
    return jsonify(load_playlists())

@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    playlists = load_playlists()
    if name in playlists:
        return jsonify({"error": "Playlist already exists"}), 400
    playlists[name] = {"songs": [], "modified": time.time()}
    save_playlists(playlists)
    return jsonify({"success": True, "playlists": playlists})

@app.route("/api/playlists/<name>", methods=["DELETE"])
def delete_playlist(name):
    name = name.strip()
    playlists = load_playlists()
    if name not in playlists:
        return jsonify({"error": "Playlist not found"}), 404
    del playlists[name]
    save_playlists(playlists)
    return jsonify({"success": True, "playlists": playlists})

@app.route("/api/playlists/<name>/songs", methods=["POST"])
def add_song(name):
    name = name.strip()
    playlists = load_playlists()
    if name not in playlists:
        return jsonify({"error": "Playlist not found"}), 404
    data = request.get_json(silent=True) or {}
    song = data.get("song")
    if not song or not isinstance(song, dict) or not song.get("videoId"):
        return jsonify({"error": "Valid song required"}), 400
    if not any(s.get('videoId') == song['videoId'] for s in playlists[name]['songs']):
        playlists[name]['songs'].append(song)
        playlists[name]['modified'] = time.time()
        save_playlists(playlists)
    return jsonify({"success": True, "playlist": playlists[name]})

@app.route("/api/playlists/<name>/songs/<videoId>", methods=["DELETE"])
def remove_song(name, videoId):
    name = name.strip()
    playlists = load_playlists()
    if name not in playlists:
        return jsonify({"error": "Playlist not found"}), 404
    playlists[name]['songs'] = [s for s in playlists[name]['songs'] if s.get('videoId') != videoId]
    playlists[name]['modified'] = time.time()
    save_playlists(playlists)
    return jsonify({"success": True, "playlist": playlists[name]})

@app.route("/api/playlists/<old>", methods=["PUT"])
def rename_playlist(old):
    old = old.strip()
    data = request.get_json(silent=True) or {}
    new = (data.get("name") or "").strip()
    playlists = load_playlists()
    if old not in playlists:
        return jsonify({"error": "Playlist not found"}), 404
    if not new:
        return jsonify({"error": "New name required"}), 400
    if new in playlists and new != old:
        return jsonify({"error": "Playlist already exists"}), 400
    playlists[new] = playlists.pop(old)
    playlists[new]['modified'] = time.time()
    save_playlists(playlists)
    return jsonify({"success": True, "playlists": playlists})

# ===== Startup =====

def ensure_yt_dlp():
    target_path = os.path.join(get_app_path(), YTDLP_FILENAME)
    if not os.path.exists(target_path):
        try: urllib.request.urlretrieve(YTDLP_DOWNLOAD_URL, target_path)
        except: pass
    
    marker = "last_update_check.txt"
    if not os.path.exists(marker) or (time.time() - os.path.getmtime(marker)) > 86400:
        def update_task():
            try:
                subprocess.run([target_path, "-U"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                with open(marker, 'w') as f: f.write(str(time.time()))
            except: pass
        threading.Thread(target=update_task, daemon=True).start()

def start_server():
    serve(app, host='0.0.0.0', port=5000, threads=4)

def check_inactivity():
    while True:
        time.sleep(120)  # Check every 2 minutes instead of 1 (less CPU wake-ups)
        if time.time() - last_activity_time > INACTIVITY_TIMEOUT:
            gc.collect()
            os._exit(0)

if __name__ == "__main__":
    # Minimize garbage before startup
    gc.collect()
    
    ensure_yt_dlp()
    threading.Thread(target=start_server, daemon=True).start()
    # threading.Thread(target=check_inactivity, daemon=True).start()
    
    time.sleep(1)
    webbrowser.open('http://127.0.0.1:5000')
    
    try: 
        while True: 
            time.sleep(2)  # Sleep longer to reduce CPU wake-ups
    except KeyboardInterrupt:
        pass