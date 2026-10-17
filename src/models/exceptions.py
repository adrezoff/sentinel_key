"""Ошибки уровня моделей (без SQL и I/O)."""


class InvalidTransition(ValueError):
    """Переход статуса запрещён автоматом модели (``Task``, ``Incident`` и т.п.)."""

    def __init__(self, current: object, new: object, *, entity: str) -> None:
        self.current = current
        self.new = new
        self.entity = entity
        super().__init__(f"{entity}: {current!r} → {new!r} is not allowed")
