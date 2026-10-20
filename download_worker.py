"""Run a single source in a killable process without bot/database credentials."""

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_flow import Candidate


async def run_isolated(character_id: str, source: str, timeout: float = 300) -> Candidate:
    with tempfile.TemporaryDirectory(prefix="card-job-") as directory:
        root = Path(directory)
        result_path = root / "result.json"
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
        try:
            await asyncio.wait_for(process.wait(), timeout)
            if process.returncode != 0 or not result_path.is_file() or result_path.stat().st_size > 4096:
                raise ValueError("Источник не вернул допустимую карточку.")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            card = Path(result["file_path"]).resolve(strict=True)
            if card.parent != root.resolve() or card.suffix != ".png":
                raise ValueError("Недопустимый файл результата.")
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


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import parser
    from download_flow import DownloadSource

    character_id, source, result_name = sys.argv[1:]
    result_path = Path(result_name)
    if result_path.resolve().parent != Path.cwd().resolve() or result_path.name != "result.json":
        raise ValueError("Invalid result path")
    if not character_id or len(character_id) > 200 or not all(char.isascii() and (char.isalnum() or char in "_-") for char in character_id):
        raise ValueError("Invalid character identifier")
    downloaders = {"datacat": parser._download_datacat_character_sync,
                   "jannyai": parser._download_janny_character_sync,
                   "proxy": parser._download_janitor_character_sync}
    parser._character_file_path = lambda: str(result_path.parent / "character.png")
    candidate = parser._download_candidate_sync(downloaders[source], character_id, DownloadSource(source))
    result_path.write_text(json.dumps({"file_path": candidate.file_path,
                                       "definition_available": candidate.definition_available,
                                       "notice": candidate.notice}), encoding="utf-8")


if __name__ == "__main__":
    main()
