"""Paste-a-link movie downloader for the Movie Channel ("submitter" service on Easypanel).

Serves a page at /submit/ (nginx puts it behind the admin password). Every link goes
through license_check first; allowed videos download into /media/incoming and move to
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

import license_check

MEDIA = os.environ.get("MEDIA", "/media")
MOVIES = os.path.join(MEDIA, "Movies")
INCOMING = os.path.join(MEDIA, "incoming")
STATE = os.path.join(MEDIA, ".submit", "jobs.json")
ETV = os.environ.get("ETV_URL", "http://ersatztv:8409")
ETV_CONFIG = os.environ.get("ETV_CONFIG", "/etv")
LIBRARY = os.environ.get("ETV_LIBRARY_ID", "1")
MIN_FREE = 5 * 1024**3  # keep room on the disk for the server's other projects
VIDEO_EXTS = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm", ".ogv", ".mpg", ".mpeg")
ACTIVE = ("queued", "checking", "downloading")

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
    update(job, status="checking", reason="checking the license...")
    allowed, why = license_check.check(job["url"])
    if not allowed:
        return update(job, status="rejected", reason=why)
    if shutil.disk_usage(MEDIA).free < MIN_FREE:
        return update(job, status="failed", reason="the server's disk is almost full")

    tmp = os.path.join(INCOMING, job["id"])
    os.makedirs(tmp, exist_ok=True)
    prepare = prepare_archive if is_archive(job["url"]) else prepare_ytdlp
    found_title, fetch = prepare(job, tmp)
    title = clean_title(job.get("requested_title") or found_title) or job["id"]
    dest = os.path.join(MOVIES, title)
    if os.path.exists(dest):
        shutil.rmtree(tmp, ignore_errors=True)
        return update(job, status="rejected", title=title, reason=f"'{title}' is already in the library")

    update(job, status="downloading", title=title, reason=why, progress=0)
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
           reason=f"added as Movies/{title}/{title}{ext}; it joins the channel after the next library scan")
    rescan()


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


def rescan():
    # ErsatzTV also scans every hour by itself, so this only makes new movies show up sooner
    try:
        headers = {}
        for p in [os.path.join(ETV_CONFIG, "api-secrets.json"), *glob.glob(os.path.join(ETV_CONFIG, "*", "api-secrets.json"))]:
            if os.path.exists(p):
                with open(p) as f:
                    headers["X-Etv-Api-Key"] = json.load(f)["ApiKey"]
                break
        req = urllib.request.Request(f"{ETV}/api/libraries/{LIBRARY}/scan", method="POST", headers=headers)
        urllib.request.urlopen(req, timeout=30).close()
        print("asked ErsatzTV to scan the library", flush=True)
    except Exception as e:
        print(f"library scan request failed ({e}); the hourly scan will pick it up", flush=True)


# ---- web ----

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/submit":  # the page's api/ links are relative to /submit/
            self.send_response(301)
            self.send_header("Location", "/submit/")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        if self.path == "/submit/":
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/submit/api/jobs":
            with lock:
                listed = sorted(jobs.values(), key=lambda j: j["created"], reverse=True)[:100]
                body = json.dumps(listed)
            return self.reply(200, body.encode(), "application/json")
        self.reply(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/submit/api/jobs":
            return self.reply(404, b"not found", "text/plain")
        # JSON only: a plain cross-site form can't send it, so a remembered admin login can't be abused
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.error(415, "send JSON")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.error(400, "invalid JSON")
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
  <h1>Add a movie to the channel</h1>
  <p class="lead">Paste a link to a single video. Only public-domain or Creative Commons videos are accepted
    (Internet Archive items marked public domain / CC, or YouTube videos marked Creative Commons).
    New movies join the channel after the next library scan.</p>
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
const LABELS = { queued: "queued", checking: "checking", downloading: "downloading", done: "added", rejected: "rejected", failed: "failed" };

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


if __name__ == "__main__":
    load()
    threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print(f"submitter listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
