import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import speedtest


SCRIPT = Path(__file__).with_name("speedtest.py")
FIRST_PART = b"a" * (64 * 1024)
LAST_PART = b"b" * 64
PAYLOAD_SIZE = len(FIRST_PART) + len(LAST_PART)


class DownloadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with self.server.lock:
            self.server.headers_received.append(self.headers)
            request_number = len(self.server.headers_received)
            self.server.events.append("redirect" if self.path == "/redirect" else "start")

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/file")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/missing":
            self.send_error(404)
            return

        self.send_response(200)
        if self.path != "/without-length":
            self.send_header("Content-Length", str(PAYLOAD_SIZE))
        self.end_headers()
        self.wfile.write(FIRST_PART)

        if self.path == f"/truncate/{request_number}":
            return

        time.sleep(0.02)
        with self.server.lock:
            self.wfile.write(LAST_PART)
            self.server.events.append("finish")

    def log_message(self, *args):
        pass


class DownloadTimingTests(unittest.TestCase):
    def test_request_timing_includes_open_and_reads_but_excludes_progress(self):
        steps = Mock()
        steps.perf_counter.side_effect = [10.0, 17.0]
        steps.read.side_effect = [FIRST_PART, LAST_PART, b""]
        response = Mock(length=PAYLOAD_SIZE, read=steps.read)
        steps.urlopen.return_value = nullcontext(response)
        terminal = Mock()
        terminal.isatty.return_value = True

        with patch.multiple(
            speedtest,
            time=steps,
            urlopen=steps.urlopen,
            print_progress=steps.print_progress,
            REQUEST_COUNT=1,
        ), patch.object(sys, "argv", ["speedtest.py", "http://example.test/file"]):
            with redirect_stdout(StringIO()), redirect_stderr(terminal):
                exit_code = speedtest.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            steps.mock_calls,
            [
                call.perf_counter(),
                call.urlopen(ANY, timeout=speedtest.TIMEOUT_SECONDS),
                call.read(speedtest.CHUNK_SIZE),
                call.read(speedtest.CHUNK_SIZE),
                call.read(speedtest.CHUNK_SIZE),
                call.perf_counter(),
                call.print_progress(1, PAYLOAD_SIZE, 7.0),
            ],
        )


class SpeedtestTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        self.server.lock = threading.Lock()
        self.server.headers_received = []
        self.server.events = []
        worker = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}
        )
        worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(self.server.shutdown)

    def run_cli(self, path=None):
        args = [sys.executable, str(SCRIPT)]
        if path is not None:
            args.append(f"http://127.0.0.1:{self.server.server_port}{path}")
        env = {**os.environ, "no_proxy": "127.0.0.1", "PYTHONUTF8": "0"}
        env.pop("PYTHONIOENCODING", None)
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            env=env,
            timeout=10,
        )

    def test_ten_sequential_downloads_and_summary(self):
        result = self.run_cli("/file")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.server.events, ["start", "finish"] * 10)
        for headers in self.server.headers_received:
            self.assertEqual(headers["Cache-Control"], "no-cache")
            self.assertEqual(headers["Accept-Encoding"], "identity")

        lines = result.stdout.splitlines()
        count_line, size_line, total_line, average_line, speed_line = lines
        total_bytes = PAYLOAD_SIZE * 10
        self.assertEqual(count_line, "Выполнено запросов: 10")
        self.assertIn(f"({total_bytes} байт)", size_line)
        total_seconds = float(total_line.split()[-2])
        average_seconds = float(average_line.split()[-2])
        speed = speed_line.removeprefix("Скорость скачивания: ")
        mib_speed, mbit_speed = speed.split(" MiB/s (")
        self.assertAlmostEqual(average_seconds, total_seconds / 10, delta=0.001)
        self.assertAlmostEqual(
            float(mib_speed), total_bytes / 2**20 / total_seconds, delta=0.02
        )
        self.assertAlmostEqual(
            float(mbit_speed.removesuffix(" Mbit/s)")),
            total_bytes * 8 / 1_000_000 / total_seconds,
            delta=0.1,
        )

    def test_complete_response_without_content_length(self):
        result = self.run_cli("/without-length")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.events, ["start", "finish"] * 10)
        self.assertIn(f"({PAYLOAD_SIZE * 10} байт)", result.stdout)

    def test_redirect_downloads_final_resource_ten_times(self):
        result = self.run_cli("/redirect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.server.events, ["redirect", "start", "finish"] * 10)
        self.assertIn("Выполнено запросов: 10", result.stdout)
        self.assertIn(f"({PAYLOAD_SIZE * 10} байт)", result.stdout)
        self.assertIn("MiB/s", result.stdout)

    def test_http_404_exits_without_speed(self):
        result = self.run_cli("/missing")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.events, ["start"])
        self.assertEqual(result.stdout, "")
        self.assertIn("Ошибка запроса 1", result.stderr)
        self.assertIn("Файл не найден", result.stderr)
        self.assertNotIn("MiB/s", result.stderr)

    def test_stops_immediately_on_truncated_response(self):
        result = self.run_cli("/truncate/1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.events, ["start"])
        self.assertEqual(result.stdout, "")
        self.assertIn("Ошибка запроса 1", result.stderr)
        self.assertIn("неполный ответ", result.stderr)
        self.assertIn("Ожидалось 65 600 байт, получено 65 536 байт", result.stderr)

    def test_no_summary_when_last_response_is_truncated(self):
        result = self.run_cli("/truncate/10")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.server.events, ["start", "finish"] * 9 + ["start"])
        self.assertEqual(result.stdout, "")
        self.assertIn("Ошибка запроса 10", result.stderr)
        self.assertNotIn("MiB/s", result.stderr)

    def test_missing_url_in_noninteractive_mode_suggests_example(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Укажите URL", result.stderr)
        self.assertIn("https://proof.ovh.net/files/1Mb.dat", result.stderr)


if __name__ == "__main__":
    unittest.main()
