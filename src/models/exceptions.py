"""Model-level errors (no SQL, no I/O)."""


class InvalidTransition(ValueError):
    """Raised when a status change is not allowed by the model's automaton."""

    def __init__(self, current: object, new: object, *, entity: str) -> None:
        self.current = current
        self.new = new
        self.entity = entity
        super().__init__(f"{entity}: {current!r} → {new!r} is not allowed")
