"""Адаптер GitLab с общим безопасным HTTP(S)-транспортом."""

from .remote import RemoteGitCollector


class GitLabCollector(RemoteGitCollector):
    """GitLab clone URL с локальным зеркалом и ограничениями нагрузки."""
