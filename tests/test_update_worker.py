import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


WORKER = Path(__file__).resolve().parents[1] / "update-worker.py"
spec = importlib.util.spec_from_file_location("tally_update_worker", WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class UpdateWorkerTests(unittest.TestCase):
    def test_installs_allowlisted_files_and_preserves_score_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "tally"
            app = root / "app"
            data = root / "data"
            app.mkdir(parents=True)
            data.mkdir()
            (app / "server.py").write_text('APP_VERSION = "1.3.4"\n')
            (app / "VERSION").write_text("1.3.4\n")
            (app / "icon.svg").write_text("<svg old/>")
            score_file = data / "tally.json"
            score_file.write_text('{"cameron": 31, "paget": 28}')
            request = data / "update.req"
            request.write_text('{"version":"2.0.0"}')

            worker.APP_DIR=app
            worker.DATA_DIR=data
            worker.REQUEST=request
            worker.STATUS=data / "update-status.json"
            payloads={
                "VERSION":b"2.0.0\n",
                "server.py":b'APP_VERSION = "2.0.0"\n',
                "icon.svg":b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>",
                "icon-180.png":b"\x89PNG\r\n\x1a\n180",
                "icon-192.png":b"\x89PNG\r\n\x1a\n192",
                "icon-512.png":b"\x89PNG\r\n\x1a\n512",
                "icon-512-maskable.png":b"\x89PNG\r\n\x1a\nmask",
                "update-worker.py":WORKER.read_bytes(),
                "install-worker.sh":b"#!/bin/sh\n",
                "README.md":b"Tally 2.0.0",
            }
            with patch.object(worker,"fetch",side_effect=lambda n:payloads[n]), \
                 patch.object(worker.subprocess,"run",return_value=SimpleNamespace(returncode=0,stderr="",stdout="")) as run:
                self.assertEqual(worker.main(),0)
            self.assertEqual((app/"server.py").read_bytes(),payloads["server.py"])
            self.assertEqual(score_file.read_text(),'{"cameron": 31, "paget": 28}')
            self.assertTrue(any((data/"update-backups").iterdir()))
            with open(worker.STATUS) as fh:
                self.assertEqual(json.load(fh)["state"],"done")
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
