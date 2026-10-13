"""Paste-a-link movie downloader for the Movie Channel ("submitter" service on Easypanel).

Serves a page at /submit/ (nginx puts it behind the admin password). Submitted videos
download directly into /media/incoming and move to
/media/Movies/<Title>/<Title>.<ext> only when complete, so ErsatzTV never sees a partial
file. Jobs are kept in /media/.submit/jobs.json so the list survives restarts.

  - Internet Archive links: the item's video file is downloaded directly
  - everything else: yt-dlp (YouTube etc.), at most 720p since the channel streams 720p
"""
import glob
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse
from channel_sync import ChannelSync

MEDIA = os.environ.get("MEDIA", "/media")
MOVIES = os.path.join(MEDIA, "Movies")
INCOMING = os.path.join(MEDIA, "incoming")
STATE = os.path.join(MEDIA, ".submit", "jobs.json")
ETV = os.environ.get("ETV_URL", "http://ersatztv:8409")
ETV_CONFIG = os.environ.get("ETV_CONFIG", "/etv")
LIBRARY = os.environ.get("ETV_LIBRARY_ID", "1")
CHANNEL = os.environ.get("ETV_CHANNEL_NUMBER", "1")
channel_sync = ChannelSync(ETV, ETV_CONFIG, LIBRARY, CHANNEL,
                           os.path.join(MEDIA, ".submit", "channel-sync.json"))
MIN_FREE = 5 * 1024**3  # keep room on the disk for the server's other projects
VIDEO_EXTS = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm", ".ogv", ".mpg", ".mpeg")
ACTIVE = ("queued", "checking", "downloading")  # resume legacy checking jobs too

jobs = {}
lock = threading.Lock()
work = queue.Queue()


# ---- job state ----

def save():
    # caller holds lock
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(list(jobs.values()), f, indent=1)
    os.replace(STATE + ".tmp", STATE)


def update(job, **fields):
    with lock:
        job.update(fields)
        save()


def set_progress(job, fraction):
    # progress changes constantly, so it is only kept in memory
    with lock:
        job["progress"] = round(min(fraction, 1) * 100, 1)


def load():
    try:
        with open(STATE) as f:
            saved = json.load(f)
    except FileNotFoundError:
        return
    for job in saved:
        jobs[job["id"]] = job
        if job["status"] in ACTIVE:  # interrupted by a restart: start again (downloads resume)
            job.update(status="queued", reason="restarted, waiting", progress=None)
            work.put(job)


# ---- processing ----

def worker():
    while True:
        job = work.get()
        try:
            process(job)
        except Exception as e:
            traceback.print_exc()
            update(job, status="failed", reason=f"{type(e).__name__}: {e}", progress=None)


def process(job):
    if shutil.disk_usage(MEDIA).free < MIN_FREE:
        return update(job, status="failed", reason="the server's disk is almost full")

    update(job, status="downloading", reason="starting download...", progress=0)
    tmp = os.path.join(INCOMING, job["id"])
    os.makedirs(tmp, exist_ok=True)
    prepare = prepare_archive if is_archive(job["url"]) else prepare_ytdlp
    found_title, fetch = prepare(job, tmp)
    title = clean_title(job.get("requested_title") or found_title) or job["id"]
    dest = os.path.join(MOVIES, title)
    if os.path.exists(dest):
        shutil.rmtree(tmp, ignore_errors=True)
        return update(job, status="rejected", title=title, reason=f"'{title}' is already in the library")

    update(job, status="downloading", title=title, reason="downloading...", progress=0)
    src = fetch()
    ext = os.path.splitext(src)[1].lower()
    # assemble the folder in incoming, then rename it into Movies in one step
    staged = os.path.join(tmp, "out")
    os.makedirs(staged, exist_ok=True)
    os.rename(src, os.path.join(staged, title + ext))
    os.makedirs(MOVIES, exist_ok=True)
    os.rename(staged, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    update(job, status="done", progress=None,
           reason=f"added as Movies/{title}/{title}{ext}; the channel is being scanned and reshuffled")
    channel_sync.request()


def is_archive(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "archive.org" or host.endswith(".archive.org")


def clean_title(title):
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title)
    return re.sub(r"\s+", " ", title).strip(" .")[:120]


def with_year(title, year):
    year = str(year or "")[:4]
    if year.isdigit() and f"({year})" not in title:
        return f"{title} ({year})"
    return title


def prepare_archive(job, tmp):
    # https://archive.org/details/<id>[/...] or https://archive.org/download/<id>/<file>
    parts = [unquote(p) for p in urlparse(job["url"]).path.split("/") if p]
    item = parts[1]
    with urllib.request.urlopen(f"https://archive.org/metadata/{quote(item)}", timeout=60) as r:
        data = json.load(r)
    meta = data.get("metadata") or {}
    if parts[0] == "download" and len(parts) > 2:
        name = "/".join(parts[2:])
    else:
        name = pick_archive_file(data.get("files") or [])
    if not name.lower().endswith(VIDEO_EXTS):
        raise RuntimeError(f"'{name}' is not a video file")
    title = with_year(meta.get("title") or item, meta.get("year") or meta.get("date"))
    path = os.path.join(tmp, "video" + os.path.splitext(name)[1].lower())

    def fetch():
        http_download(f"https://archive.org/download/{quote(item)}/{quote(name)}", path, job)
        return path
    return title, fetch


def pick_archive_file(files):
    videos = [f for f in files if f.get("name", "").lower().endswith(VIDEO_EXTS)]
    if not videos:
        raise RuntimeError("this archive.org item has no video file")
    size = lambda f: int(f.get("size") or 0)
    # best quality that is still a sane size; mp4 first since it needs the least work to stream
    for group in ([f for f in videos if f["name"].lower().endswith(".mp4")], videos):
        fitting = [f for f in group if 0 < size(f) <= 4 * 1024**3]
        if fitting:
            return max(fitting, key=size)["name"]
    return min(videos, key=size)["name"]


def http_download(url, path, job):
    for _ in range(30):
        have = os.path.getsize(path) if os.path.exists(path) else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:  # server ignored the range: start over
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                with open(path, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        have += len(chunk)
                        if total:
                            set_progress(job, have / total)
            if not total or have >= total:
                return
        except urllib.error.HTTPError as e:
            if e.code == 416:  # range not satisfiable: the file is already complete
                return
            if e.code in (401, 403, 404):
                raise
        except OSError:
            pass
        time.sleep(5)
    raise RuntimeError("the download kept failing")


def prepare_ytdlp(job, tmp):
    from yt_dlp import YoutubeDL

    def hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d["status"] == "downloading" and total:
            set_progress(job, d.get("downloaded_bytes", 0) / total)

    ydl = YoutubeDL({
        # H.264 first: the server transcodes in software and AV1/VP9 are much slower to decode
        "format": "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][vcodec^=avc1]"
                  "/bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(tmp, "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "js_runtimes": {"node": {}, "deno": {}},
        "progress_hooks": [hook],
    })
    info = ydl.extract_info(job["url"], download=False, process=False)
    title = with_year(info.get("title") or "", info.get("release_year"))

    def fetch():
        nonlocal info
        # YouTube sometimes answers 403 on a stream URL; fresh URLs usually work
        for attempt in range(3):
            try:
                ydl.process_ie_result(info, download=True)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(10)
                info = ydl.extract_info(job["url"], download=False, process=False)
        # the merged file is video.<ext>; video.f137.mp4 style names are leftover parts
        done = [f for f in glob.glob(os.path.join(tmp, "video.*"))
                if os.path.basename(f).count(".") == 1 and f.lower().endswith(VIDEO_EXTS)]
        if not done:
            raise RuntimeError("the download finished but produced no video file")
        return done[0]
    return title, fetch


# ---- web ----

def movie_files(path):
    if os.path.isfile(path):
        return [path] if path.lower().endswith(VIDEO_EXTS) else []
    files = []
    for root, _, names in os.walk(path, followlinks=False):
        for name in names:
            file = os.path.join(root, name)
            if name.lower().endswith(VIDEO_EXTS) and not os.path.islink(file):
                files.append(file)
    return files


def list_movies():
    movies = []
    if not os.path.isdir(MOVIES):
        return movies
    for name in sorted(os.listdir(MOVIES), key=str.casefold):
        path = os.path.join(MOVIES, name)
        if os.path.islink(path):
            continue
        try:
            files = movie_files(path)
            if files:
                movies.append({"id": name, "title": name if os.path.isdir(path) else os.path.splitext(name)[0],
                               "size": sum(os.path.getsize(f) for f in files), "files": len(files)})
        except FileNotFoundError:  # a download or deletion changed the directory
            continue
    return movies


def delete_movie(name):
    # Accept only one existing entry in Movies; never a path supplied by the client.
    if not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in ("/", "\\", "\x00")):
        raise ValueError("invalid movie")
    path = os.path.join(MOVIES, name)
    if os.path.islink(path):
        raise ValueError("cannot delete a linked folder or file")
    if not os.path.exists(path) or not movie_files(path):
        raise FileNotFoundError("movie not found")
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    with lock:
        for job in jobs.values():
            if job.get("status") == "done" and job.get("title") == name:
                job.update(status="deleted", reason="deleted from the server", progress=None)
        save()
    channel_sync.request()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/submit":  # the page's api/ links are relative to /submit/
            self.send_response(301)
            self.send_header("Location", "/submit/")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if self.path == "/submit/":
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path in ("/submit/movies", "/submit/movies/"):
            return self.reply(200, MOVIES_PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/submit/api/movies":
            return self.reply(200, json.dumps(list_movies()).encode(), "application/json")
        if self.path == "/submit/api/channel":
            return self.reply(200, json.dumps(channel_sync.snapshot()).encode(), "application/json")
        if self.path == "/submit/api/jobs":
            with lock:
                listed = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)[:100]
                body = json.dumps(listed)
            return self.reply(200, body.encode(), "application/json")
        self.reply(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path not in ("/submit/api/jobs", "/submit/api/movies/delete", "/submit/api/channel/reshuffle"):
            return self.reply(404, b"not found", "text/plain")
        # JSON only: a plain cross-site form can't send it, so a remembered admin login can't be abused
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.error(415, "send JSON")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.error(400, "invalid JSON")
        if not isinstance(body, dict):
            return self.error(400, "send a JSON object")
        if self.path == "/submit/api/channel/reshuffle":
            channel_sync.request()
            return self.reply(202, json.dumps(channel_sync.snapshot()).encode(), "application/json")
        if self.path == "/submit/api/movies/delete":
            if body.get("confirm") is not True:
                return self.error(400, "confirm deletion first")
            try:
                delete_movie(body.get("id"))
            except ValueError as e:
                return self.error(400, str(e))
            except FileNotFoundError:
                return self.error(404, "movie not found")
            except OSError:
                traceback.print_exc()
                return self.error(500, "could not delete the movie; refresh the list and try again")
            return self.reply(200, json.dumps({"message": "Movie deleted from the server. The channel is being scanned, reshuffled, and restarted."}).encode(), "application/json")
        url = str(body.get("url") or "").strip()
        title = clean_title(str(body.get("title") or ""))
        if not re.match(r"https?://\S+$", url):
            return self.error(400, "paste a full http(s) link")
        job = {"id": uuid.uuid4().hex[:12], "url": url, "requested_title": title, "title": title,
               "status": "queued", "reason": "waiting", "progress": None, "created": time.time()}
        with lock:
            jobs[job["id"]] = job
            save()
        work.put(job)
        self.reply(201, json.dumps(job).encode(), "application/json")

    def error(self, code, message):
        self.reply(code, json.dumps({"error": message}).encode(), "application/json")

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if self.command != "GET":  # the page polls the job list; don't log every poll
            super().log_message(fmt, *args)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Add a Movie</title>
<style>
  :root { --bg: #0b0d1a; --panel: #151829; --line: #262a40; --text: #f4f4f6; --muted: #a9adbf;
    --accent: #f5b041; --ok: #4cc38a; --bad: #ff6b6b; }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 760px; margin: 0 auto; padding: 32px 16px 48px; }
  h1 { font-size: 24px; margin: 0 0 6px; }
  .lead { color: var(--muted); margin: 0 0 22px; }
  form { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px; display: grid; gap: 12px; }
  label { display: grid; gap: 6px; font-weight: 600; font-size: 14px; }
  label span { color: var(--muted); font-weight: 400; }
  input { font: inherit; color: var(--text); background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; width: 100%; }
  input:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  .actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  button { font: inherit; font-weight: 600; color: #1b1300; background: var(--accent); border: 0; border-radius: 8px;
    padding: 10px 18px; cursor: pointer; }
  button:disabled { opacity: .6; cursor: default; }
  .msg { color: var(--bad); }
  h2 { font-size: 16px; margin: 30px 0 10px; color: var(--muted); font-weight: 600; }
  .empty { color: var(--muted); }
  .job { border-bottom: 1px solid var(--line); padding: 12px 0; display: grid; gap: 4px; }
  .top { display: flex; gap: 10px; align-items: baseline; justify-content: space-between; }
  .name { font-weight: 600; overflow-wrap: anywhere; }
  .badge { flex: none; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
    padding: 2px 8px; border-radius: 99px; background: var(--line); color: var(--muted); }
  .badge.downloading { background: rgba(245,176,65,.18); color: var(--accent); }
  .badge.done { background: rgba(76,195,138,.18); color: var(--ok); }
  .badge.rejected, .badge.failed { background: rgba(255,107,107,.16); color: var(--bad); }
  .url, .reason { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
  .url a { color: inherit; }
  .track { height: 5px; border-radius: 3px; background: var(--line); overflow: hidden; margin-top: 4px; }
  .fill { height: 100%; background: var(--accent); width: 0; transition: width .6s linear; }
</style>
</head>
<body>
<main>
  <p><a href="/submit/movies/" style="color: var(--accent)">Manage Movies — view and delete server movies</a></p>
  <h1>Add a movie to the channel</h1>
  <p class="lead">Paste a link to a single video to download it directly.
    Completed downloads automatically refresh and reshuffle the channel.</p>
  <form id="f">
    <label>Video link
      <input id="url" type="url" required placeholder="https://archive.org/details/... or https://www.youtube.com/watch?v=...">
    </label>
    <label>Title <span>(optional, e.g. "Nosferatu (1922)"; otherwise taken from the video)</span>
      <input id="title" type="text" maxlength="120">
    </label>
    <div class="actions"><button id="go">Add movie</button><span class="msg" id="msg"></span></div>
  </form>
  <h2>Submissions</h2>
  <div id="list"><p class="empty">Loading...</p></div>
</main>
<script>
const $ = (id) => document.getElementById(id);
const LABELS = { queued: "queued", checking: "checking", downloading: "downloading", done: "added", deleted: "deleted", rejected: "rejected", failed: "failed" };

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

function render(jobs) {
  const list = $("list");
  list.replaceChildren();
  if (!jobs.length) { list.append(el("p", "empty", "Nothing submitted yet.")); return; }
  for (const j of jobs) {
    const row = el("div", "job");
    const top = el("div", "top");
    top.append(el("span", "name", j.title || j.url), el("span", "badge " + j.status, LABELS[j.status] || j.status));
    const url = el("div", "url"); const a = el("a", null, j.url); a.href = j.url; a.target = "_blank"; a.rel = "noopener"; url.append(a);
    row.append(top, url);
    if (j.status === "downloading") {
      const t = el("div", "track"), f = el("div", "fill"); f.style.width = (j.progress || 0) + "%"; t.append(f); row.append(t);
      row.append(el("div", "reason", (j.progress != null ? j.progress.toFixed(0) + "% · " : "") + (j.reason || "")));
    } else if (j.reason) {
      row.append(el("div", "reason", j.reason));
    }
    list.append(row);
  }
}

async function refresh() {
  try { const r = await fetch("api/jobs", { cache: "no-store" }); if (r.ok) render(await r.json()); } catch (e) {}
}

$("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("go").disabled = true; $("msg").textContent = "";
  try {
    const r = await fetch("api/jobs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: $("url").value, title: $("title").value }) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { $("msg").textContent = data.error || "Could not submit (" + r.status + ")"; return; }
    $("url").value = ""; $("title").value = "";
    refresh();
  } catch (e) { $("msg").textContent = "Could not reach the server"; }
  finally { $("go").disabled = false; }
});

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
</script>
</body>
</html>
"""

MOVIES_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manage Movies</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b0d1a; color: #f4f4f6;
    font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  main { max-width: 900px; margin: auto; padding: 32px 16px 48px; }
  a { color: #f5b041; }
  h1 { font-size: 24px; margin-bottom: 6px; }
  .lead, .details { color: #a9adbf; }
  .tools { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 24px 0; }
  input { flex: 1; min-width: 180px; padding: 10px 12px; border: 1px solid #262a40;
    background: #151829; color: inherit; border-radius: 8px; font: inherit; }
  button { padding: 10px 16px; border: 1px solid #262a40; background: #151829;
    color: inherit; border-radius: 8px; font: inherit; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .delete { color: #ff6b6b; border-color: #75404a; }
  .movie { padding: 16px 0; border-bottom: 1px solid #262a40; display: flex;
    gap: 16px; align-items: center; justify-content: space-between; }
  .title { font-weight: 600; overflow-wrap: anywhere; }
  .info { min-width: 0; }
  .details { font-size: 13px; margin-top: 4px; }
  #message { overflow-wrap: anywhere; }
</style>
</head>
<body><main>
<a href="/submit/">← Add a movie</a>
<h1>Manage Movies</h1>
<p class="lead">Movies stored on the server. Downloads appear here when complete.
Deleting a movie removes its folder and files permanently.
Adding or deleting a movie automatically reshuffles and restarts the channel.</p>
<div class="tools"><input id="search" type="search" placeholder="Search movies" aria-label="Search movies">
<button id="refresh" type="button">Refresh</button><span id="count"></span></div>
<p><button id="reshuffle" type="button">Reshuffle channel</button></p>
<p id="channel" role="status" aria-live="polite"></p>
<p id="message" role="status" aria-live="polite"></p>
<div id="movies">Loading movies...</div>
</main><script>
const $ = id => document.getElementById(id);
let movies = [], deleting = false;
function size(bytes) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return bytes.toFixed(i ? 1 : 0) + ' ' + units[i];
}
function render() {
  const list = $('movies'); list.replaceChildren();
  $('count').textContent = movies.length + (movies.length === 1 ? ' movie' : ' movies');
  const visible = movies.filter(m => m.title.toLocaleLowerCase().includes($('search').value.toLocaleLowerCase()));
  if (!visible.length) { list.textContent = movies.length ? 'No matching movies.' : 'No completed movies on the server yet.'; return; }
  for (const movie of visible) {
    const row = document.createElement('div'); row.className = 'movie';
    const info = document.createElement('div'); info.className = 'info';
    const title = document.createElement('div'); title.className = 'title'; title.textContent = movie.title;
    const details = document.createElement('div'); details.className = 'details';
    details.textContent = size(movie.size) + ' · ' + movie.files + (movie.files === 1 ? ' video file' : ' video files');
    info.append(title, details);
    const button = document.createElement('button'); button.className = 'delete'; button.textContent = 'Delete';
    button.type = 'button'; button.disabled = deleting;
    button.setAttribute('aria-label', 'Delete ' + movie.title);
    button.addEventListener('click', () => removeMovie(movie));
    row.append(info, button); list.append(row);
  }
}
async function refresh() {
  $('refresh').disabled = true;
  try {
    const response = await fetch('/submit/api/movies', {cache: 'no-store'});
    if (!response.ok) throw new Error('Could not load movies (' + response.status + ').');
    movies = await response.json(); render();
  } catch (error) { $('message').textContent = error.message; }
  finally { $('refresh').disabled = false; }
}
async function removeMovie(movie) {
  if (deleting || !confirm('Permanently delete "' + movie.title + '" and its files from the server? This cannot be undone.')) return;
  deleting = true; render(); $('message').textContent = 'Deleting ' + movie.title + '...';
  try {
    const response = await fetch('/submit/api/movies/delete', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: movie.id, confirm: true})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not delete movie.');
    $('message').textContent = data.message; await refresh();
  } catch (error) { $('message').textContent = error.message; }
  finally { deleting = false; render(); }
}
$('search').addEventListener('input', render);
$('refresh').addEventListener('click', refresh);
async function refreshChannel() {
  try {
    const response = await fetch('/submit/api/channel', {cache: 'no-store'});
    if (!response.ok) throw new Error('Could not load channel status.');
    const state = await response.json();
    $('channel').textContent = state.reason;
    $('reshuffle').disabled = state.status === 'queued' || state.status === 'syncing';
  } catch (error) { $('channel').textContent = error.message; }
}
$('reshuffle').addEventListener('click', async () => {
  if (!confirm('Reshuffle the channel from the available movies and restart playback? Missing-file library records will be cleared.')) return;
  $('reshuffle').disabled = true;
  try {
    const response = await fetch('/submit/api/channel/reshuffle', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'});
    if (!response.ok) throw new Error('Could not request reshuffle.');
    await refreshChannel();
  } catch (error) { $('channel').textContent = error.message; $('reshuffle').disabled = false; }
});
refresh();
refreshChannel();
setInterval(() => { if (!document.hidden) refreshChannel(); }, 2000);
</script></body></html>
"""


if __name__ == "__main__":
    load()
    channel_sync.start()
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"submitter listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
