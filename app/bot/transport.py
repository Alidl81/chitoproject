from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from app.config import settings

logging.getLogger("httpx").setLevel(logging.WARNING)


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


class Transport(Protocol):
    platform: str

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...
    async def send_message(self, chat_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> str: ...
    async def edit_message_text(self, chat_id: str, message_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None: ...
    async def delete_message(self, chat_id: str, message_id: str) -> bool: ...
    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    async def download_to(self, file_id: str, destination_dir: Path, max_bytes: int) -> tuple[Path, str, str, int]: ...
    async def close(self) -> None: ...


@dataclass
class BotAdapter:
    platform: str
    token: str
    api_base: str
    file_base: str
    http: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.bot_api_timeout, connect=8.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    def _keyboard(self, keyboard: list[list[dict[str, str]]] | None) -> list[list[dict[str, str]]] | None:
        # Telegram and Bale both document `style` for InlineKeyboardButton, with
        # the same enum values and platform-specific color rendering.
        return keyboard

    async def _api(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.http.post(f"{self.api_base}/bot{self.token}/{method}", json=payload or {})
            response.raise_for_status()
            body = response.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"{self.platform} transport unavailable") from exc
        except httpx.HTTPStatusError as exc:
            retry_after = None
            try:
                retry_after = int(exc.response.headers.get("Retry-After", "")) or None
            except ValueError:
                pass
            raise ProviderError(f"{self.platform} HTTP {exc.response.status_code}", retry_after=retry_after,
                                permanent=400 <= exc.response.status_code < 500 and exc.response.status_code != 429) from exc
        if not isinstance(body, dict) or not body.get("ok"):
            error = body if isinstance(body, dict) else {}
            parameters = error.get("parameters") or {}
            description = str(error.get("description", "provider request failed"))[:100]
            raise ProviderError(
                f"{self.platform} API {error.get('error_code', 'error')}: {description}",
                retry_after=parameters.get("retry_after"),
                permanent=error.get("error_code") in {400, 401, 403},
            )
        return body.get("result")

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": max(1, min(timeout, 50)),
            "allowed_updates": ["message", "edited_message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._api("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> str:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": self._keyboard(keyboard)}
        result = await self._api("sendMessage", payload)
        return str(result["message_id"])

    async def edit_message_text(self, chat_id: str, message_id: str, text: str, keyboard: list[list[dict[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": self._keyboard(keyboard)}
        try:
            await self._api("editMessageText", payload)
        except ProviderError as exc:
            if "message is not modified" in str(exc).casefold():
                return
            raise

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        return bool(await self._api("deleteMessage", {"chat_id": chat_id, "message_id": message_id}))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    async def download_to(self, file_id: str, destination_dir: Path, max_bytes: int) -> tuple[Path, str, str, int]:
        info = await self._api("getFile", {"file_id": file_id})
        file_path = info.get("file_path") if isinstance(info, dict) else None
        if not isinstance(file_path, str) or not file_path or file_path.startswith("/") or ".." in Path(file_path).parts:
            raise ProviderError("provider returned an invalid file path", permanent=True)
        destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = destination_dir / uuid4().hex
        total = 0
        try:
            async with self.http.stream("GET", f"{self.file_base}{self.token}/{file_path}") as response:
                response.raise_for_status()
                raw_length = response.headers.get("Content-Length")
                if raw_length and int(raw_length) > max_bytes:
                    raise ProviderError("file exceeds configured upload limit", permanent=True)
                with target.open("xb") as out:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProviderError("file exceeds configured upload limit", permanent=True)
                        await asyncio.to_thread(out.write, chunk)
                    await asyncio.to_thread(out.flush)
            target.chmod(0o600)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        filename = Path(file_path).name[:180] or "attachment"
        mime = "application/octet-stream"
        return target, filename, mime, total

    async def close(self) -> None:
        await self.http.aclose()


class TelegramAdapter(BotAdapter):
    def __init__(self, token: str):
        super().__init__("telegram", token, "https://api.telegram.org", "https://api.telegram.org/file/bot")


class BaleAdapter(BotAdapter):
    def __init__(self, token: str):
        super().__init__("bale", token, "https://tapi.bale.ai", "https://tapi.bale.ai/file/bot")


def configured_adapters() -> dict[str, BotAdapter]:
    adapters: dict[str, BotAdapter] = {}
    if settings.telegram_bot_token:
        adapters["telegram"] = TelegramAdapter(settings.telegram_bot_token.get_secret_value())
    if settings.bale_bot_token:
        adapters["bale"] = BaleAdapter(settings.bale_bot_token.get_secret_value())
    return adapters
