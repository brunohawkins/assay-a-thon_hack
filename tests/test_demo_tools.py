"""The demo launcher (tools/demo.py) and the twin's offline guarantee.

    python -m unittest discover -s tests -v
"""

import json
import re
import socket
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import demo  # noqa: E402


class Launcher(unittest.TestCase):
    def test_presenter_url(self):
        self.assertEqual(demo.presenter_url(8000), "http://localhost:8000/twin/?present")
        self.assertEqual(demo.presenter_url(8001, step=4), "http://localhost:8001/twin/?present#step=4")
        self.assertEqual(demo.presenter_url(8000, twin=True), "http://localhost:8000/twin/")
        self.assertEqual(demo.presenter_url(8000, bench=True), "http://localhost:8000/twin/#view=bench")

    def test_javascript_is_served_as_javascript(self):
        # ES modules refuse to load as text/plain (some Windows registries map .js to it)
        self.assertEqual(demo.Handler.extensions_map[".js"], "text/javascript")

    def test_takes_the_next_free_port(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            taken = busy.getsockname()[1]
            busy.listen()
            httpd, port = demo.make_server(taken)
            try:
                self.assertNotEqual(port, taken)
            finally:
                httpd.server_close()


class Offline(unittest.TestCase):
    def test_three_js_is_vendored_and_the_importmap_points_at_it(self):
        html = (ROOT / "twin" / "index.html").read_text()
        imports = json.loads(re.search(r'<script type="importmap">(.*?)</script>', html, re.S).group(1))["imports"]
        for spec, path in imports.items():
            self.assertFalse(path.startswith("http"), f"{spec} is loaded from the network: {path}")
        self.assertTrue((ROOT / "twin" / imports["three"]).is_file())
        for addon in ("controls/OrbitControls.js", "environments/RoomEnvironment.js", "loaders/STLLoader.js", "renderers/CSS2DRenderer.js"):
            self.assertTrue((ROOT / "twin" / imports["three/addons/"] / addon).is_file(), addon)

    def test_no_script_imports_or_fetches_from_the_network(self):
        for f in (ROOT / "twin").glob("*.js"):
            text = f.read_text()
            self.assertIsNone(re.search(r"""(?:from\s*|import\(\s*|fetch\(\s*)['"`]https?://""", text), f.name)


if __name__ == "__main__":
    unittest.main()
