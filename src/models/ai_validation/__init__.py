"""Модели AI-ветки: AiVerdict, AiValidationPayload, AiValidationOutput.

Соответствуют ``docs/models.md`` §3.8. Сырой секрет не хранится —
только ``verdict`` (``is_secret`` + ``secret_type``) и ссылки на кандидат
через ``candidate_id`` (マスクрованные данные в ``Candidate``).
"""

from .ai_verdict import AiVerdict
from .ai_validation_output import AiValidationOutput
from .ai_validation_payload import AiValidationPayload

__all__ = ["AiVerdict", "AiValidationPayload", "AiValidationOutput"]
