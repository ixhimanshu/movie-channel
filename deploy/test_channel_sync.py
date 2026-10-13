import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from channel_sync import ChannelSync


class ChannelSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sync = ChannelSync('http://ersatztv:8409', str(self.root), '1', '1', str(self.root / 'state/sync.json'))
        self.db = self.root / 'ersatztv.sqlite3'
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript('''
                CREATE TABLE LibraryPath (Id INTEGER, LibraryId INTEGER, LastScan TEXT);
                INSERT INTO LibraryPath VALUES (1, 1, 'old');
                CREATE TABLE Channel (Id INTEGER, Number TEXT);
                INSERT INTO Channel VALUES (1, '1');
                CREATE TABLE Playout (Id INTEGER, ChannelId INTEGER, Seed INTEGER);
                INSERT INTO Playout VALUES (1, 1, 100);
                CREATE TABLE PlayoutBuildStatus (PlayoutId INTEGER, LastBuild TEXT, Success INTEGER, Message TEXT);
                INSERT INTO PlayoutBuildStatus VALUES (1, 'old', 1, NULL);
            ''')

    def execute(self, sql):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript(sql)

    def test_refresh_waits_for_scan_and_new_shuffle_before_stopping_stream(self):
        self.sync.request()
        calls = []
        def api(path, method='POST', missing_ok=False):
            calls.append((method, path))
            if path.endswith('empty_trash'):
                self.assertEqual(self.sync.scans(), {1: 'new'})
            if method == 'DELETE':
                self.assertEqual(self.sync.build()[:3], (200, 'new', 1))
        def sleep(_):
            if calls[-1][1].endswith('/scan'):
                self.execute("UPDATE LibraryPath SET LastScan = 'new';")
            elif calls[-1][1].endswith('/reset'):
                self.execute("UPDATE Playout SET Seed = 200; UPDATE PlayoutBuildStatus SET LastBuild = 'new';")
        with patch.object(self.sync, 'api', side_effect=api), patch('channel_sync.time.sleep', side_effect=sleep) as sleeper:
            self.sync.refresh(1)
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(calls, [('POST', '/api/libraries/1/scan'), ('POST', '/api/maintenance/empty_trash'),
                                 ('POST', '/api/channels/1/playout/reset'), ('DELETE', '/api/session/1')])
        self.assertEqual(self.sync.snapshot()['status'], 'done')

    def test_scan_timeout_never_purges_or_resets_schedule(self):
        with patch.object(self.sync, 'api') as api, patch.object(self.sync, 'wait_for', side_effect=TimeoutError('scan incomplete')):
            with self.assertRaises(TimeoutError):
                self.sync.refresh(0)
        api.assert_called_once_with('/api/libraries/1/scan')

    def test_failed_build_does_not_restart_playback_or_report_success(self):
        self.sync.request()
        def api(path, **kwargs):
            if path.endswith('/scan'):
                self.execute("UPDATE LibraryPath SET LastScan = 'new';")
            if path.endswith('/reset'):
                self.execute("UPDATE PlayoutBuildStatus SET LastBuild = 'failed', Success = 0, Message = 'invalid schedule';")
        with patch.object(self.sync, 'api', side_effect=api) as mock:
            with self.assertRaisesRegex(RuntimeError, 'invalid schedule'):
                self.sync.refresh(1)
        self.assertNotIn('/api/session/1', [call.args[0] for call in mock.call_args_list])
        self.assertNotEqual(self.sync.snapshot()['status'], 'done')

    def test_refresh_build_without_new_seed_is_not_treated_as_reset(self):
        def api(path, **kwargs):
            if path.endswith('/scan'):
                self.execute("UPDATE LibraryPath SET LastScan = 'new';")
            if path.endswith('/reset'):
                self.execute("UPDATE PlayoutBuildStatus SET LastBuild = 'new';")
        def wait(condition, description, **kwargs):
            if 'scan' in description:
                self.assertTrue(condition())
            else:
                self.assertFalse(condition())
                raise TimeoutError('shuffle not complete')
        with patch.object(self.sync, 'api', side_effect=api), patch.object(self.sync, 'wait_for', side_effect=wait):
            with self.assertRaisesRegex(TimeoutError, 'shuffle not complete'):
                self.sync.refresh(0)

    def test_new_request_cannot_be_overwritten_by_old_completion(self):
        self.sync.request()
        self.sync.request()
        self.sync.update(1, 'done', 'old update')
        self.assertEqual(self.sync.snapshot()['status'], 'queued')
        resumed = ChannelSync(self.sync.etv, self.sync.config, '1', '1', self.sync.state_file)
        with patch('channel_sync.threading.Thread'):
            resumed.start()
        self.assertEqual(resumed.work.get_nowait(), 3)

    def test_database_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.sync.query('DELETE FROM LibraryPath WHERE LibraryId = ?', ('1',))
        self.assertEqual(self.sync.scans(), {1: 'old'})

    def test_api_session_missing_is_success_but_other_errors_are_not(self):
        (self.root / 'api-secrets.json').write_text(json.dumps({'ApiKey': 'test-only'}))
        error = urllib.error.HTTPError('http://test', 404, 'missing', {}, None)
        with patch('channel_sync.urllib.request.urlopen', side_effect=error):
            self.sync.api('/api/session/1', 'DELETE', missing_ok=True)
        error = urllib.error.HTTPError('http://test', 401, 'unauthorized', {}, None)
        with patch('channel_sync.urllib.request.urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                self.sync.api('/api/session/1', 'DELETE', missing_ok=True)


if __name__ == '__main__':
    unittest.main()
