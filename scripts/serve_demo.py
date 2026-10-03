"""Serve demo/ on 127.0.0.1 and map /local-data/ to ignored private demo views.

Usage (repository root):
    python scripts/serve_demo.py [--port 8000]

Open http://127.0.0.1:8000 for synthetic data, or
http://127.0.0.1:8000/?source=local after `hino-analytics export-demo`.
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
LOCAL_VIEWS = ROOT / "data" / "processed" / "demo_views"
LOCAL_PREFIX = "/local-data/"


class DemoHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith(LOCAL_PREFIX):
            name = clean[len(LOCAL_PREFIX) :]
            if "/" in name or "\\" in name or not name.endswith(".local.json"):
                return str(LOCAL_VIEWS / "__not_found__")
            return str(LOCAL_VIEWS / name)
        return super().translate_path(path)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    handler = partial(DemoHandler, directory=str(DEMO))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Synthetic demo:  http://127.0.0.1:{args.port}/")
    print(f"Local private:   http://127.0.0.1:{args.port}/?source=local")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
