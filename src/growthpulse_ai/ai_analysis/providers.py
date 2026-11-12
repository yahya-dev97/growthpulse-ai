"""Providers that turn a prompt into a structured response: an offline mock and Anthropic.

The Anthropic adapter is the only code that holds the API key. It reads the key from a
``SecretStr`` at the moment it builds the client and never logs, stores or returns it.
SDK retries are disabled (``max_retries=0``), and errors become a ``ProviderError`` with a
fixed code and a safe message, never the SDK's own text. Each error also says whether the
request could have been billed, which decides how the ledger resolves its reservation.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import SecretStr

from .pricing import TokenUsage

Billable = Literal["no", "unknown"]


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    user: str
    output_schema: dict
    max_output_tokens: int
    effort: str
    timeout_seconds: float


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    stop_reason: str | None
    usage: TokenUsage | None
    price_modifiers: tuple[str, ...] = ()
    message_id: str | None = None


class ProviderError(RuntimeError):
    """A provider call failed. ``code`` is stable; ``billable`` drives the ledger."""

    def __init__(self, code: str, billable: Billable, message: str):
        super().__init__(message)
        self.code = code
        self.billable = billable


class InsightProvider(Protocol):
    name: str
    live: bool

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


@dataclass
class MockProvider:
    """Offline and free. Returns a fixed payload, the deterministic insights, or an error.

    ``live=True`` and ``usage`` exist only so tests can drive the live ledger path offline.
    """

    payload: Callable[[ProviderRequest], Any] | Any = None
    error: ProviderError | None = None
    stop_reason: str = "end_turn"
    model: str | None = None
    usage: TokenUsage | None = TokenUsage(input_tokens=0, output_tokens=0)
    price_modifiers: tuple[str, ...] = ()
    calls: list[ProviderRequest] = field(default_factory=list)
    name: str = "mock"
    live: bool = False

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        payload = self.payload(request) if callable(self.payload) else self.payload
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return ProviderResponse(
            text=text,
            model=self.model or request.model,
            stop_reason=self.stop_reason,
            usage=self.usage,
            price_modifiers=self.price_modifiers,
        )


def _status_error(code: int) -> ProviderError:
    if code == 429:
        return ProviderError("rate_limited", "no", "Anthropic API rate limit reached (429)")
    if code in (401, 403):
        return ProviderError("authentication", "no", f"Anthropic API rejected the key ({code})")
    if 400 <= code < 500:
        return ProviderError("bad_request", "no", f"Anthropic API rejected the request ({code})")
    return ProviderError("server_error", "unknown", f"Anthropic API server error ({code})")


def map_sdk_error(error: BaseException) -> ProviderError:
    """Translate an SDK exception without reading its message, which could carry data."""
    import anthropic

    if isinstance(error, anthropic.APITimeoutError):
        return ProviderError("timeout", "unknown", "Anthropic API request timed out")
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderError("connection", "unknown", "Anthropic API connection failed")
    if isinstance(error, anthropic.APIStatusError):
        return _status_error(int(error.status_code))
    return ProviderError("unexpected", "unknown", f"Anthropic call failed ({type(error).__name__})")


def _usage(raw: Any) -> tuple[TokenUsage | None, tuple[str, ...]]:
    if raw is None or getattr(raw, "input_tokens", None) is None:
        return None, ()
    breakdown = getattr(raw, "cache_creation", None)
    five = getattr(breakdown, "ephemeral_5m_input_tokens", None) if breakdown else None
    hour = getattr(breakdown, "ephemeral_1h_input_tokens", None) if breakdown else None
    total_writes = getattr(raw, "cache_creation_input_tokens", None) or 0
    if five is None and hour is None:
        # No TTL breakdown: price every cache write at the dearer 1-hour rate.
        five, hour = 0, total_writes
    usage = TokenUsage(
        input_tokens=int(raw.input_tokens),
        output_tokens=int(raw.output_tokens),
        cache_creation_5m_input_tokens=int(five or 0),
        cache_creation_1h_input_tokens=int(hour or 0),
        cache_read_input_tokens=int(getattr(raw, "cache_read_input_tokens", None) or 0),
    )
    modifiers = []
    if getattr(raw, "service_tier", None) not in (None, "standard"):
        modifiers.append(f"service_tier={raw.service_tier}")
    if getattr(raw, "speed", None) not in (None, "standard"):
        modifiers.append(f"speed={raw.speed}")
    if getattr(raw, "inference_geo", None) not in (None, "global", "not_available"):
        modifiers.append(f"inference_geo={raw.inference_geo}")
    return usage, tuple(modifiers)


class AnthropicProvider:
    """Live Claude calls through the official SDK. Constructed only after every gate passes.

    ``client_factory`` exists for offline tests: it receives the client options and
    returns an object with the SDK's ``messages.stream`` interface.
    """

    name = "anthropic"
    live = True

    def __init__(self, api_key: SecretStr, client_factory: Callable[..., Any] | None = None):
        if not isinstance(api_key, SecretStr) or not api_key.get_secret_value():
            raise ProviderError("missing_key", "no", "ANTHROPIC_API_KEY is not set")
        self._api_key = api_key
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return "AnthropicProvider(api_key=**********)"

    def _client(self, timeout: float) -> Any:
        options = {"max_retries": 0, "timeout": timeout}
        if self._client_factory is not None:
            return self._client_factory(api_key=self._api_key.get_secret_value(), **options)
        import anthropic

        return anthropic.Anthropic(api_key=self._api_key.get_secret_value(), **options)

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            client = self._client(request.timeout_seconds)
            with client.messages.stream(
                model=request.model,
                max_tokens=request.max_output_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
                output_config={
                    "effort": request.effort,
                    "format": {"type": "json_schema", "schema": request.output_schema},
                },
            ) as stream:
                message = stream.get_final_message()
        except ProviderError:
            raise
        except Exception as error:
            raise map_sdk_error(error) from None
        usage, modifiers = _usage(getattr(message, "usage", None))
        text = next(
            (block.text for block in message.content if getattr(block, "type", None) == "text"),
            "",
        )
        return ProviderResponse(
            text=text,
            model=str(getattr(message, "model", request.model)),
            stop_reason=getattr(message, "stop_reason", None),
            usage=usage,
            price_modifiers=modifiers,
            message_id=getattr(message, "id", None),
        )
