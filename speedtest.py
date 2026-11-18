import argparse
import os
import sys
import threading
import time
from contextlib import nullcontext
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from download_errors import ERROR_RULES, HTTP_MESSAGES, TruncatedResponse


REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
TIMEOUT_SECONDS = 30
PROGRESS_INTERVAL_SECONDS = 0.25
BYTES_PER_MIB = 1024 * 1024
DEFAULT_URL = "https://proof.ovh.net/files/1Mb.dat"


def describe_error(error):
    if isinstance(error, HTTPError):
        status = error.code
        if message := HTTP_MESSAGES.get(status):
            return message
        if 500 <= status < 600:
            return f"Ошибка на стороне сервера (HTTP {status}). Попробуйте позже."
        return f"Сервер не смог выполнить запрос (HTTP {status}). Проверьте ссылку."

    if isinstance(error, URLError):
        error = error.reason

    error_code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    for error_types, codes, message in ERROR_RULES:
        if isinstance(error, error_types) or error_code in codes:
            return str(error) if message is None else message

    return "Не удалось скачать файл. Проверьте ссылку и подключение к интернету."


def print_error(message, request_number=None):
    title = f"Ошибка запроса {request_number}" if request_number is not None else "Ошибка"
    print(colorize(f"{title}: {message}", "31", stream=sys.stderr), file=sys.stderr)


def colorize(text, color, stream=None):
    stream = stream if stream is not None else sys.stdout
    if (
        not stream.isatty()
        or "NO_COLOR" in os.environ
        or os.environ.get("TERM") == "dumb"
    ):
        return text

    if os.name == "nt":
        import ctypes
        import msvcrt

        try:
            handle = ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno()))
            mode = ctypes.c_ulong()
            kernel32 = ctypes.windll.kernel32
            if not (
                kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                and kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            ):
                return text
        except (OSError, ValueError):
            return text

    return f"\033[{color}m{text}\033[0m"


class LiveProgress:
    def __init__(self, request_number):
        self.request_number = request_number
        self._counts = (0, None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._line_width = 0
        self._worker = threading.Thread(target=self._render, daemon=True)

    def __enter__(self):
        self._worker.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._worker.join()
        if self._line_width:
            sys.stderr.write("\r" + " " * self._line_width + "\r")
            sys.stderr.flush()

    def update(self, downloaded_bytes, total_size):
        with self._lock:
            self._counts = downloaded_bytes, total_size

    def _render(self):
        while not self._stop.wait(PROGRESS_INTERVAL_SECONDS):
            with self._lock:
                downloaded_bytes, total_size = self._counts

            message = (
                f"[{self.request_number}/{REQUEST_COUNT}] "
                f"Скачано: {downloaded_bytes / BYTES_PER_MIB:.2f} MiB"
            )
            if total_size:
                message += f" ({downloaded_bytes / total_size:.1%})"
            self._line_width = len(message)
            print(
                "\r" + colorize(message, "36", stream=sys.stderr),
                end="",
                file=sys.stderr,
                flush=True,
            )


def print_progress(request_number, downloaded_bytes, elapsed):
    downloaded_mib = downloaded_bytes / BYTES_PER_MIB
    progress = (
        f"[{request_number}/{REQUEST_COUNT}] "
        f"Скачано: {downloaded_mib:.2f} MiB за {elapsed:.3f} с"
    )
    print(
        colorize(progress, "32", stream=sys.stderr),
        file=sys.stderr,
        flush=True,
    )


def download_once(url: str, progress=None) -> tuple[int, float]:
    request = Request(
        url,
        headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"},
    )
    downloaded_bytes = 0
    started_at = time.perf_counter()
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        total_size = getattr(response, "length", None)
        if progress is not None:
            progress.update(downloaded_bytes, total_size)

        while chunk := response.read(CHUNK_SIZE):
            downloaded_bytes += len(chunk)
            if progress is not None:
                progress.update(downloaded_bytes, total_size)

        if total_size is not None and downloaded_bytes != total_size:
            expected = f"{total_size:,}".replace(",", " ")
            received = f"{downloaded_bytes:,}".replace(",", " ")
            raise TruncatedResponse(
                f"Сервер прислал неполный ответ. Ожидалось {expected} байт, "
                f"получено {received} байт."
            )

    elapsed = time.perf_counter() - started_at
    return downloaded_bytes, elapsed


def main():
    parser = argparse.ArgumentParser(
        description="Скачать ресурс 10 раз и измерить скорость скачивания."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="HTTP- или HTTPS-ссылка. Без аргумента терминал предложит тестовый файл.",
    )
    args = parser.parse_args()
    show_progress = sys.stderr.isatty()

    address = args.url
    if address is None:
        if not (sys.stdin.isatty() and show_progress):
            parser.error(f"Укажите URL, например {DEFAULT_URL}")
        print(
            f"Тестовый файл OVHcloud: {DEFAULT_URL} (10 MiB за серию)\n"
            "Введите URL или нажмите Enter, чтобы скачать тестовый файл: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            address = input().strip() or DEFAULT_URL
        except EOFError:
            print(file=sys.stderr)
            parser.error(f"Укажите URL, например {DEFAULT_URL}")

    try:
        url = urlsplit(address)
        port = url.port
    except ValueError:
        parser.error("Некорректный адрес или порт.")
    if url.scheme not in ("http", "https") or not url.hostname or port == 0:
        parser.error("Укажите полный HTTP- или HTTPS-адрес ресурса.")

    total_bytes = 0
    total_time = 0.0

    for request_number in range(1, REQUEST_COUNT + 1):
        progress = LiveProgress(request_number) if show_progress else nullcontext()
        try:
            with progress as live_progress:
                downloaded_bytes, elapsed = download_once(address, live_progress)
        except (URLError, OSError, ValueError, HTTPException) as error:
            print_error(describe_error(error), request_number)
            return 1

        if show_progress:
            print_progress(request_number, downloaded_bytes, elapsed)
        total_time += elapsed
        total_bytes += downloaded_bytes

    average_time = total_time / REQUEST_COUNT
    total_mib = total_bytes / BYTES_PER_MIB
    mib_per_second = total_mib / total_time
    mbit_per_second = total_bytes * 8 / total_time / 1_000_000

    print(colorize(f"Выполнено запросов: {REQUEST_COUNT}", "32"))
    print(f"Скачано: {total_mib:.2f} MiB ({total_bytes} байт)")
    print(f"Суммарное время запросов: {total_time:.3f} с")
    print(f"Среднее время запроса: {average_time:.3f} с")
    speed = f"{mib_per_second:.2f} MiB/s ({mbit_per_second:.2f} Mbit/s)"
    print(colorize(f"Скорость скачивания: {speed}", "1;32"))
    return 0


if __name__ == "__main__":
    if not os.environ.get("PYTHONIOENCODING"):
        for stream in (sys.stdout, sys.stderr):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")

    try:
        exit_code = main()
    except KeyboardInterrupt:
        print("\nЗамер прерван.", file=sys.stderr)
        exit_code = 130
    sys.exit(exit_code)
