"""Run a single source in a killable process without bot/database credentials."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_flow import Candidate, DownloadProgress, DownloadPhase, DownloadSource, ProgressCallback, notify_progress
from download_errors import SourceError, describe_error
import download_trace as trace


def _download_has_started(path: Path) -> bool:
    try:
        with path.open('rb') as stream:
            body = stream.read(trace.MAX_BYTES + 4097)
        if len(body) > trace.MAX_BYTES + 4096:
            return False
        for line in body.splitlines():
            try:
                event = trace.sanitize(json.loads(line))
            except (ValueError, UnicodeError):
                continue
            if event.get('event') == 'started' and event.get('operation') == 'download_image':
                return True
    except OSError:
        pass
    return False


async def run_isolated(character_id: str, source: str, timeout: float = 300,
                       *, on_progress: ProgressCallback | None = None) -> Candidate:
    with tempfile.TemporaryDirectory(prefix="card-job-") as directory:
        root = Path(directory)
        result_path = root / "result.json"
        events_path = root / "events.jsonl"
        job = trace.current_job() or uuid4().hex
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE",
                   "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG", "LD_LIBRARY_PATH", "XDG_CACHE_HOME",
                   "XDG_CONFIG_HOME",
                   "MY_COOKIES", "JANITORAI_EMAIL", "JANITORAI_PASSWORD", "JANNYAI_EMAIL", "JANNYAI_PASSWORD",
                   "JANITOR_STORAGE_STATE_PATH", "PLAYWRIGHT_BROWSERS_PATH", "CLOAKBROWSER_PROXY"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(Path(__file__).resolve()), character_id, source, str(result_path),
            cwd=directory, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
        waiter = asyncio.create_task(process.wait())

        async def wait_with_progress():
            announced = False
            title = ''
            while True:
                if on_progress is not None:
                    new_title = title
                    try:
                        with root.joinpath('title.json').open('rb') as stream:
                            body = stream.read(2049)
                        if len(body) <= 2048:
                            new_title = trace.clean_title(json.loads(body)) or title
                    except (OSError, ValueError, UnicodeError):
                        pass
                    downloading = announced or _download_has_started(events_path)
                    if new_title != title or downloading != announced:
                        title, announced = new_title, downloading
                        phase = DownloadPhase.DOWNLOADING if downloading else DownloadPhase.CHECKING
                        await notify_progress(on_progress, DownloadProgress(DownloadSource(source), phase, title))
                if waiter.done():
                    return await waiter
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), .15)
                except asyncio.TimeoutError:
                    continue

        try:
            await asyncio.wait_for(wait_with_progress(), timeout)
            if not result_path.is_file():
                raise SourceError("worker_failed", exit_code=process.returncode)
            if result_path.stat().st_size > 4096:
                raise SourceError("worker_invalid_result", exit_code=process.returncode)
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                raise SourceError("worker_invalid_result", exit_code=process.returncode) from None
            if not isinstance(result, dict):
                raise SourceError("worker_invalid_result", exit_code=process.returncode)
            if "error" in result:
                failure = SourceError.from_payload(result["error"])
                failure.exit_code = process.returncode
                raise failure
            if process.returncode != 0:
                raise SourceError("worker_failed", exit_code=process.returncode)
            if not isinstance(result.get("file_path"), str) or type(result.get("definition_available")) is not bool:
                raise SourceError("worker_invalid_result")
            try:
                card = Path(result["file_path"]).resolve(strict=True)
            except (OSError, ValueError):
                raise SourceError("worker_invalid_result") from None
            if card.parent != root.resolve() or card.suffix != ".png" or not card.is_file():
                raise SourceError("worker_invalid_result")
            descriptor, destination = tempfile.mkstemp(prefix="character-", suffix=".png")
            os.close(descriptor)
            try:
                os.replace(card, destination)
                os.chmod(destination, 0o600)
                return Candidate(destination, result["definition_available"] is True, result.get("notice"))
            except BaseException:
                Path(destination).unlink(missing_ok=True)
                raise
        finally:
            # Kill the whole browser process tree, including after a successful result.
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                await asyncio.to_thread(subprocess.run,
                                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            if process.returncode is None:
                process.kill()
            await process.wait()
            await waiter
            trace.relay(events_path, job, source)


def main():
    character_id, source, result_name = sys.argv[1:]
    result_path = Path(result_name)
    if result_path.resolve().parent != Path.cwd().resolve() or result_path.name != "result.json":
        raise ValueError("Invalid result path")
    if not character_id or len(character_id) > 200 or not all(char.isascii() and (char.isalnum() or char in "_-") for char in character_id):
        raise ValueError("Invalid character identifier")
    try:
        import parser
        from download_flow import DownloadSource

        downloaders = {"datacat": parser._download_datacat_character_sync,
                       "jannyai": parser._download_janny_character_sync,
                       "proxy": parser._download_janitor_character_sync}
        parser._character_file_path = lambda: str(result_path.parent / "character.png")
        with trace.session(character_id, source, result_path.parent / "events.jsonl"):
            candidate = parser._download_candidate_sync(downloaders[source], character_id, DownloadSource(source))
        result = {"file_path": candidate.file_path, "definition_available": candidate.definition_available,
                  "notice": candidate.notice}
    except Exception as exc:
        result_path.write_text(json.dumps({"error": describe_error(exc).to_payload()}), encoding="utf-8")
        return 1
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
