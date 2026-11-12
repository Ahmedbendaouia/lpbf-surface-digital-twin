"""
twin_app.py -- run the LPBF surface digital twin locally.

    python twin_app.py              serve the twin and open it in a browser window
    python twin_app.py --rebuild    rebuild data.json from raw/ first (build_twin_data.py), then open the twin
    python twin_app.py --no-window  only serve it; open the printed address in any browser

Serves this folder on 127.0.0.1 (this computer only). Works offline: Three.js is in vendor/.
"""
import argparse
import functools
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
BROWSERS = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".json": "application/json"}

    def log_message(self, fmt, *args):
        if "favicon" not in str(args[0]) and not str(args[1]).startswith(("2", "3")):
            sys.stderr.write("  ! " + fmt % args + "\n")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser(description="LPBF surface digital twin")
    ap.add_argument("--rebuild", action="store_true", help="rebuild data.json from raw/ first")
    ap.add_argument("--no-window", action="store_true", help="serve only, do not open a window")
    ap.add_argument("--port", type=int, default=0, help="port (default: any free port)")
    args = ap.parse_args()

    if args.rebuild and subprocess.call([sys.executable, str(HERE / "build_twin_data.py")]) != 0:
        sys.exit("Rebuild failed - see the messages above.")
    if not (HERE / "data.json").is_file():
        sys.exit("data.json not found. Run:  python twin_app.py --rebuild")

    port = args.port or free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(QuietHandler, directory=str(HERE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"Surface twin running at {url}")

    browser = None if args.no_window else next((p for p in BROWSERS if p.is_file()), None)
    try:
        if browser:
            profile = Path(tempfile.gettempdir()) / "lpbf_surface_twin_profile"
            proc = subprocess.Popen([str(browser), f"--app={url}", f"--user-data-dir={profile}",
                                     "--window-size=1500,950", "--no-first-run", "--no-default-browser-check"])
            print("Close the twin window to quit.")
            proc.wait()
        else:
            if not args.no_window:
                webbrowser.open(url)
            print("Press Ctrl+C to quit.")
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        print("Surface twin stopped.")


if __name__ == "__main__":
    main()
