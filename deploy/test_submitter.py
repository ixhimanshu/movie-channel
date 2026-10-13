"""Run with: python3 -m unittest discover -s deploy -p 'test_*.py'."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import submitter


class MovieManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.movies = self.root / "Movies"
        self.movies.mkdir()
        for name, value in (("MOVIES", str(self.movies)), ("STATE", str(self.root / "state/jobs.json")), ("jobs", {})):
            p = patch.object(submitter, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.scan = patch.object(submitter.channel_sync, "request").start()
        self.addCleanup(patch.stopall)

    def movie(self, name="Example"):
        folder = self.movies / name
        folder.mkdir()
        (folder / "movie.mp4").write_bytes(b"video")
        return folder

    def test_list_actual_files_including_unicode_and_flat_files(self):
        self.movie("सरैनोडु")
        (self.movies / "Other.mkv").write_bytes(b"123")
        (self.movies / "incomplete").mkdir()
        (self.movies / "incomplete/movie.mp4.part").write_bytes(b"partial")
        result = submitter.list_movies()
        self.assertEqual({m["id"] for m in result}, {"सरैनोडु", "Other.mkv"})
        self.assertEqual(sum(m["size"] for m in result), 8)

    def test_delete_only_selected_folder_and_update_history(self):
        selected = self.movie("सरैनोडु")
        kept = self.movie("Keep")
        submitter.jobs["job"] = {"status": "done", "title": "सरैनोडु"}
        submitter.delete_movie("सरैनोडु")
        self.assertFalse(selected.exists())
        self.assertTrue(kept.exists())
        self.assertEqual(submitter.jobs["job"]["status"], "deleted")
        self.scan.assert_called_once_with()

    def test_delete_flat_video(self):
        file = self.movies / "Flat.mp4"
        file.write_bytes(b"video")
        submitter.delete_movie(file.name)
        self.assertFalse(file.exists())

    def test_completed_download_requests_reshuffle(self):
        job = {'id': 'new', 'url': 'https://example.com/video', 'status': 'queued', 'requested_title': ''}
        submitter.jobs['new'] = job
        def prepare(job, tmp):
            def fetch():
                file = Path(tmp) / 'video.mp4'
                file.write_bytes(b'video')
                return str(file)
            return 'New Movie', fetch
        with patch.object(submitter, 'MEDIA', str(self.root)), patch.object(submitter, 'INCOMING', str(self.root / 'incoming')), \
                patch.object(submitter, 'MIN_FREE', 0), patch.object(submitter, 'prepare_ytdlp', side_effect=prepare):
            submitter.process(job)
        self.assertEqual(job['status'], 'done')
        self.assertTrue((self.movies / 'New Movie/New Movie.mp4').exists())
        self.scan.assert_called_once_with()

    def test_reject_paths_and_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "movie.mp4").write_bytes(b"keep")
        os.symlink(outside, self.movies / "Linked")
        self.assertEqual(submitter.list_movies(), [])
        for name in ("../outside", str(outside), ".", "..", "", "a/b", "a\\b", "\x00", None, "Linked"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                submitter.delete_movie(name)
        self.assertEqual((outside / "movie.mp4").read_bytes(), b"keep")

    def test_nested_symlink_does_not_delete_target(self):
        folder = self.movie()
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"keep")
        os.symlink(outside, folder / "linked.mp4")
        submitter.delete_movie(folder.name)
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_missing_and_nonmovie_entries_cannot_be_deleted(self):
        (self.movies / "notes.txt").write_text("keep")
        for name in ("missing", "notes.txt"):
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                submitter.delete_movie(name)

    def test_http_confirmation_json_and_pages(self):
        folder = self.movie()
        server = submitter.ThreadingHTTPServer(("127.0.0.1", 0), submitter.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        def post(body, content_type="application/json", path="/submit/api/movies/delete"):
            request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                             headers={"Content-Type": content_type})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                with error:
                    return error.code
        self.assertEqual(post({"id": "Example"}), 400)
        self.assertEqual(post({"id": "Example", "confirm": True}, "text/plain"), 415)
        self.assertEqual(post([]), 400)
        self.assertTrue(folder.exists())
        for path in ("/submit/", "/submit/movies/", "/submit/api/movies", "/submit/api/channel", "/submit/api/playback"):
            with urllib.request.urlopen(base + path) as response:
                self.assertEqual(response.status, 200)
                if path == "/submit/api/playback":
                    self.assertEqual(set(json.load(response)), {"revision"})
        self.assertEqual(post({"id": "Example", "confirm": True}), 200)
        self.assertFalse(folder.exists())
        self.assertEqual(post({"id": "Example", "confirm": True}), 404)
        self.scan.reset_mock()
        self.assertEqual(post({}, path="/submit/api/channel/reshuffle"), 202)
        self.scan.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
