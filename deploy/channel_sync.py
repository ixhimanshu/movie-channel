"""Refresh the Movie Channel through ErsatzTV's API; its database is read-only."""
import glob
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote


class ChannelSync:
    def __init__(self, etv, config, library, channel, state_file):
        self.etv, self.config = etv.rstrip('/'), config
        self.library, self.channel = library, channel
        self.state_file = state_file
        self.lock = threading.Lock()
        self.work = queue.Queue()
        self.state = {"status": "idle", "reason": "Channel ready", "revision": 0}
        try:
            with open(state_file) as f:
                self.state.update(json.load(f))
        except FileNotFoundError:
            pass

    def start(self):
        # Replay an interrupted update after a service restart.
        if self.state['status'] in ('queued', 'syncing', 'failed'):
            self.request()
        threading.Thread(target=self.worker, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def save(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file + '.tmp', 'w') as f:
            json.dump(self.state, f)
        os.replace(self.state_file + '.tmp', self.state_file)

    def request(self):
        with self.lock:
            revision = self.state['revision'] + 1
            self.state.update(status='queued', reason='Waiting to scan and reshuffle the channel...', revision=revision)
            self.save()
        self.work.put(revision)

    def update(self, revision, status, reason):
        with self.lock:
            if self.state['revision'] == revision:
                self.state.update(status=status, reason=reason)
                self.save()

    def worker(self):
        while True:
            revision = self.work.get()
            # A single worker serializes changes and combines queued requests.
            while True:
                try:
                    revision = self.work.get_nowait()
                except queue.Empty:
                    break
            try:
                self.refresh(revision)
            except Exception as e:
                print(f'channel refresh failed: {e}', flush=True)
                self.update(revision, 'failed', f'Channel refresh failed: {e}. Click Reshuffle channel to retry.')

    def api(self, path, method='POST', missing_ok=False):
        candidates = [os.path.join(self.config, 'api-secrets.json'),
                      *glob.glob(os.path.join(self.config, '*', 'api-secrets.json'))]
        key_file = next((p for p in candidates if os.path.isfile(p)), None)
        if not key_file:
            raise RuntimeError('ErsatzTV API key not found')
        with open(key_file) as f:
            headers = {'X-Etv-Api-Key': json.load(f)['ApiKey']}
        request = urllib.request.Request(self.etv + path, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as e:
            with e:
                if not (missing_ok and e.code == 404):
                    raise RuntimeError(f'ErsatzTV {method} {path} returned HTTP {e.code}') from e

    def database(self):
        candidates = [os.path.join(self.config, 'ersatztv.sqlite3'),
                      *glob.glob(os.path.join(self.config, '*', 'ersatztv.sqlite3'))]
        path = next((p for p in candidates if os.path.isfile(p)), None)
        if not path:
            raise RuntimeError('ErsatzTV database not found; cannot verify scan completion')
        return path

    def query(self, sql, parameters):
        # Never change ErsatzTV tables or mark a queued API request as complete.
        connection = sqlite3.connect('file:' + quote(self.database(), safe='/') + '?mode=ro', uri=True, timeout=5)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def scans(self):
        rows = self.query('SELECT Id, LastScan FROM LibraryPath WHERE LibraryId = ?', (self.library,))
        if not rows:
            raise RuntimeError('Movie library has no configured paths')
        return dict(rows)

    def build(self):
        rows = self.query('''SELECT p.Seed, b.LastBuild, b.Success, b.Message
                            FROM Playout p JOIN Channel c ON c.Id = p.ChannelId
                            LEFT JOIN PlayoutBuildStatus b ON b.PlayoutId = p.Id
                            WHERE c.Number = ?''', (self.channel,))
        if not rows:
            raise RuntimeError('Movie Channel has no configured playout')
        return rows[0]

    def wait_for(self, condition, description, timeout=600):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise TimeoutError(f'timed out waiting for {description}')
            time.sleep(2)

    def refresh(self, revision):
        before = self.scans()
        self.update(revision, 'syncing', 'Scanning the movie library...')
        self.api(f'/api/libraries/{quote(str(self.library), safe="")}/scan')
        def scanned():
            current = self.scans()
            return bool(current) and all(stamp is not None and stamp != before.get(path)
                                         for path, stamp in current.items())
        self.wait_for(scanned, 'the movie library scan')

        self.update(revision, 'syncing', 'Removing missing movie records and reshuffling...')
        self.api('/api/maintenance/empty_trash')
        old_seed, old_build, _, _ = self.build()
        self.api(f'/api/channels/{quote(self.channel, safe="")}/playout/reset')
        def built():
            seed, last_build, success, message = self.build()
            if last_build is None or last_build == old_build:
                return False
            if not success:
                raise RuntimeError(message or 'ErsatzTV could not build the schedule')
            return seed != old_seed  # Reset creates a fresh shuffle seed.
        self.wait_for(built, 'the shuffled channel schedule')

        self.update(revision, 'syncing', 'Restarting playback with the new shuffle...')
        self.api(f'/api/session/{quote(self.channel, safe="")}', method='DELETE', missing_ok=True)
        self.update(revision, 'done', 'Channel reshuffled. Playback restarted; already buffered video may play for a few seconds.')
