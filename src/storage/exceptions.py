"""Исключения слоя хранения."""


class StorageError(RuntimeError):
    """Базовая ошибка открытия, миграции или выполнения операции хранилища."""


class StorageNotInitializedError(StorageError):
    """Операция вызвана до ``SqliteStorage.initialize`` или после закрытия."""


class ConcurrentUpdateError(StorageError):
    """Optimistic lock не позволил перезаписать более свежую версию записи."""
