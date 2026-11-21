# ytmp-dl

Archive your YouTube Music library into a Plex/PlexAmp-ready music tree.

Reads your liked songs, playlists, and play history; expands every artist to
their full discography; downloads the highest-quality audio YouTube serves;
then tags everything with canonical MusicBrainz metadata (Spotify fallback)
including album, track/disc numbers, date, ISRC, and cover art.

```
music/
  Artist Name/
    Album Title (Year)/
      01 - Track Title.ogg        # or .m4a/.webm/.opus — best source kept
      cover.jpg
```

## Requirements

- Python 3.10+
- ffmpeg / ffprobe (for Opus-in-MP4 remux and fallback tagging)
- A modern browser (Chrome/Edge/Firefox) for cookie export

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.toml.example config.toml   # then edit as needed
```

A standalone `yt-dlp` binary is also supported — place `yt-dlp` on PATH or set
`YTDLP` in `config.toml` (defaults to `yt-dlp`).

## Cookies (required for your library)

YouTube Music has no public API for personal libraries, so the tool uses your
browser session cookies.

1. Install a cookies export extension, e.g.
   **"Get cookies.txt LOCALLY"** (Chrome/Firefox).
2. While logged in to YouTube Music in that browser, open
   `music.youtube.com` and export **all cookies for the site** as
   `cookies.txt` (Netscape format).
3. Save it as `cookies.txt` in this directory (gitignored).

> Your cookies are secrets — never commit or share `cookies.txt` or
> `auth.json`. Treat them like a password.

Then validate:

```bash
.venv/bin/python -m ytmp_dl auth
```

## Windows (one-click install)

On the Windows machine (the one running Plex):

1. Download + extract the source zip, e.g. to `C:\ytmp-dl`.
2. Double-click `windows\install.bat` — it silently installs Python if
   needed, installs dependencies, downloads ffmpeg, builds
   `dist\ytmp-dl.exe`, drops a **ytmp-dl** shortcut on your Desktop, and
   launches the GUI.
3. In the GUI's **Account** panel click **Sign in** — it reads cookies
   directly from your installed browser (Chrome/Edge/Firefox/Brave…),
   no cookie export needed. Paste-cookies fallback included.
4. Hit **Run All**. Music lands in `dist\music\Artist\Album (Year)\` —
   point your Plex music library there.

`ytmp-dl.exe` with no arguments launches the GUI; CLI subcommands work
too (`ytmp-dl.exe discover`, `ytmp-dl.exe download`, …).

## GUI

```bash
.venv/bin/python -m ytmp_dl gui          # or: ./ytmp-dl gui
# Windows exe: ytmp-dl.exe gui
```

Opens a local web UI at `http://127.0.0.1:8377` (binds localhost only):

- stat cards + progress bar (done/pending/failed/skipped, size on disk)
- one-click pipeline steps: Auth, Discover, Artists, Discography,
  Download, Retry Failed, or Run All — jobs queue and run sequentially
- add arbitrary URLs / playlists / artist names
- Artists tab with per-artist progress, Tracks tab with filters
  (status / artist / search) and pagination, live Log console
- "Re-auth" re-reads `cookies.txt` without restarting

## Usage

The pipeline is phased so short-lived cookies only matter up front:

1. **Discovery** (`auth`, `discover`, `artists`, `discography`) — talks to
   YouTube Music, needs fresh cookies. Everything is stored in `ytmp_dl.db`.
2. **Download** (`download`) — reads only the DB + MusicBrainz. yt-dlp tries
   *without* cookies first and only falls back to `cookies.txt` if needed,
   so downloads keep working after your cookies expire.

```bash
# one-shot: auth check → discover library → resolve artists → expand
# discographies → download everything (resumable, safe to re-run)
.venv/bin/python -m ytmp_dl all

# or step by step — run discovery while cookies are fresh, download anytime
.venv/bin/python -m ytmp_dl discover    # liked songs, history, playlists
.venv/bin/python -m ytmp_dl artists     # resolve artist names → YTM channels
.venv/bin/python -m ytmp_dl discography # fetch every album/single + tracklist
.venv/bin/python -m ytmp_dl download    # download + tag (workers in config)
.venv/bin/python -m ytmp_dl status      # progress summary
.venv/bin/python -m ytmp_dl retry       # re-attempt failed tracks

# cookies expired? re-export cookies.txt, then re-run discovery — it
# upserts into the same DB, then continue downloading
.venv/bin/python -m ytmp_dl auth && .venv/bin/python -m ytmp_dl discover

# add content ad-hoc
.venv/bin/python -m ytmp_dl add "https://music.youtube.com/watch?v=..."
.venv/bin/python -m ytmp_dl add --artist "Boards of Canada"
```

No cookies? You can still test with public artists — the `add --artist` path
works unauthenticated for anything YouTube Music exposes publicly.

## How it works

- **Discovery** — `ytmusicapi` reads Liked Music (`LM`), history, and library
  playlists; artist "songs/albums" shelves are expanded through YouTube
  Music's continuation API to get complete discographies.
- **Download** — `yt-dlp` fetches `bestaudio/best`: typically Opus ~130 kbps
  (webm/opus) or AAC ~130 kbps (m4a). Premium-only streams aren't accessible
  without Premium, so this is the best free source.
- **Metadata** — MusicBrainz is queried per artist; every YouTube track is
  fuzzy-matched to a canonical release recording by title + duration, with
  the YouTube album name breaking ties. Spotify is used only when
  MusicBrainz can't match (needs free API keys in `config.toml`).
- **Dedup** — the same song appearing under multiple YouTube videos (album
  upload vs. single upload vs. topic channel) is downloaded once, keyed on
  the MusicBrainz recording ID.
- **State** — everything is tracked in `ytmp_dl.db` (SQLite, WAL mode).
  Interrupts, retries, and re-runs resume where they left off; nothing is
  downloaded twice.

## Notes

- Downloads are the best *free* audio YouTube exposes (~128–160 kbps Opus or
  AAC). With YouTube Premium, higher-bitrate formats may appear and will be
  picked up automatically by the same `bestaudio/best` selector.
- Live recordings, remixes, and deluxe-edition pressings in your library are
  kept as their own albums — that's intentional.
- This tool is for archiving content you have the right to download. Respect
  YouTube's Terms of Service and artists' rights.
