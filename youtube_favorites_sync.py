#!/usr/bin/env python3
"""
YouTube playlist to MP3 sync

Downloads new videos from a YouTube playlist and converts them to MP3.
Tracks previously downloaded videos to avoid duplicates.

On startup the script installs missing Python packages, asks which playlist
to sync, and asks which folder to save the MP3s in.

Usage:
    python youtube_favorites_sync.py
    python youtube_favorites_sync.py --limit 5
    python youtube_favorites_sync.py --dry-run
    python youtube_favorites_sync.py --playlist LL --output ~/Music/YouTube
"""

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

# Configuration
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "user_config.json"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "Youtube" / "mp3"
# Older runs stored history next to the mp3 folder, not inside it.
LEGACY_STATE_FILE = Path.home() / "Downloads" / "Youtube" / ".sync_state.json"
PLAYLIST_FEED_URL = "https://www.youtube.com/feed/playlists"
_EXACT_LIST_IDS = {"LL", "WL"}
_LIST_PREFIXES = ("PL", "FL", "UU", "OL", "RD", "VL")

# Cookies configuration for authenticating to YouTube (required for personal playlists like Favorites)
# YouTube still requires login/cookies in 2026 for accessing user-specific content and to avoid bot detection.
#
# Best options on Windows:
#   - Use COOKIES_FILE pointing to an exported cookies.txt (most reliable long-term)
#   - Or COOKIES_BROWSER="firefox" (often works out of the box if logged in to YT in Firefox)
#   - Chrome/Edge --cookies-from-browser frequently broken on Win due to App-Bound Encryption (DPAPI issues)
# Auto-detect a local cookies.txt next to the script (recommended for portability)
_local_cookies = SCRIPT_DIR / "cookies.txt"
COOKIES_FILE = _local_cookies if _local_cookies.exists() else None
COOKIES_BROWSER = "firefox" if not COOKIES_FILE else None  # "firefox", "chrome", "edge" etc.
_cookie_note = None

# Only mark as permanently skipped when the video itself is gone.
# Do not include extractor/auth flakes: "sign in", "not available", "blocked", 403, etc.
PERMANENT_FAILURE_PHRASES = (
    "private video",
    "this video has been removed by the uploader",
    "removed by the uploader",
    "account has been terminated",
    "uploader has closed their youtube account",
    "contractual obligation",
    "music licensor",
    "this video is no longer available because",
)

_log_file = None
_cookie_rotation_warned = False


def log(msg):
    """Print with immediate flush, and append to the run log if one is open."""
    print(msg, flush=True)
    if _log_file is not None:
        _log_file.write(msg + "\n")
        _log_file.flush()


def ask(prompt):
    """Prompt on one line and copy the prompt plus the answer into the run log."""
    print(prompt, end="", flush=True)
    if _log_file is not None:
        _log_file.write(prompt)
        _log_file.flush()
    try:
        answer = input()
    except EOFError:
        print()
        raise
    if _log_file is not None:
        _log_file.write(answer + "\n")
        _log_file.flush()
    return answer


def start_run_log():
    """Create a dated log next to the script (sync_YYYYMMDD_HHMMSS.log)."""
    global _log_file
    path = SCRIPT_DIR / f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    _log_file = open(path, "w", encoding="utf-8")
    return path


def close_run_log():
    global _log_file
    if _log_file is not None:
        _log_file.close()
        _log_file = None


def load_user_config():
    """Last interactive playlist and save folder. Missing or broken files are ignored."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_user_config(playlist, output_dir):
    data = {
        "playlist_url": playlist.get("url"),
        "playlist_title": playlist.get("title"),
        "output_dir": str(output_dir),
    }
    if playlist.get("channel_url"):
        data["channel_url"] = playlist["channel_url"]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _strip_text(text):
    return (text or "").strip().strip('"').strip("'")


def looks_like_playlist_id(text):
    """True for LL, WL, and normal YouTube playlist ids. Ordinary words are not ids."""
    if text in _EXACT_LIST_IDS:
        return True
    return any(text.startswith(prefix) and len(text) >= 13 for prefix in _LIST_PREFIXES)


def _is_handle(text):
    if len(text) < 3 or any(ch.isspace() for ch in text):
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
    return all(ch in allowed for ch in text)


def channel_playlists_url(text):
    """Return a /playlists URL for a channel or @handle, or None."""
    text = _strip_text(text)
    if not text or looks_like_playlist_id(text):
        return None
    if text.startswith("@"):
        handle = text.split("/")[0]
        return f"https://www.youtube.com/{handle}/playlists" if _is_handle(handle[1:]) else None
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urllib.parse.urlparse(text)
        if "list=" in parsed.query:
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        if parts[0] in {"channel", "c", "user"} and len(parts) >= 2:
            root = f"{parsed.scheme}://{parsed.netloc}/{parts[0]}/{parts[1]}"
            return root + "/playlists"
        if parts[0].startswith("@"):
            root = f"{parsed.scheme}://{parsed.netloc}/{parts[0]}"
            return root + "/playlists"
        return None
    if _is_handle(text):
        return f"https://www.youtube.com/@{text}/playlists"
    return None


def playlist_url_from_user_text(text):
    """Return a playlist URL, or None when the text is not a URL or list id."""
    text = _strip_text(text)
    if not text or channel_playlists_url(text):
        return None
    if text.startswith("http://") or text.startswith("https://"):
        if "list=" in text or "/playlist" in text:
            return text
        return None
    if looks_like_playlist_id(text):
        return f"https://www.youtube.com/playlist?list={text}"
    return None


def playlist_id_from_url(url):
    if not url:
        return None
    candidate = url.strip()
    if looks_like_playlist_id(candidate) and "://" not in candidate:
        return candidate
    parsed = urllib.parse.urlparse(candidate)
    ids = urllib.parse.parse_qs(parsed.query).get("list")
    if ids and ids[0]:
        return ids[0]
    return None


def parse_menu_choice(text, playlist_count, default_index):
    """Return ('item', n), ('paste', None), ('url', url), or ('invalid', None).

    playlist_count is the number of playlists. The paste row is the next number.
    default_index is 1-based, or None when Enter should not select anything.
    """
    raw = (text or "").strip()
    paste_index = playlist_count + 1
    if raw == "":
        if default_index is not None:
            return ("item", default_index)
        return ("invalid", None)
    if raw.isdigit():
        number = int(raw)
        if number == paste_index:
            return ("paste", None)
        if 1 <= number <= playlist_count:
            return ("item", number)
        return ("invalid", None)
    url = playlist_url_from_user_text(raw)
    if url:
        return ("url", url)
    return ("invalid", None)


def default_menu_index(playlists, saved_url):
    if not isinstance(saved_url, str) or not saved_url.strip():
        return None
    saved_id = playlist_id_from_url(saved_url.strip())
    for index, item in enumerate(playlists, 1):
        if item.get("url") == saved_url or (saved_id and item.get("id") == saved_id):
            return index
    return None


def legacy_state_candidates(output_dir, legacy_file=None):
    """Older history files to read when the save folder does not have one yet."""
    legacy_file = LEGACY_STATE_FILE if legacy_file is None else legacy_file
    paths = []
    parent_state = output_dir.parent / ".sync_state.json"
    for candidate in (parent_state, legacy_file):
        if candidate not in paths:
            paths.append(candidate)
    return paths


def resolve_state_paths(output_dir, legacy_file=None):
    """Return (load_path or None, save_path).

    save_path is always <output_dir>/.sync_state.json. load_path is that file
    when it exists, otherwise a legacy history file.
    """
    save_path = output_dir / ".sync_state.json"
    if save_path.exists():
        return save_path, save_path
    for candidate in legacy_state_candidates(output_dir, legacy_file):
        if not candidate.exists():
            continue
        try:
            if candidate.resolve() == save_path.resolve():
                continue
        except OSError:
            continue
        return candidate, save_path
    return None, save_path


def load_state(output_dir):
    """Load previously downloaded video IDs and the path new history is written to."""
    load_path, save_path = resolve_state_paths(output_dir)
    if load_path is None:
        return set(), save_path
    try:
        if load_path.resolve() != save_path.resolve():
            log(f"Using existing history from {load_path}")
    except OSError:
        log(f"Using existing history from {load_path}")
    try:
        with open(load_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("downloaded", [])), save_path
    except (OSError, json.JSONDecodeError) as exc:
        log(f"WARNING: Could not read state file {load_path}: {exc}")
        return set(), save_path


def save_state(downloaded_ids, state_path):
    """Save the set of downloaded video IDs."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({
            "downloaded": list(downloaded_ids),
            "last_sync": datetime.now().isoformat()
        }, f, indent=2)


def _package_installed(name):
    result = subprocess.run(
        [sys.executable, "-m", "pip", "show", name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_python_deps():
    """Install requirements.txt when yt-dlp or yt-dlp-ejs is missing."""
    if importlib.util.find_spec("yt_dlp") is not None and _package_installed("yt-dlp-ejs"):
        return True

    req = SCRIPT_DIR / "requirements.txt"
    if not req.exists():
        log(f"ERROR: Missing {req.name}. Cannot install yt-dlp.")
        return False

    log("Installing Python packages from requirements.txt...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    if result.returncode != 0:
        log("ERROR: Could not install Python packages.")
        log(f"       Run: {sys.executable} -m pip install -r {req}")
        return False
    importlib.invalidate_caches()
    return True


def detect_js_runtime():
    """Prefer Deno (yt-dlp default), otherwise Node if installed."""
    if shutil.which("deno"):
        return "deno"
    if shutil.which("node"):
        return "node"
    return None


def _browser_youtube_login(browser):
    """Return (logged_in, error). Checks cookie names only, not cookie values."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = extract_cookies_from_browser(browser)
    except Exception as exc:
        return False, _short_error(str(exc))
    names = set()
    for cookie in jar:
        if "youtube.com" in (cookie.domain or "") and cookie.name in {
            "LOGIN_INFO", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID",
        }:
            names.add(cookie.name)
    logged_in = "LOGIN_INFO" in names and bool(
        names.intersection({"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"})
    )
    return logged_in, None


def select_cookie_source():
    """Use a cookies file, or a browser that is actually logged into YouTube."""
    global COOKIES_FILE, COOKIES_BROWSER, _cookie_note
    _cookie_note = None
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        COOKIES_BROWSER = None
        return

    firefox_ok, _firefox_err = _browser_youtube_login("firefox")
    if firefox_ok:
        COOKIES_BROWSER = "firefox"
        return

    edge_ok, edge_err = _browser_youtube_login("edge")
    if edge_ok:
        COOKIES_BROWSER = "edge"
        _cookie_note = "Using Edge for YouTube login. Firefox is not logged in."
        return

    COOKIES_BROWSER = None
    note = "Firefox is not logged into YouTube, so your library playlists can't be listed."
    if edge_err and "copy" in edge_err.lower():
        note += " Edge is open, so its cookies can't be read. Close Edge completely, then run this again."
    _cookie_note = note


def get_cookies_args():
    """Return yt-dlp cookies flags based on config (file preferred over browser)."""
    if COOKIES_FILE:
        p = Path(COOKIES_FILE)
        if p.exists():
            return ["--cookies", str(p)]
    if COOKIES_BROWSER:
        return ["--cookies-from-browser", COOKIES_BROWSER]
    return []


def ytdlp_subprocess_kwargs():
    """Decode yt-dlp output as UTF-8 so playlist titles survive a non-UTF-8 console."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return {
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
    }


def get_ytdlp_base_args(*, warnings=True):
    """Shared yt-dlp flags required for current YouTube extraction."""
    args = [sys.executable, "-m", "yt_dlp", "--no-update"]
    runtime = detect_js_runtime()
    if runtime:
        args.extend(["--js-runtimes", runtime])
    args.extend(["--remote-components", "ejs:github"])
    args.extend(["--extractor-args", "youtube:player_client=default,web_embedded"])
    args.extend(get_cookies_args())
    if not warnings:
        args.append("--no-warnings")
    return args


def cookie_source_label():
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        return f"file ({COOKIES_FILE})"
    if COOKIES_BROWSER:
        return f"browser ({COOKIES_BROWSER})"
    return "none"


def note_cookie_rotation(line):
    """Print a one-time hint if yt-dlp reports rotated YouTube cookies."""
    global _cookie_rotation_warned
    if _cookie_rotation_warned:
        return
    lower = line.lower()
    if "cookies are no longer valid" in lower or "been rotated" in lower:
        _cookie_rotation_warned = True
        log("  Note: YouTube cookies look rotated/expired. Public videos can still download;")
        log("        refresh Firefox login or re-export cookies.txt for private/age-restricted items.")


def _short_error(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    for line in reversed(lines):
        if "ERROR" in line or "error" in line.lower():
            return line[:300]
    return lines[-1][:300]


def log_startup_checks():
    """Print yt-dlp / JS runtime / ffmpeg / cookie status. Abort if a requirement is missing."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-update", "--version"],
            capture_output=True,
            timeout=60,
            **ytdlp_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"ERROR: Could not run yt-dlp: {exc}")
        return False

    if result.returncode != 0:
        log(f"ERROR: yt-dlp failed to start: {_short_error(result.stderr or result.stdout)}")
        return False

    version_lines = (result.stdout or result.stderr).strip().splitlines()
    version = version_lines[0] if version_lines else "unknown"
    log(f"yt-dlp: {version} ({sys.executable} -m yt_dlp)")

    runtime = detect_js_runtime()
    if runtime:
        log(f"JS runtime: {runtime}")
    else:
        log("ERROR: No JavaScript runtime found (install Node or Deno).")
        log("       YouTube downloads will 403 without one. See README.")
        return False

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        log(f"ffmpeg: {ffmpeg}")
    else:
        log("ERROR: ffmpeg is not on PATH. It is required to save MP3s.")
        log("       Download a static build from https://ffmpeg.org/download.html")
        return False

    select_cookie_source()
    log(f"Cookies: {cookie_source_label()}")
    if _cookie_note:
        log(_cookie_note)
    return True


def _valid_channel_id(value):
    if isinstance(value, str) and value.startswith("UC") and len(value) == 24:
        return value
    return None


def _parse_json_document(text):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _entry_playlist(entry, parent_count=None):
    """Return a menu item when a flat-playlist entry is itself a playlist."""
    if not isinstance(entry, dict):
        return None
    playlist_id = entry.get("id")
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    ie_key = entry.get("ie_key")
    is_playlist = (
        entry.get("_type") == "playlist"
        or ie_key in ("YoutubeTab", "YoutubePlaylist")
        or playlist_id in ("LL", "WL")
        or playlist_id.startswith(("PL", "FL", "UU", "OL", "RD", "VL"))
    )
    if not is_playlist:
        return None

    raw_url = entry.get("webpage_url") or entry.get("url") or ""
    if isinstance(raw_url, str) and raw_url.startswith("http") and "list=" in raw_url:
        playlist_url = raw_url
    else:
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

    title = entry.get("title") or playlist_id
    if not isinstance(title, str):
        title = str(playlist_id)
    title = " ".join(title.split())

    count = entry.get("playlist_count")
    if not isinstance(count, int):
        count = entry.get("n_entries") if isinstance(entry.get("n_entries"), int) else None
    # Flat entries often copy the parent tab's playlist_count (how many playlists),
    # which is not the number of videos inside this playlist.
    if isinstance(count, int) and parent_count is not None and count == parent_count:
        count = None
    return {"id": playlist_id, "title": title, "url": playlist_url, "count": count}


def _owner_channel_id(data):
    """Channel id of the logged-in user, when the feed JSON actually includes one.

    A saved playlist can belong to someone else, so a mix of channel ids is not used.
    """
    if not isinstance(data, dict):
        return None
    for key in ("channel_id", "uploader_id"):
        channel_id = _valid_channel_id(data.get(key))
        if channel_id:
            return channel_id
    found = set()
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        channel_id = _valid_channel_id(entry.get("channel_id")) or _valid_channel_id(entry.get("uploader_id"))
        if channel_id:
            found.add(channel_id)
    if len(found) == 1:
        return next(iter(found))
    return None


def build_playlist_menu(feed_entries, channel_id, include_system=True):
    """Favorites (when the channel id is known), then the feed.

    Liked videos and Watch Later are added only for a logged-in library list.
    On a public channel tab those two ids currently 404, so they are left off.
    """
    feed_by_id = {}
    feed_order = []
    for entry in feed_entries:
        if entry["id"] in feed_by_id:
            continue
        feed_by_id[entry["id"]] = entry
        feed_order.append(entry["id"])

    ordered = []
    seen = set()

    def push(item):
        playlist_id = item["id"]
        if playlist_id in seen:
            return
        seen.add(playlist_id)
        ordered.append(feed_by_id.get(playlist_id, item))

    # Legacy Favorites id is "FL" plus the channel id without the "UC" prefix.
    if channel_id:
        favorites_id = "FL" + channel_id[2:]
        push({
            "id": favorites_id,
            "title": "Favorites",
            "url": f"https://www.youtube.com/playlist?list={favorites_id}",
            "count": None,
        })
    if include_system:
        push({
            "id": "LL",
            "title": "Liked videos",
            "url": "https://www.youtube.com/playlist?list=LL",
            "count": None,
        })
        push({
            "id": "WL",
            "title": "Watch Later",
            "url": "https://www.youtube.com/playlist?list=WL",
            "count": None,
        })
    for playlist_id in feed_order:
        push(feed_by_id[playlist_id])
    return ordered


def fetch_playlist_tab(url):
    """Return (playlist entries, owner channel id or None, error message or None)."""
    cmd = [
        *get_ytdlp_base_args(warnings=False),
        "--flat-playlist",
        "--dump-single-json",
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=180,
            **ytdlp_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return [], None, "Timed out while listing playlists."

    if result.stderr:
        for line in result.stderr.splitlines():
            note_cookie_rotation(line)

    data = None
    if (result.stdout or "").strip():
        try:
            data = _parse_json_document(result.stdout)
        except json.JSONDecodeError:
            data = None

    if not isinstance(data, dict):
        message = _short_error(result.stderr) if result.returncode != 0 else "Could not read the playlist list."
        return [], None, message

    parent_count = data.get("playlist_count") if isinstance(data.get("playlist_count"), int) else None
    entries = []
    for entry in data.get("entries") or []:
        item = _entry_playlist(entry, parent_count)
        if item:
            entries.append(item)
    channel_id = _owner_channel_id(data)
    if result.returncode != 0 and not entries:
        return [], channel_id, _short_error(result.stderr)
    return entries, channel_id, None


def _menu_from_entries(entries, channel_id, include_system):
    return build_playlist_menu(entries, channel_id, include_system=include_system)


def _playlist_choice(url, title=None, channel_url=None):
    choice = {
        "id": playlist_id_from_url(url),
        "title": title,
        "url": url,
        "count": None,
    }
    if channel_url:
        choice["channel_url"] = channel_url
    return choice


def format_playlist_line(index, item):
    title = item.get("title") or item.get("id") or item.get("url")
    count = item.get("count")
    suffix = f" ({count})" if isinstance(count, int) else ""
    return f"  {index}. {title}{suffix}"


def prompt_for_pasted_playlist():
    while True:
        url = playlist_url_from_user_text(ask("Playlist URL or id: "))
        if url:
            return {
                "id": playlist_id_from_url(url),
                "title": None,
                "url": url,
                "count": None,
            }
        log("Paste a YouTube playlist URL or a list id (for example PLxxxx, LL, or WL).")


def prompt_for_channel_target():
    """Ask for a channel, @handle, or a playlist URL. Return ('menu', menu, channel_url) or ('playlist', choice)."""
    log("Paste a channel URL (https://www.youtube.com/@name), or a playlist URL.")
    while True:
        text = ask("Channel or playlist: ")
        playlist_url = playlist_url_from_user_text(text)
        if playlist_url:
            return ("playlist", _playlist_choice(playlist_url))
        channel_url = channel_playlists_url(text)
        if not channel_url:
            log("Paste a channel URL like https://www.youtube.com/@name, or a playlist URL.")
            continue
        log(f"Fetching playlists from {channel_url}")
        entries, channel_id, error = fetch_playlist_tab(channel_url)
        if not entries:
            log(f"Could not list playlists for that channel: {error}")
            continue
        return ("menu", _menu_from_entries(entries, channel_id, False), channel_url)


def prompt_for_playlist(playlists, saved_url, feed_error):
    if feed_error:
        log(f"Could not list every playlist: {feed_error}")
        log("You can still paste a playlist URL.")
    log("Your YouTube playlists:")
    for index, item in enumerate(playlists, 1):
        log(format_playlist_line(index, item))
    paste_index = len(playlists) + 1
    log(f"  {paste_index}. Paste a playlist URL")

    default_index = default_menu_index(playlists, saved_url)
    while True:
        if default_index is not None:
            answer = ask(f"Choice [{default_index}]: ")
        else:
            answer = ask("Choice: ")
        kind, value = parse_menu_choice(answer, len(playlists), default_index)
        if kind == "item":
            return playlists[value - 1]
        if kind == "paste":
            return prompt_for_pasted_playlist()
        if kind == "url":
            return _playlist_choice(value)
        log("Enter a number from the list.")


def prepare_output_dir(text):
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_output_dir(config):
    raw = config.get("output_dir")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser()
    return DEFAULT_OUTPUT_DIR


def prompt_for_output_dir(default):
    while True:
        entered = ask(f"Save MP3s to [{default}]: ").strip().strip('"').strip("'")
        try:
            return prepare_output_dir(entered or str(default))
        except OSError as exc:
            log(f"Cannot use that directory: {exc}")


def _with_channel(choice, channel_url):
    if not channel_url:
        return choice
    updated = dict(choice)
    updated["channel_url"] = channel_url
    return updated


def resolve_playlist(args, config):
    if args.playlist:
        url = playlist_url_from_user_text(args.playlist)
        if not url:
            log(f"ERROR: Could not understand playlist {args.playlist!r}.")
            return None
        return _playlist_choice(url)
    if not sys.stdin.isatty():
        log("ERROR: Pass --playlist URL when stdin is not a terminal.")
        return None

    log("Fetching your YouTube playlists...")
    entries, channel_id, feed_error = [], None, None
    if COOKIES_BROWSER or (COOKIES_FILE and Path(COOKIES_FILE).exists()):
        entries, channel_id, feed_error = fetch_playlist_tab(PLAYLIST_FEED_URL)
    channel_url = None
    if entries:
        playlists = _menu_from_entries(entries, channel_id, True)
        return _with_channel(
            prompt_for_playlist(playlists, config.get("playlist_url"), None),
            channel_url,
        )

    saved_channel = config.get("channel_url")
    if isinstance(saved_channel, str) and saved_channel.strip():
        channel_url = saved_channel.strip()
        if feed_error:
            log(f"Library list unavailable ({feed_error}).")
        log(f"Fetching playlists from {channel_url}")
        entries, channel_id, channel_error = fetch_playlist_tab(channel_url)
        if entries:
            playlists = _menu_from_entries(entries, channel_id, False)
            return _with_channel(
                prompt_for_playlist(playlists, config.get("playlist_url"), None),
                channel_url,
            )
        feed_error = channel_error or feed_error

    if feed_error:
        log(f"Could not list library playlists: {feed_error}")
    target = prompt_for_channel_target()
    if target[0] == "playlist":
        return target[1]
    playlists, channel_url = target[1], target[2]
    return _with_channel(
        prompt_for_playlist(playlists, config.get("playlist_url"), None),
        channel_url,
    )


def resolve_output_dir(args, config):
    if args.output:
        try:
            return prepare_output_dir(args.output)
        except OSError as exc:
            log(f"ERROR: Cannot use output directory {args.output!r}: {exc}")
            return None
    if not sys.stdin.isatty():
        log("ERROR: Pass --output DIR when stdin is not a terminal.")
        return None
    return prompt_for_output_dir(default_output_dir(config))


def get_playlist_videos(playlist_url):
    """Fetch all video IDs and titles from the playlist."""
    log("Fetching playlist contents...")

    cmd = [
        *get_ytdlp_base_args(warnings=False),
        "--flat-playlist",
        "--dump-json",
        playlist_url
    ]

    result = subprocess.run(cmd, capture_output=True, **ytdlp_subprocess_kwargs())

    if result.returncode != 0:
        log(f"Error fetching playlist: {result.stderr}")
        return []

    videos = []
    for line in result.stdout.strip().split("\n"):
        if line:
            try:
                data = json.loads(line)
                vid_id = data.get("id")
                title = data.get("title")
                if not title or title == "None":
                    title = f"[ID: {vid_id}]"
                videos.append({
                    "id": vid_id,
                    "title": title
                })
            except json.JSONDecodeError:
                continue

    return videos


def display_title(title, video_id):
    return title if title and title != "None" else f"[ID: {video_id}]"


def is_permanent_failure(error_reason):
    if not error_reason:
        return False
    lower = error_reason.lower()
    return any(phrase in lower for phrase in PERMANENT_FAILURE_PHRASES)


def download_video_as_mp3(video_id, title, output_dir):
    """Download a video and convert to MP3.
    Returns (success: bool, error_reason: str or None)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    log(f"  Downloading: {display_title(title, video_id)}")

    cmd = [
        *get_ytdlp_base_args(warnings=True),
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",  # Best quality
        "--output", str(output_dir / "%(title)s.%(ext)s"),
        "--no-playlist",
        "--newline",  # Progress on new lines
        url
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        **ytdlp_subprocess_kwargs(),
    )

    error_lines = []
    for line in process.stdout:
        line = line.strip()
        if not line or line.startswith("[download]"):
            continue
        is_cookie_rotation = (
            "cookies are no longer valid" in line.lower()
            or "been rotated" in line.lower()
        )
        if is_cookie_rotation:
            note_cookie_rotation(line)
            continue
        log(f"    {line}")
        if "ERROR:" in line or "Private video" in line or "unavailable" in line.lower():
            error_lines.append(line)

    process.wait()
    success = process.returncode == 0
    error_reason = "; ".join(error_lines) if error_lines else None
    return success, error_reason


def main():
    parser = argparse.ArgumentParser(description="Sync a YouTube playlist to MP3")
    parser.add_argument("--limit", type=int, help="Max videos to download")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be downloaded")
    parser.add_argument(
        "--playlist",
        help="Playlist URL or id (PL..., FL..., LL, WL). Skips the playlist menu.",
    )
    parser.add_argument(
        "--output",
        help="Directory to save MP3s in. Skips the folder prompt.",
    )
    args = parser.parse_args()

    log_path = start_run_log()
    try:
        log(f"Log file: {log_path}")
        if not ensure_python_deps():
            return 1
        if not log_startup_checks():
            return 1

        config = load_user_config()
        playlist = resolve_playlist(args, config)
        if not playlist:
            return 1
        output_dir = resolve_output_dir(args, config)
        if not output_dir:
            return 1
        if args.playlist is None or args.output is None:
            save_user_config(playlist, output_dir)

        label = playlist.get("title") or playlist["url"]
        log(f"Playlist: {label}")
        if playlist.get("title"):
            log(f"Playlist URL: {playlist['url']}")
        log(f"Save to: {output_dir}")

        downloaded_ids, state_path = load_state(output_dir)
        log(f"Previously downloaded: {len(downloaded_ids)} videos")

        videos = get_playlist_videos(playlist["url"])
        log(f"Found {len(videos)} videos in playlist")

        if not videos:
            log("No videos found or error fetching playlist.")
            return 1

        new_videos = [v for v in videos if v["id"] not in downloaded_ids]
        log(f"New videos to download: {len(new_videos)}")

        if not new_videos:
            log("Nothing new to download.")
            return 0

        if args.limit and args.limit < len(new_videos):
            new_videos = new_videos[:args.limit]
            log(f"Limited to {args.limit} videos")

        if args.dry_run:
            log("\nWould download:")
            for video in new_videos:
                log(f"  - {display_title(video['title'], video['id'])}")
            return 0

        success_count = 0
        skipped_permanent = 0
        for i, video in enumerate(new_videos, 1):
            log(f"\n[{i}/{len(new_videos)}]")
            success, error_reason = download_video_as_mp3(video["id"], video["title"], output_dir)
            if success:
                downloaded_ids.add(video["id"])
                success_count += 1
                save_state(downloaded_ids, state_path)
            else:
                log(f"  Failed to download: {display_title(video['title'], video['id'])}")
                if is_permanent_failure(error_reason):
                    downloaded_ids.add(video["id"])
                    skipped_permanent += 1
                    log("    → Marked as skipped (permanent: private/removed). Will not retry.")
                    save_state(downloaded_ids, state_path)
                # else: transient error, leave out of state so we retry next run

        msg = f"\nCompleted: {success_count}/{len(new_videos)} new videos downloaded"
        if skipped_permanent:
            msg += f" ({skipped_permanent} permanent failures skipped)"
        log(msg)
        return 0
    except EOFError:
        log("ERROR: Input closed before a choice was made.")
        return 1
    except KeyboardInterrupt:
        log("\nStopped.")
        return 130
    finally:
        close_run_log()


if __name__ == "__main__":
    sys.exit(main())
