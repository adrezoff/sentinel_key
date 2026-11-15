"""Исключения слоя хранения."""


class StorageError(RuntimeError):
    """Базовая ошибка открытия, миграции или выполнения операции хранилища."""


class StorageNotInitializedError(StorageError):
    """Операция вызвана до ``SqliteStorage.initialize`` или после закрытия."""


class TaskLeaseLostError(StorageError):
    """Аренда задачи истекла либо задача больше не принадлежит worker'у."""


class ConcurrentUpdateError(StorageError):
    """Optimistic lock не позволил перезаписать более свежую версию записи."""
