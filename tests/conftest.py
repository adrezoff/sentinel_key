"""Общие настройки pytest и изоляция тестового окружения."""

import sys
from pathlib import Path

# Пути нужны вспомогательным модулям integration-тестов.
src_path = Path(__file__).parent.parent / "src"
tests_path = Path(__file__).parent

if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))
if str(tests_path) not in sys.path:
    sys.path.insert(0, str(tests_path))

import os
import pytest


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch, tmp_path):
    """Не наследовать подписи, hooks и фильтры пользовательского Git в тестах."""
    for name in list(os.environ):
        if name.startswith("GIT_CONFIG"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "false")
