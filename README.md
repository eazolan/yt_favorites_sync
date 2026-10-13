# YouTube playlist to MP3 sync

Downloads new videos from a YouTube playlist and converts them to MP3.

Originally used on macOS. Adapted for Windows 11. When you run it, the script installs its Python packages if they are missing, lists your YouTube playlists, and asks which folder to save into.

## Prerequisites

- Python 3.10+
- `ffmpeg` on PATH (for audio extraction). Download from https://ffmpeg.org/ if missing.
- A JavaScript runtime for YouTube downloads: **Node.js 22+** or **Deno 2.3+** (yt-dlp's default). Without one, media URLs 403.
- A logged-in browser session with access to the target playlist.

`yt-dlp[default]` and `yt-dlp-ejs` are installed automatically with the same Python that runs the script. ffmpeg and the JavaScript runtime are not Python packages, so those still need to be installed yourself.

## Quick Start

1. **Authentication (required for your own playlists)**

   YouTube requires cookies/login for personal playlists.

   **Recommended easy method (Firefox):**
   - Install Firefox if not present.
   - Log into YouTube with the account that owns the playlists.
   - If there is no `cookies.txt` next to the script, it uses Firefox cookies (`COOKIES_BROWSER = "firefox"`).

   **More reliable long-term: export cookies.txt**
   - While logged into YouTube (preferably in a private/incognito window, visit https://www.youtube.com/robots.txt then export):
     - Use the Chrome extension **"Get cookies.txt LOCALLY"** (https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
     - Or run: `yt-dlp --cookies-from-browser firefox --cookies cookies.txt "https://www.youtube.com/robots.txt"`
   - Place `cookies.txt` next to this script. It is picked up automatically.
   - For a different path, edit `youtube_favorites_sync.py` and set:
     ```python
     COOKIES_FILE = "cookies.txt"   # or absolute path
     COOKIES_BROWSER = None
     ```

   Note: Chrome/Edge `--cookies-from-browser` is often broken on Windows 11 due to Chrome's App-Bound Encryption (DPAPI decryption fails). Firefox usually works.

   YouTube rotates cookies. If the script reports they are no longer valid, re-login in Firefox or re-export `cookies.txt`. Public videos can still download; private/age-restricted ones need fresh cookies.

2. Run:
   ```
   python youtube_favorites_sync.py
   ```
   The script lists playlists from your YouTube library when Firefox or Edge is logged into YouTube. Firefox has to actually be logged in. Edge has to be fully closed, or Windows will not let the script read its cookies. If the library list is unavailable, the script uses the channel saved in `user_config.json` and asks which playlist to sync. You can also paste a playlist URL. Then it asks where to save the MP3s. Press Enter to accept the bracketed default.

   The first suggested folder is `~/Downloads/Youtube/mp3`. Later runs offer the last playlist and folder you chose. Those choices are stored in `user_config.json` next to the script.

   Other runs:
   ```
   python youtube_favorites_sync.py --dry-run
   python youtube_favorites_sync.py --limit 5
   python youtube_favorites_sync.py --playlist LL --output "%USERPROFILE%\Music\YouTube"
   ```

   `--playlist` takes a playlist URL or a list id (`PLxxxx`, `FLxxxx`, `LL`, `WL`) and skips the menu. `--output` is the save folder and skips the folder prompt. When stdin is not a terminal, pass both.

## State / History

- Tracked in `<save folder>/.sync_state.json`.
- Stores video IDs of successfully processed items, plus videos confirmed permanently gone (private/removed).
- Transient failures (HTTP 403, extractor errors) are left out of state so the next run retries them.
- If the save folder does not have a history file yet, the script reads one of these when it exists, then writes new history into the save folder (the old file is left in place):
  - `<parent of the save folder>/.sync_state.json`
  - `~/Downloads/Youtube/.sync_state.json` (the location used by earlier versions of this script)
- Without a state file, it may attempt videos you already have.

## Notes / Gotchas (2026)

- YouTube signs media URLs with a JS n-challenge. The script passes `--js-runtimes` (Deno if present, else Node) and `--remote-components ejs:github`, and uses `player_client=default,web_embedded`.
- Many entries in old Favorites lists are now private/deleted. Only those are marked skipped permanently.
- Cookies can expire/rotate. Re-export if you get "Sign in..." or "cookies are no longer valid".
- Each run writes `sync_YYYYMMDD_HHMMSS.log` next to the script.
- MP3s go in the folder you chose.

## Behavior

- Uses yt-dlp under the hood (`python -m yt_dlp`).
- Converts best audio to high-quality MP3.
- Saves progress after each successful download.

## Troubleshooting

- `HTTP Error 403: Forbidden` while downloading format 251: yt-dlp is too old, or no JS runtime / EJS solver. Upgrade yt-dlp and confirm `node` or `deno` is on PATH.
- `Failed to decrypt with DPAPI`: Don't use chrome/edge cookies-from-browser. Use firefox or cookies file.
- No new videos but you expect some: restore the state file or delete `.sync_state.json` to start fresh (careful).
- Private video errors: Normal for old favorites that are now private. They are skipped.
- The script asks for a channel instead of listing your library: YouTube's library page refused the login cookies (often HTTP 401). Paste your channel URL or `@handle` and pick a playlist, or paste a playlist URL. Refresh Firefox or `cookies.txt` if your own private playlists are missing from the channel page.

## Updating yt-dlp

The script installs missing packages. It does not upgrade them on every run. To update:

```powershell
python -m pip install -U -r requirements.txt
```

See yt-dlp docs for more: https://github.com/yt-dlp/yt-dlp
