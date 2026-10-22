"""Извлечение потенциальных секретов из добавленных строк Git diff."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from git_collector import DiffHunk, DiffLine, DiffLineKind, FileDiff
from models import RawCandidate, SecretType

from .config import PreprocessorConfig

_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*(?:=|:)\s*(?P<value>.+?)\s*$"
)
_BEARER_RE = re.compile(
    r"(?i)\b(?P<key>Authorization)\s*:\s*[\"']?Bearer\s+"
    r"(?P<value>[A-Za-z0-9._~+/=-]{8,})"
)
_URL_CREDENTIALS_RE = re.compile(
    r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://"
    r"(?P<user>[^\s/:@]+):(?P<password>[^\s/@]+)@"
)
_PEM_BEGIN_RE = re.compile(r"^\s*-----BEGIN (?P<label>.+ PRIVATE KEY)-----\s*$")


@dataclass(frozen=True, slots=True)
class _ExtractedValue:
    value: str
    key_name: str | None
    secret_type: SecretType
    start: int
    end: int


class Preprocessor:
    """Детерминированно преобразует diff в модели ``RawCandidate``.

    HMAC-ключ передаётся явно и никогда не попадает в выходные модели.
    """

    def __init__(
        self,
        fingerprint_key: bytes,
        config: PreprocessorConfig | None = None,
    ) -> None:
        """Создать предобработчик с непустым HMAC-ключом."""
        if not isinstance(fingerprint_key, bytes) or not fingerprint_key:
            raise ValueError("fingerprint_key must be non-empty bytes")
        self._fingerprint_key = fingerprint_key
        self._config = config if config is not None else PreprocessorConfig()
        self._bare_token_re = re.compile(
            rf"(?<![A-Za-z0-9_+/=.-])[A-Za-z0-9_+/=.-]"
            rf"{{{self._config.min_bare_token_len},}}(?![A-Za-z0-9_+/=.-])"
        )

    def process(
        self,
        file_diff: FileDiff,
        *,
        repository_id: int,
        commit_id: int,
    ) -> Iterator[RawCandidate]:
        """Выдать уникальные кандидаты из добавленных строк одного файла."""
        if self._should_skip(file_diff):
            return

        seen: set[tuple[str, int, bytes]] = set()
        for hunk in file_diff.hunks:
            for candidate in self._process_hunk(
                hunk,
                file_path=file_diff.path,
                repository_id=repository_id,
                commit_id=commit_id,
            ):
                dedup_key = (
                    candidate.file_path,
                    candidate.line_number,
                    candidate.value_fingerprint,
                )
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                yield candidate

    def fingerprint(self, raw_value: str) -> bytes:
        """Вычислить 32-байтный HMAC-SHA256 по точному значению."""
        return hmac.new(
            self._fingerprint_key,
            raw_value.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    def mask(self, raw_value: str) -> str:
        """Замаскировать значение, не раскрывая короткие секреты целиком."""
        visible = self._config.mask_visible_chars
        if not raw_value:
            return "***"
        if visible == 0 or len(raw_value) <= visible * 2:
            return "*" * min(len(raw_value), 8)
        return f"{raw_value[:visible]}...{raw_value[-visible:]}"

    def _should_skip(self, file_diff: FileDiff) -> bool:
        if file_diff.skipped or file_diff.is_binary:
            return True
        normalized = file_diff.path.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part)
        if any(part in self._config.ignored_path_parts for part in parts[:-1]):
            return True
        file_name = parts[-1] if parts else normalized
        if file_name in self._config.ignored_file_names:
            return True
        return file_name.endswith(self._config.ignored_suffixes)

    def _process_hunk(
        self,
        hunk: DiffHunk,
        *,
        file_path: str,
        repository_id: int,
        commit_id: int,
    ) -> Iterator[RawCandidate]:
        pem_ranges = self._pem_ranges(hunk.lines)
        pem_line_indexes = {
            index for start, end, _label in pem_ranges for index in range(start, end + 1)
        }
        extracted_by_line = {
            index: tuple(self._extract(line.text))
            for index, line in enumerate(hunk.lines)
            if line.kind is not DiffLineKind.DELETE
            and index not in pem_line_indexes
            and len(line.text) <= self._config.max_line_len
        }
        masked_lines = [line.text for line in hunk.lines]
        for index, extracted_values in extracted_by_line.items():
            masked_lines[index] = self._mask_extracted_values(
                hunk.lines[index].text,
                extracted_values,
            )

        for start, end, label in pem_ranges:
            lines = hunk.lines[start : end + 1]
            if any(len(line.text) > self._config.max_line_len for line in lines):
                continue
            raw_value = "\n".join(line.text for line in lines)
            value_masked = self.mask(raw_value)
            for index in range(start, end + 1):
                masked_lines[index] = value_masked
            source = lines[0]
            if source.new_lineno is None:
                continue
            yield self._build_candidate(
                raw_value=raw_value,
                key_name=label,
                secret_type=SecretType.PRIVATE_KEY,
                source=source,
                line_index=start,
                lines=hunk.lines,
                file_path=file_path,
                repository_id=repository_id,
                commit_id=commit_id,
                masked_lines=masked_lines,
            )

        for index, line in enumerate(hunk.lines):
            if index in pem_line_indexes:
                continue
            if line.kind is not DiffLineKind.ADD or line.new_lineno is None:
                continue
            if len(line.text) > self._config.max_line_len:
                continue
            for extracted in extracted_by_line.get(index, ()):
                yield self._build_candidate(
                    raw_value=extracted.value,
                    key_name=extracted.key_name,
                    secret_type=extracted.secret_type,
                    source=line,
                    line_index=index,
                    lines=hunk.lines,
                    file_path=file_path,
                    repository_id=repository_id,
                    commit_id=commit_id,
                    masked_lines=masked_lines,
                )

    def _build_candidate(
        self,
        *,
        raw_value: str,
        key_name: str | None,
        secret_type: SecretType,
        source: DiffLine,
        line_index: int,
        lines: Sequence[DiffLine],
        file_path: str,
        repository_id: int,
        commit_id: int,
        masked_lines: Sequence[str],
    ) -> RawCandidate:
        value_masked = self.mask(raw_value)
        context_indexes = self._context_indexes(lines, line_index)
        context_raw = [lines[index].text for index in context_indexes]
        return RawCandidate(
            repository_id=repository_id,
            commit_id=commit_id,
            file_path=file_path,
            line_number=source.new_lineno if source.new_lineno is not None else 0,
            key_name=key_name,
            raw_value=raw_value,
            source_line_raw=source.text,
            context_raw=context_raw,
            source_line_masked=masked_lines[line_index],
            context_masked="\n".join(masked_lines[index] for index in context_indexes),
            secret_type_hint=secret_type,
            value_fingerprint=self.fingerprint(raw_value),
            value_masked=value_masked,
        )

    def _extract(self, text: str) -> Iterator[_ExtractedValue]:
        occupied: list[tuple[int, int]] = []

        for match in _BEARER_RE.finditer(text):
            yield _ExtractedValue(
                value=match.group("value"),
                key_name=match.group("key"),
                secret_type=SecretType.BEARER_TOKEN,
                start=match.start("value"),
                end=match.end("value"),
            )
            occupied.append(match.span())

        for match in _URL_CREDENTIALS_RE.finditer(text):
            yield _ExtractedValue(
                value=match.group("password"),
                key_name="password",
                secret_type=SecretType.DATABASE_URL,
                start=match.start("password"),
                end=match.end("password"),
            )
            occupied.append(match.span())

        assignment = _ASSIGNMENT_RE.match(text)
        if assignment is not None and not occupied:
            value, start, end = self._assignment_value(assignment)
            if value:
                yield _ExtractedValue(
                    value=value,
                    key_name=assignment.group("key"),
                    secret_type=SecretType.UNKNOWN,
                    start=start,
                    end=end,
                )
                occupied.append((start, end))

        for match in self._bare_token_re.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            yield _ExtractedValue(
                value=match.group(0),
                key_name=None,
                secret_type=SecretType.UNKNOWN,
                start=match.start(),
                end=match.end(),
            )

    @staticmethod
    def _assignment_value(match: re.Match[str]) -> tuple[str, int, int]:
        value = match.group("value").strip()
        raw_match = match.group("value")
        start = match.start("value") + len(raw_match) - len(raw_match.lstrip())
        value = value.removesuffix(";").removesuffix(",").rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
            start += 1
        return value, start, start + len(value)

    def _context_indexes(
        self,
        lines: Sequence[DiffLine],
        source_index: int,
    ) -> list[int]:
        result: list[int] = []
        lower = max(0, source_index - self._config.context_lines)
        upper = min(len(lines), source_index + self._config.context_lines + 1)
        for index in range(lower, upper):
            if index == source_index or lines[index].kind is DiffLineKind.DELETE:
                continue
            result.append(index)
        return result

    def _mask_extracted_values(
        self,
        text: str,
        extracted_values: Sequence[_ExtractedValue],
    ) -> str:
        masked = text
        unique_values = {extracted.value for extracted in extracted_values}
        for raw_value in sorted(unique_values, key=len, reverse=True):
            masked = masked.replace(raw_value, self.mask(raw_value))
        return masked

    @staticmethod
    def _pem_ranges(lines: Sequence[DiffLine]) -> list[tuple[int, int, str]]:
        ranges: list[tuple[int, int, str]] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            begin = _PEM_BEGIN_RE.match(line.text)
            if line.kind is not DiffLineKind.ADD or begin is None:
                index += 1
                continue
            label = begin.group("label")
            end_marker = f"-----END {label}-----"
            end = index + 1
            while end < len(lines):
                current = lines[end]
                if current.kind is not DiffLineKind.ADD:
                    break
                if current.text.strip() == end_marker:
                    ranges.append((index, end, label))
                    index = end
                    break
                end += 1
            index += 1
        return ranges
