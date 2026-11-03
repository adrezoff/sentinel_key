# -*- coding: utf-8 -*-\n
"""LLM Client: протокол и реализации для AI-валидации.

Протокол ``LlmClient`` определяет интерфейс вызова LLM.
``FakeLlmClient`` — для тестов (возвращает заранее заданные вердикты).
``RealLlmClient`` — заглушка для будущей интеграции с NVIDIA Nemotron 3 Ultra.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Protocol

from models.ai_validation.ai_verdict import AiVerdict
from models.ai_validation.ai_validation_payload import AiValidationPayload
from models.candidate import Candidate
# ---------------------------------------------------------------------------
# Конфигурация LLM
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LlmConfig:
    """Конфигурация вызова LLM.

    Значения по умолчанию под MVP; переопределяются через env / конфиг позже.
    """

    # Эндпоинт API (OpenAI-совместимый). Для NVIDIA Nemotron будет задан на этапе 13.
    api_endpoint: str = "https://integrate.api.nvidia.com/v1/chat/completions"

    # Имя модели. Для NVIDIA Nemotron 3 Ultra: "nvidia/nemotron-3-ultra"
    model: str = "nvidia/nemotron-3-ultra"

    # API-ключ. Читается из env SENTINEL_KEY_LLM_API_KEY на этапе 13.
    api_key: str | None = None

    # Температура ~0 для детерминированных ответов
    temperature: float = 0.0

    # Максимальное количество токенов в ответе
    max_tokens: int = 512

    # Таймаут запроса (секунды)
    timeout_s: float = 30.0

    # Максимальное число попыток (включая первую)
    max_attempts: int = 3

    # База экспоненциального бэкоффа (секунды)
    retry_base_s: float = 1.0

    # Лимит параллельных вызовов (backpressure)
    max_concurrent: int = 5

    # Версия промпта (входит в idempotency_key задачи AI_VALIDATION)
    prompt_version: int = 1

    @classmethod
    def from_env(cls) -> "LlmConfig":
        """Создать конфиг из переменных окружения (для этапа 13)."""
        return cls(
            api_endpoint=os.getenv("SENTINEL_KEY_LLM_ENDPOINT", cls.api_endpoint),
            model=os.getenv("SENTINEL_KEY_LLM_MODEL", cls.model),
            api_key=os.getenv("SENTINEL_KEY_LLM_API_KEY"),
            temperature=float(os.getenv("SENTINEL_KEY_LLM_TEMPERATURE", str(cls.temperature))),
            max_tokens=int(os.getenv("SENTINEL_KEY_LLM_MAX_TOKENS", str(cls.max_tokens))),
            timeout_s=float(os.getenv("SENTINEL_KEY_LLM_TIMEOUT", str(cls.timeout_s))),
            max_attempts=int(os.getenv("SENTINEL_KEY_LLM_MAX_ATTEMPTS", str(cls.max_attempts))),
            retry_base_s=float(os.getenv("SENTINEL_KEY_LLM_RETRY_BASE", str(cls.retry_base_s))),
            max_concurrent=int(os.getenv("SENTINEL_KEY_LLM_MAX_CONCURRENT", str(cls.max_concurrent))),
            prompt_version=int(os.getenv("SENTINEL_KEY_LLM_PROMPT_VERSION", str(cls.prompt_version))),
        )
# ---------------------------------------------------------------------------
# Протокол LLM-клиента
# ---------------------------------------------------------------------------

class LlmClient(Protocol):
    """Интерфейс вызова LLM для AI-валидации.

    Реализации должны быть потокобезопасны и уважать семафор параллелизма.
    """

    @property
    def prompt_version(self) -> int:
        """Версия промпта (для идемпотентности)."""
        ...

    @property
    def max_concurrent(self) -> int:
        """Лимит параллельных вызовов."""
        ...

    async def complete(
        self,
        candidate: Candidate,
        payload: AiValidationPayload,
    ) -> AiVerdict:
        """Вызвать LLM и вернуть валидированный вердикт.

        Args:
            candidate: Кандидат с маскированными данными (value_masked, context_masked).
            payload: Параметры задачи (candidate_id, prompt_version, model override).

        Returns:
            AiVerdict с заполненными полями is_secret, secret_type, confidence,
            severity, reason, recommendation, model, prompt_version.

        Raises:
            LlmError: при ошибке сети, таймауте, невалидном ответе или превышении попыток.
        """
        ...


# ---------------------------------------------------------------------------
# Исключения LLM
# ---------------------------------------------------------------------------

class LlmError(Exception):
    """Базовое исключение LLM-клиента."""

    def __init__(self, message: str, *, retryable: bool = False, code: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class LlmTimeoutError(LlmError):
    """Превышен таймаут запроса."""

    def __init__(self, message: str = "LLM request timeout") -> None:
        super().__init__(message, retryable=True, code="LLM_TIMEOUT")


class LlmNetworkError(LlmError):
    """Сетевая ошибка при обращении к LLM."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, retryable=retryable, code="LLM_NETWORK_ERROR")


class LlmInvalidResponseError(LlmError):
    """Ответ LLM не прошел валидацию схемы."""

    def __init__(self, message: str = "LLM response validation failed") -> None:
        super().__init__(message, retryable=True, code="LLM_INVALID_RESPONSE")


class LlmRateLimitError(LlmError):
    """Превышен лимит запросов (429)."""

    def __init__(self, message: str = "LLM rate limit exceeded") -> None:
        super().__init__(message, retryable=True, code="LLM_RATE_LIMIT")


class LlmAuthError(LlmError):
    """Ошибка аутентификации (401/403)."""

    def __init__(self, message: str = "LLM authentication failed") -> None:
        super().__init__(message, retryable=False, code="LLM_AUTH_ERROR")
# ---------------------------------------------------------------------------
# Фейковый клиент для тестов
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FakeLlmClient:
    """Фейковый LLM-клиент для unit-тестов.

    Возвращает заранее заданные вердикты по candidate_id.
    Если вердикта нет — бросает LlmInvalidResponseError.
    """

    # candidate_id -> AiVerdict (без task_id, он проставится в AI Worker)
    _verdicts: dict[int, AiVerdict] = field(default_factory=dict)

    # Настройки для имитации задержек/ошибок
    delay_s: float = 0.0
    fail_next: bool = False
    fail_code: str = "LLM_NETWORK_ERROR"

    # Протокол
    prompt_version: int = 1
    max_concurrent: int = 5

    def set_verdict(self, candidate_id: int, verdict: AiVerdict) -> None:
        """Задать ожидаемый вердикт для кандидата."""
        self._verdicts[candidate_id] = verdict

    def clear(self) -> None:
        """Очистить все заданные вердикты."""
        self._verdicts.clear()

    async def complete(
        self,
        candidate: Candidate,
        payload: AiValidationPayload,
    ) -> AiVerdict:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)

        if self.fail_next:
            self.fail_next = False
            if self.fail_code == "LLM_TIMEOUT":
                raise LlmTimeoutError()
            elif self.fail_code == "LLM_RATE_LIMIT":
                raise LlmRateLimitError()
            elif self.fail_code == "LLM_AUTH_ERROR":
                raise LlmAuthError()
            else:
                raise LlmNetworkError("Simulated network error")

        verdict = self._verdicts.get(candidate.id)
        if verdict is None:
            raise LlmInvalidResponseError(f"No fake verdict configured for candidate {candidate.id}")

        # Вернуть копию с правильным prompt_version из payload
        return AiVerdict(
            task_id=verdict.task_id,
            candidate_id=verdict.candidate_id,
            is_secret=verdict.is_secret,
            secret_type=verdict.secret_type,
            confidence=verdict.confidence,
            severity=verdict.severity,
            reason=verdict.reason,
            recommendation=verdict.recommendation,
            model=verdict.model,
            prompt_version=payload.prompt_version,
            created_at=verdict.created_at,
        )
# ---------------------------------------------------------------------------
# Утилиты: построение промпта и парсинг ответа
# ---------------------------------------------------------------------------

# Промпт на английском, ответ ожидаем на русском
SYSTEM_PROMPT = """You are a security analyst specializing in detecting leaked credentials and secrets in source code.

Your task: Analyze the provided code snippet and determine if it contains a real secret/credential.

Respond ONLY with a valid JSON object in Russian language with the following fields:
- is_secret: boolean (true if this is a real credential/secret)
- secret_type: string (one of: UNKNOWN, AWS_ACCESS_KEY, AWS_SECRET_KEY, GITHUB_TOKEN, SLACK_TOKEN, JWT, PRIVATE_KEY, BEARER_TOKEN, DATABASE_URL, DATABASE_PASSWORD, API_KEY, GENERIC_PASSWORD, OTHER)
- confidence: float (0.0 to 1.0, how certain you are)
- severity: string (one of: LOW, MEDIUM, HIGH, CRITICAL)
- reason: string (brief explanation in Russian)
- recommendation: string (remediation advice in Russian)

Rules:
- Public examples, placeholders, test fixtures, UUIDs, hashes, timestamps are NOT secrets.
- Real API keys, tokens, passwords, private keys ARE secrets.
- High entropy alone is not enough; context matters.
- If unsure, set is_secret=false with low confidence."""

USER_PROMPT_TEMPLATE = """Analyze this code snippet for secrets:

File: {file_path}
Line: {line_number}
Secret type hint: {secret_type}
Masked value: {value_masked}
Context (masked):
{context_masked}
Regex rules matched: {regex_rules}
Entropy: {entropy:.2f}
Detector flags: {detector_flags}
Local score: {score_total:.2f}

Is this a real secret? Respond with JSON only."""


def build_prompt(candidate: Candidate) -> tuple[str, str]:
    """Построить system и user промпты для кандидата.

    Returns:
        (system_prompt, user_prompt)
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(
        file_path=candidate.file_path,
        line_number=candidate.line_number,
        secret_type=candidate.secret_type.name,
        value_masked=candidate.value_masked,
        context_masked=candidate.context_masked,
        regex_rules=json.dumps(candidate.regex_rules, ensure_ascii=False),
        entropy=candidate.entropy,
        detector_flags=str(candidate.detector_flags),
        score_total=candidate.score_total,
    )
    return SYSTEM_PROMPT, user_prompt
def parse_llm_response(response_text: str, candidate_id: int, task_id: int, model: str, prompt_version: int) -> AiVerdict:
    """Распарсить и провалидировать ответ LLM в AiVerdict.

    Args:
        response_text: Сырой текст ответа LLM.
        candidate_id: ID кандидата.
        task_id: ID задачи AI_VALIDATION.
        model: Имя использованной модели.
        prompt_version: Версия промпта.

    Returns:
        Валидированный AiVerdict.

    Raises:
        LlmInvalidResponseError: если JSON невалиден или поля не прошли проверку.
    """
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise LlmInvalidResponseError(f"Invalid JSON: {exc}") from exc

    # Валидация полей
    required_fields = ["is_secret", "secret_type", "confidence", "severity", "reason", "recommendation"]
    for field_name in required_fields:
        if field_name not in data:
            raise LlmInvalidResponseError(f"Missing required field: {field_name}")

    # is_secret
    is_secret = data["is_secret"]
    if not isinstance(is_secret, bool):
        raise LlmInvalidResponseError("is_secret must be boolean")

    # secret_type
    from models.enums import SecretType
    try:
        secret_type = SecretType[data["secret_type"]]
    except KeyError:
        raise LlmInvalidResponseError(f"Invalid secret_type: {data['secret_type']}")

    # confidence
    confidence = data["confidence"]
    if not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise LlmInvalidResponseError("confidence must be float in [0.0, 1.0]")

    # severity
    from models.enums import Severity
    try:
        severity = Severity[data["severity"]]
    except KeyError:
        raise LlmInvalidResponseError(f"Invalid severity: {data['severity']}")

    # reason, recommendation
    reason = data["reason"]
    recommendation = data["recommendation"]
    if not isinstance(reason, str) or not reason.strip():
        raise LlmInvalidResponseError("reason must be non-empty string")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise LlmInvalidResponseError("recommendation must be non-empty string")

    return AiVerdict(
        task_id=task_id,
        candidate_id=candidate_id,
        is_secret=is_secret,
        secret_type=secret_type,
        confidence=float(confidence),
        severity=severity,
        reason=reason.strip(),
        recommendation=recommendation.strip(),
        model=model,
        prompt_version=prompt_version,
    )


# ---------------------------------------------------------------------------
# Реальный HTTP-клиент для NVIDIA Nemotron 3 Ultra (OpenAI-совместимый API)
# ---------------------------------------------------------------------------

class RealLlmClient:
    """Реальный HTTP-клиент для NVIDIA Nemotron 3 Ultra (OpenAI-совместимый API).

    Поддерживает:
    - Semaphore для backpressure (лимит параллельных вызовов)
    - Таймаут через asyncio.wait_for
    - Retry с экспоненциальным backoff и jitter
    - Структурированные ошибки (LlmError и наследники)
    """

    def __init__(self, config: LlmConfig | None = None) -> None:
        self._config = config or LlmConfig()
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)

    async def complete(
        self,
        candidate: Candidate,
        payload: AiValidationPayload,
    ) -> AiVerdict:
        """Вызвать LLM с ретраями, таймаутом и семафором.

        Args:
            candidate: Кандидат с маскированными данными.
            payload: Параметры задачи (candidate_id, prompt_version, model override).

        Returns:
            Валидированный AiVerdict.

        Raises:
            LlmTimeoutError: превышен таймаут.
            LlmNetworkError: сетевая ошибка.
            LlmInvalidResponseError: невалидный ответ LLM.
            LlmAuthError: ошибка аутентификации (401/403).
            LlmRateLimitError: превышен rate limit (429).
        """
        if self._config.api_key is None:
            raise LlmAuthError("API key not configured. Set SENTINEL_KEY_LLM_API_KEY.")

        system_prompt, user_prompt = build_prompt(candidate)
        model = payload.model or self._config.model

        # Подготовка запроса к OpenAI-совместимому API
        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
        }
        data = json.dumps(request_body).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }

        last_exception: LlmError | None = None

        for attempt in range(1, self._config.max_attempts + 1):
            try:
                async with self._semaphore:
                    verdict = await self._call_with_timeout(data, headers, payload.prompt_version)
                    return verdict

            except LlmTimeoutError:
                last_exception = LlmTimeoutError(f"Attempt {attempt}/{self._config.max_attempts} timed out")
            except LlmRateLimitError:
                last_exception = LlmRateLimitError(f"Attempt {attempt}/{self._config.max_attempts} rate limited")
            except LlmAuthError:
                # Auth errors are not retryable
                raise
            except LlmNetworkError as e:
                last_exception = e
            except LlmInvalidResponseError as e:
                last_exception = e

            # Если это не последняя попытка — ждём с экспоненциальным backoff + jitter
            if attempt < self._config.max_attempts:
                wait_time = self._config.retry_base_s * (2 ** (attempt - 1))
                jitter = random.uniform(0, wait_time * 0.1)  # ±10% jitter
                await asyncio.sleep(wait_time + jitter)

        # Все попытки исчерпаны
        if last_exception:
            raise last_exception
        raise LlmNetworkError("All attempts failed without a specific error")

    async def _call_with_timeout(
        self,
        data: bytes,
        headers: dict[str, str],
        prompt_version: int,
    ) -> AiVerdict:
        """Выполнить HTTP-запрос с таймаутом и парсингом ответа."""
        request = urllib.request.Request(
            self._config.api_endpoint,
            data=data,
            headers=headers,
            method="POST",
        )

        try:
            # asyncio.wait_for для таймаута
            response = await asyncio.wait_for(
                asyncio.to_thread(self._do_request, request),
                timeout=self._config.timeout_s,
            )
        except asyncio.TimeoutError as e:
            raise LlmTimeoutError(f"Request timed out after {self._config.timeout_s}s") from e
        except urllib.error.HTTPError as e:
            self._handle_http_error(e)
        except urllib.error.URLError as e:
            raise LlmNetworkError(f"Network error: {e.reason}") from e

        # Парсинг ответа
        try:
            response_data = json.loads(response.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise LlmInvalidResponseError(f"Invalid JSON response: {e}") from e

        # Извлечение контента из OpenAI-совместимого ответа
        try:
            content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LlmInvalidResponseError(f"Unexpected response structure: {e}") from e

        # Парсинг и валидация вердикта
        return parse_llm_response(
            content,
            candidate_id=0,  # будет перезаписан в AI Worker
            task_id=0,       # будет перезаписан в AI Worker
            model=self._config.model,
            prompt_version=prompt_version,
        )

    def _do_request(self, request: urllib.request.Request) -> bytes:
        """Синхронный HTTP-запрос (выполняется в thread pool)."""
        with urllib.request.urlopen(request) as response:
            return response.read()

    @staticmethod
    def _handle_http_error(error: urllib.error.HTTPError) -> None:
        """Преобразовать HTTP-ошибку в LlmError."""
        if error.code == 401 or error.code == 403:
            raise LlmAuthError(f"Authentication failed: {error.code}")
        if error.code == 429:
            raise LlmRateLimitError(f"Rate limit exceeded: {error.code}")
        if 500 <= error.code < 600:
            raise LlmNetworkError(f"Server error: {error.code}")
        raise LlmNetworkError(f"HTTP error: {error.code}")

    @property
    def prompt_version(self) -> int:
        return self._config.prompt_version

    @property
    def max_concurrent(self) -> int:
        return self._config.max_concurrent