"""Sendblue direct-message iMessage/SMS adapter."""

import asyncio
import hmac
import json
import re
from collections import OrderedDict

import httpx
from aiohttp import web
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.message_session import MessageSesion

_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")


class SendblueAdapter(Platform):
    plugin_config: dict = {}

    def __init__(
        self, platform_config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ):
        super().__init__(platform_config, event_queue)
        self.config["unified_webhook_mode"] = False
        settings = self.plugin_config
        self.from_number = settings.get("sendblue_from_number", "")
        self.allow_from = settings.get("sendblue_allow_from", [])
        self.signing_secret = settings.get("sendblue_signing_secret", "")
        key = settings.get("sendblue_api_key", "")
        secret = settings.get("sendblue_api_secret", "")
        if not all(
            isinstance(value, str) and value
            for value in (self.from_number, self.signing_secret, key, secret)
        ):
            raise ValueError(
                "Sendblue requires API credentials, a signing secret and a sending line"
            )
        if not _PHONE.fullmatch(self.from_number):
            raise ValueError("Sendblue sending line must be an E.164 number")
        if (
            not isinstance(self.allow_from, list)
            or not self.allow_from
            or any(
                not isinstance(number, str)
                or (number != "*" and not _PHONE.fullmatch(number))
                for number in self.allow_from
            )
        ):
            raise ValueError(
                "Sendblue requires a nonempty E.164 sender allowlist (or explicit '*')"
            )
        self.host = platform_config.get("listen_host", "127.0.0.1")
        self.port = platform_config.get("listen_port", 6198)
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("Sendblue listener requires a host")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Sendblue listener port must be between 1 and 65535")
        self._runner: web.AppRunner | None = None
        self._inflight = 0
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stopped = asyncio.Event()
        self.client = httpx.AsyncClient(
            base_url="https://api.sendblue.com",
            headers={"sb-api-key-id": key, "sb-api-secret-key": secret},
            timeout=30,
            follow_redirects=False,
        )

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="sendblue",
            description="Sendblue iMessage and SMS",
            id=self.config.get("id", "sendblue"),
            support_streaming_message=False,
        )

    async def run(self) -> None:
        """Serve the plugin-owned webhook until the adapter is stopped."""
        app = web.Application(client_max_size=65536)
        app.router.add_post("/sendblue/webhook", self._handle_webhook)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=10)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, self.host, self.port).start()
            await self._stopped.wait()
        finally:
            await self._runner.cleanup()
            await self.client.aclose()

    async def terminate(self) -> None:
        """Release the listener and outgoing client on disable/reload/uninstall."""
        self._stopped.set()
        if self._runner is not None:
            await self._runner.cleanup()
        await self.client.aclose()

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """Bound concurrent and slow webhook requests before parsing content."""
        if self._stopped.is_set():
            return web.Response(text="stopped", status=503)
        if self._inflight >= 16:
            return web.Response(text="busy", status=503)
        self._inflight += 1
        try:
            text, status = await asyncio.wait_for(self.webhook_callback(request), 10)
            return web.Response(text=text, status=status)
        except asyncio.TimeoutError:
            return web.Response(text="request timed out", status=408)
        finally:
            self._inflight -= 1

    async def webhook_callback(self, request: web.Request) -> tuple[str, int]:
        """Authenticate and enqueue a bounded direct-message callback.

        Args:
            request: The aiohttp request supplied by the plugin listener.

        Returns:
            Plain HTTP acknowledgement and status. Accepted messages are held in
            AstrBot's process-local event queue, not a durable inbox.
        """
        if request.method != "POST":
            return "method not allowed", 405
        if not hmac.compare_digest(
            request.headers.get("sb-signing-secret", "").encode(),
            self.signing_secret.encode(),
        ):
            return "unauthorized", 401
        if request.content_length is not None and request.content_length > 65536:
            return "payload too large", 413
        raw = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            if len(raw) + len(chunk) > 65536:
                return "payload too large", 413
            raw.extend(chunk)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return "bad request", 400
        if not isinstance(payload, dict):
            return "bad request", 400
        if (
            payload.get("is_outbound") is not False
            or payload.get("status") != "RECEIVED"
            or payload.get("group_id") not in (None, "")
            or payload.get("to_number") != self.from_number
        ):
            return "ignored", 200
        sender, handle = payload.get("from_number"), payload.get("message_handle")
        content, media = payload.get("content"), payload.get("media_url")
        if (
            not isinstance(sender, str)
            or not _PHONE.fullmatch(sender)
            or not isinstance(handle, str)
            or not 0 < len(handle) <= 256
            or (content is not None and not isinstance(content, str))
            or (media is not None and not isinstance(media, str))
        ):
            return "bad request", 400
        if sender not in self.allow_from and "*" not in self.allow_from:
            return "ignored", 200
        if handle in self._seen:
            return "duplicate", 200
        text = content or ""
        if media:
            text += "\n[Attachment received; this channel supports text only. Please resend its contents as text.]"
        if not text.strip():
            return "ignored", 200
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.self_id = self.from_number
        message.session_id = sender
        message.sender = MessageMember(user_id=sender)
        message.message_id = handle
        message.message_str = text
        message.message = [Plain(text=text)]
        # Retain the correlation ID, not the callback's account metadata or media URL.
        message.raw_message = {"message_handle": handle}
        # The global queue may be unbounded; this adapter applies its own admission cap.
        if self._event_queue.qsize() >= 128:
            return "busy", 503
        try:
            self.commit_event(SendblueMessageEvent(message, self))
        except asyncio.QueueFull:
            return "busy", 503
        self._seen[handle] = None
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return "ok", 200

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        if session.message_type != MessageType.FRIEND_MESSAGE:
            raise ValueError("Sendblue supports direct messages only")
        await self.send_text(session.session_id, message_chain)
        await super().send_by_session(session, message_chain)

    async def send_text(self, recipient: str, message: MessageChain) -> None:
        """Deliver text once; require provider acceptance without retrying POSTs.

        Args:
            recipient: Authorized E.164 recipient number.
            message: Text-only message chain to deliver in 2000-character chunks.

        Raises:
            ValueError: The recipient or message type is unsupported.
            RuntimeError: Provider acceptance could not be confirmed. Earlier chunks
                may have been accepted; inspect Sendblue before manually retrying.
        """
        if not _PHONE.fullmatch(recipient) or (
            recipient not in self.allow_from and "*" not in self.allow_from
        ):
            raise ValueError("Sendblue recipient must be an allowed E.164 number")
        if any(not isinstance(part, Plain) for part in message.chain):
            raise ValueError(
                "Sendblue supports text only; media delivery is unavailable"
            )
        text = message.get_plain_text()
        for offset in range(0, len(text), 2000):
            try:
                response = await self.client.post(
                    "/api/send-message",
                    json={
                        "number": recipient,
                        "from_number": self.from_number,
                        "content": text[offset : offset + 2000],
                    },
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise RuntimeError(
                    "Sendblue delivery unconfirmed; inspect provider status before retrying"
                ) from exc
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("message_handle"), str)
                or not data["message_handle"]
                or data.get("error_code") not in (None, 0)
                or data.get("status") not in ("QUEUED", "SENT", "DELIVERED", "READ")
            ):
                raise RuntimeError(
                    "Sendblue did not confirm acceptance; inspect provider status before retrying"
                )


class SendblueMessageEvent(AstrMessageEvent):
    def __init__(self, message: AstrBotMessage, adapter: SendblueAdapter):
        super().__init__(
            message.message_str, message, adapter.meta(), message.session_id
        )
        self.adapter = adapter

    async def send(self, message: MessageChain) -> None:
        await self.adapter.send_text(self.get_sender_id(), message)
        await super().send(message)
