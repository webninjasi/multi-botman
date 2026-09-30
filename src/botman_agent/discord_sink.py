"""Backpressured Discord webhook delivery for the target log agent."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class DiscordSinkError(RuntimeError):
    pass


class FatalDiscordDestinationError(DiscordSinkError):
    pass


def webhook_thread_url(webhook_url: str, thread_id: str) -> str:
    split = urlsplit(webhook_url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query["thread_id"] = thread_id
    query["wait"] = "true"
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


class DiscordWebhookSink:
    def __init__(
        self,
        session,
        *,
        webhook_url: str,
        thread_id: str,
        retry_initial_sec: float = 1.0,
        retry_max_sec: float = 30.0,
        sleep=asyncio.sleep,
    ):
        self.session = session
        self.url = webhook_thread_url(webhook_url, thread_id)
        self.retry_initial_sec = retry_initial_sec
        self.retry_max_sec = retry_max_sec
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def send(self, content: str) -> None:
        async with self._lock:
            delay = self.retry_initial_sec
            while True:
                try:
                    response = await self.session.post(
                        self.url,
                        json={
                            "content": content,
                            "allowed_mentions": {"parse": []},
                        },
                    )
                except (asyncio.TimeoutError, OSError):
                    await self._sleep(delay)
                    delay = min(self.retry_max_sec, max(delay * 2, self.retry_initial_sec))
                    continue
                try:
                    status = int(response.status)
                    if 200 <= status < 300:
                        return
                    if status == 429:
                        retry_after = None
                        try:
                            body = await response.json()
                            retry_after = float(body.get("retry_after", 0))
                        except Exception:
                            retry_after = None
                        await self._sleep(retry_after if retry_after and retry_after > 0 else delay)
                        delay = min(self.retry_max_sec, max(delay * 2, self.retry_initial_sec))
                        continue
                    if status in {408, 425} or 500 <= status < 600:
                        await self._sleep(delay)
                        delay = min(self.retry_max_sec, max(delay * 2, self.retry_initial_sec))
                        continue
                    # Authentication, missing/deleted webhook/thread, permission
                    # failures, and malformed destinations must not hot-loop.
                    text = ""
                    try:
                        text = (await response.text())[:300]
                    except Exception:
                        pass
                    raise FatalDiscordDestinationError(
                        f"Discord webhook destination rejected delivery with HTTP {status}: {text}"
                    )
                finally:
                    release = getattr(response, "release", None)
                    if release is not None:
                        release()
