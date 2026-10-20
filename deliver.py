"""Delivery transports and delivery failure signaling.

Production path: main wires a Discord-backed deliverer closure
(make_discord_deliverer) around discord.py's channel.send call, with
mention suppression enforced at the send site. The StubTransport exists
for offline process-level tests/smokes: it prints deliveries, can fail
after N attempts (CHECK_FOR_EBAY_DEALS_STUB_FAIL_AFTER), and can block
each send for N seconds (CHECK_FOR_EBAY_DEALS_STUB_BLOCK_SECONDS) so
shutdown tests can observe an in-flight send.
"""

import asyncio

import discord
from collections.abc import Awaitable, Callable
from discord.abc import Messageable

class DeliverFailed(Exception):
    """A single delivery attempt failed; the alert must stay pending."""


def make_discord_deliverer(
    channel: Messageable,
) -> Callable[[str], Awaitable[None]]:
    """Closure sending one message to `channel` with no mention side effects.

    allowed_mentions=AllowedMentions.none() prevents item titles from
    pinging users; the notify layer already escapes names, this is the
    transport-level backstop. HTTPException propagates to the caller,
    which keeps the alert pending (at-least-once).
    """

    async def deliver(message: str) -> None:
        _ = await channel.send(
            message, allowed_mentions=discord.AllowedMentions.none()
        )

    return deliver



class StubTransport:
    """Test/offline transport; prints messages, optional scripted failures."""

    def __init__(
        self, fail_after: int | None = None, block_seconds: float = 0.0
    ) -> None:
        self.fail_after: int | None = fail_after
        self.block_seconds: float = block_seconds
        self.attempts: int = 0

    async def deliver(self, message: str) -> None:
        self.attempts += 1
        if self.block_seconds > 0:
            await asyncio.sleep(self.block_seconds)
        if self.fail_after is not None and self.attempts > self.fail_after:
            raise DeliverFailed(f"stub transport failed at attempt {self.attempts}")
        print(f"STUB DELIVERY: {message}", flush=True)
