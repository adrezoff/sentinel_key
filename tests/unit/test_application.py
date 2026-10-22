from __future__ import annotations

import base64
import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from application import App
from models import ScanMode, ScanTaskPayload


class ApplicationCliTests(unittest.TestCase):
    def test_cli_scans_local_repository_and_reuses_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sentinel-app-test-") as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "-q")
            generated_value = secrets.token_urlsafe(32)
            (repository / ".env").write_text(
                f"DATABASE_PASSWORD={generated_value}\n", encoding="utf-8"
            )
            self._git(repository, "add", ".env")
            self._git(
                repository,
                "-c", "user.name=Smoke",
                "-c", "user.email=smoke@example.invalid",
                "commit", "-qm", "Add generated fixture",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(_SRC)
            fingerprint_key = secrets.token_bytes(32)
            environment["SENTINEL_KEY_HMAC_KEY_B64"] = base64.b64encode(
                fingerprint_key
            ).decode("ascii")
            database = root / "scan.db"
            command = [
                sys.executable, "-m", "application", "scan",
                "--repository", str(repository),
                "--database", str(database),
            ]
            first = subprocess.run(
                command, capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("outcome=OK", first.stdout)
            self.assertIn("commits_new=1", first.stdout)
            self.assertIn("candidates=1", first.stdout)
            self.assertNotIn(generated_value, first.stdout + first.stderr)

            second = subprocess.run(
                command, capture_output=True, text=True, env=environment, check=False
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("commits_new=0", second.stdout)
            self.assertIn("candidates=0", second.stdout)

            sha = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            async def prime_interrupted_scan() -> int:
                async with App(database, fingerprint_key) as app:
                    stored = await app.storage.read(
                        lambda session: session.get_repository_by_location(
                            str(repository.resolve())
                        )
                    )
                    pending = await app.orchestrator.start_scan(
                        stored.id,
                        ScanTaskPayload(
                            mode=ScanMode.TARGETED,
                            target_shas=[sha],
                            ai_enabled=False,
                        ),
                    )
                    await app.orchestrator.plan_once()
                    return pending.id

            pending_id = asyncio.run(prime_interrupted_scan())
            resumed = subprocess.run(
                [*command, "--mode", "targeted", "--target-sha", sha],
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertIn(f"run_id={pending_id}", resumed.stdout)
            self.assertIn("commits_done=1", resumed.stdout)

    @staticmethod
    def _git(repository: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True
        )


if __name__ == "__main__":
    unittest.main()
