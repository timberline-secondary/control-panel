"""CI check that a built control-panel.exe can replace itself with a newer release.

Serves a pretend release from this computer, runs `control-panel.exe --update`
against it, and checks the running exe really was swapped for the download.

    python packaging/check_update.py dist/control-panel.exe
"""

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def main(built_exe: Path) -> None:
    served = built_exe.read_bytes()
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    routes["/download/control-panel.exe"] = served

    def release(tag, digest):
        routes["/latest"] = json.dumps({"tag_name": tag, "assets": [{
            "name": "control-panel.exe", "size": len(served), "digest": f"sha256:{digest}",
            "browser_download_url": f"{base}/download/control-panel.exe",
        }]}).encode()

    def run_update(exe):
        env = dict(os.environ, CONTROL_PANEL_UPDATE_URL=f"{base}/latest")
        result = subprocess.run([str(exe), "--update"], env=env, capture_output=True,
                                text=True, timeout=300)
        print(f"$ {exe.name} --update  (exit {result.returncode})\n{result.stdout}{result.stderr}")
        return result

    with tempfile.TemporaryDirectory() as folder:
        exe = Path(folder) / built_exe.name
        shutil.copy2(built_exe, exe)
        original = exe.stat().st_ino
        good = hashlib.sha256(served).hexdigest()

        release("v0.0.1", good)
        result = run_update(exe)
        check(result.returncode == 0 and "latest version" in result.stdout, "up to date")
        check(exe.stat().st_ino == original, "up to date: exe left alone")

        release("v99.0.0", "0" * 64)
        result = run_update(exe)
        check(result.returncode == 1 and "didn't match" in result.stdout, "bad download refused")
        check(exe.stat().st_ino == original, "bad download: exe left alone")
        check(sorted(p.name for p in exe.parent.iterdir()) == [exe.name], "no files left over")

        release("v99.0.0", good)
        result = run_update(exe)
        check(result.returncode == 0 and "Updated to v99.0.0" in result.stdout, "updated")
        old = exe.with_name(f"{exe.name}.old")
        check(old.stat().st_ino == original, "running exe was moved aside")
        check(exe.stat().st_ino != original and exe.read_bytes() == served, "new exe in place")
        version = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
        check(version.returncode == 0, f"new exe runs: {version.stdout.strip()}")

    server.shutdown()
    print("All update checks passed.")


def check(ok: bool, what: str) -> None:
    print(f"{'ok' if ok else 'FAILED'}: {what}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
