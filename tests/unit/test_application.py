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
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from application import App
from ai_validation.llm_client import FakeLlmClient
from models import ScanMode, ScanTaskPayload
from models.ai_validation.ai_verdict import AiVerdict
from models.enums import SecretType, Severity


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

    def test_ai_enabled_loads_llm_config_from_environment(self) -> None:
        """LLM-клиент и фабрика используют конфигурацию из окружения."""
        environment = {
            "SENTINEL_KEY_LLM_ENDPOINT": "https://llm.example/v1",
            "SENTINEL_KEY_LLM_MODEL": "configured-model",
            "SENTINEL_KEY_LLM_API_KEY": "configured-key",
            "SENTINEL_KEY_LLM_PROMPT_VERSION": "7",
        }
        with tempfile.TemporaryDirectory(prefix="sentinel-app-config-") as directory:
            with patch.dict(os.environ, environment, clear=False):
                app = App(
                    Path(directory) / "scan.db",
                    secrets.token_bytes(32),
                    ai_enabled=True,
                )

            worker_config = app.ai_worker.llm_client._config
            factory_config = app.commit_worker.ai_task_factory._llm_config
            self.assertEqual(worker_config.api_endpoint, "https://llm.example/v1")
            self.assertEqual(worker_config.model, "configured-model")
            self.assertEqual(worker_config.api_key, "configured-key")
            self.assertEqual(worker_config.prompt_version, 7)
            self.assertIs(worker_config, factory_config)

    def test_cli_scans_with_ai_enabled(self) -> None:
        """CLI scan with --ai-enabled using fake LLM for testing."""
        with tempfile.TemporaryDirectory(prefix="sentinel-app-ai-test-") as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "-q")
            (repository / "config.py").write_text(
                "aws_key = 'AKIAIOSFODNN7EXAMPLE'\n", encoding="utf-8"
            )
            self._git(repository, "add", "config.py")
            self._git(
                repository,
                "-c", "user.name=Smoke",
                "-c", "user.email=smoke@example.invalid",
                "commit", "-qm", "Add AWS key fixture",
            )
            fingerprint_key = secrets.token_bytes(32)
            database = root / "scan.db"

            # Create fake client with default verdict (not a secret)
            fake_client = FakeLlmClient()
            fake_client.set_default_verdict(AiVerdict(
                task_id=0,
                candidate_id=0,
                is_secret=False,
                secret_type=SecretType.OTHER,
                confidence=0.9,
                severity=Severity.LOW,
                reason="Test verdict: known placeholder value",
                recommendation="No action needed",
                model="fake-model",
                prompt_version=1,
            ))

            async def run_scan():
                async with App(
                    database,
                    fingerprint_key,
                    ai_enabled=True,
                    use_fake_llm=True,
                ) as app:
                    # Replace the AI worker's LLM client with our configured one
                    if app.ai_worker:
                        app.ai_worker.llm_client = fake_client
                    payload = ScanTaskPayload(
                        mode=ScanMode.INCREMENTAL,
                        batch_size=50,
                        refs=["--all"],
                        target_shas=[],
                        ai_enabled=True,
                    )
                    return await app.scan(repository, payload)

            result = asyncio.run(run_scan())
            self.assertEqual(result.outcome.name, "OK")
            self.assertNotIn("AKIAIOSFODNN7EXAMPLE", str(result))

    @staticmethod
    def _git(repository: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *args], check=True, capture_output=True
        )


if __name__ == "__main__":
    unittest.main()
