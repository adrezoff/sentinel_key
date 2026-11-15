"""Адаптер GitHub с общим безопасным HTTP(S)-транспортом."""

from .remote import RemoteGitCollector


class GitHubCollector(RemoteGitCollector):
    """GitHub clone URL с локальным зеркалом и ограничениями нагрузки."""
