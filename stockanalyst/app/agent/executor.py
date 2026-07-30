"""A2A executor — the seller agent's outward A2A surface (two fixed-code skills).

The agent serves A2A directly via AgentCore (``serve_a2a`` wraps the official
``a2a-sdk``). This module is ONLY the a2a wire: :class:`SellerAgentExecutor`
inherits all of the seller logic + background-delivery machinery from
:class:`seller_core.SellerCore` (which imports nothing from ``a2a``) and adds the
a2a-specific :meth:`~SellerAgentExecutor.execute` / :meth:`~SellerAgentExecutor.cancel`
entrypoints plus the request/response wire helpers. :meth:`execute` reads the
inbound message's data part and dispatches on its ``skill``:

    negotiate     → ``SellerCore.negotiate`` (rule-based price clamp + EIP-191 sign)
    notify_funded → ``SellerCore.notify_funded`` (fast on-chain gate) → ACK at
                    once, then in the BACKGROUND: LLM work → ``signing.submit_result``

``notify_funded`` is the buyer's "I funded job X — please deliver" notification.
Because the work takes time, the executor does NOT block the caller: the core
verifies the funded job synchronously (a couple of eth_calls) to ACK
accepted/rejected, then runs the slow LLM work + on-chain ``submit`` in a
background asyncio task and replies immediately. The buyer reads the deliverable
back from the CHAIN (SUBMITTED / ``get_deliverable_url``) — the chain is the
source of truth. While any background delivery is in flight ``is_busy`` (from
``SellerCore``) reports busy, which ``main.py`` feeds to AgentCore's ``/ping`` as
``HEALTHY_BUSY`` so the scale-to-zero runtime stays warm until the work lands
(within the session max-lifetime).

ALL signing is FIXED code in ``signing.py`` — NEVER an LLM-callable tool (money
is never in the LLM; the LLM only produces the work text, via the ``run_work``
hook). See ``seller_core.py`` for the negotiate / notify_funded / sweep logic.

You own this file — specialise the work hook / dispatch in ``seller_core.py``, but
keep signing OUT of the LLM tool list.
"""
from __future__ import annotations

import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import DataPart, InternalError, Part
from a2a.utils import get_data_parts, new_agent_parts_message
from a2a.utils.errors import ServerError

from seller_core import SellerCore
from x402_envelope import _validate_public_base_url, dispatch_x402_envelope

logger = logging.getLogger("seller-agent.a2a")


class SellerAgentExecutor(SellerCore, AgentExecutor):
    """ERC-8183 seller A2A executor: the a2a wire over :class:`SellerCore`.

    All seller logic (negotiate, notify_funded, background delivery, ``is_busy``,
    the ``__init__`` bookkeeping, the ``run_work`` hook) lives in
    :class:`seller_core.SellerCore`; this class adds only the A2A entrypoints and
    request/response wire helpers.

    The agent exposes ONLY the two paid, structured skills — there is no
    free-form chat skill. A plain text message (no ``{"skill": ...}`` DataPart)
    is rejected: negotiate / notify_funded always need a structured DataPart, so
    prose never triggers an LLM call or a paid action.
    """

    def __init__(
        self,
        *args,
        x402_app=None,
        x402_public_base_url: str = "",
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._x402_app = x402_app
        self._x402_public_base_url = (
            _validate_public_base_url(x402_public_base_url)
            if x402_public_base_url
            else ""
        )

    # -- A2A entrypoints -------------------------------------------------------
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        data = self._inbound(context)
        skill = data.get("skill")
        try:
            result = await self.dispatch_skill(data)
        except Exception as e:
            # A genuine internal fault is surfaced as a JSON-RPC error, NOT masked
            # as a successful result. A *plain* exception would hang/500 the caller
            # (a2a-sdk's jsonrpc handler catches only ServerError; a bare exception
            # escapes to Starlette as a 500). ServerError(InternalError()) IS caught
            # and serialized to a proper -32603 carrying the request id — the event
            # consumer's 0.5s dequeue timeout re-raises it, so there is no hang.
            # CLASSIFIED business outcomes are returned as a result above (peer of
            # the MCP runtime: faults → isError, business outcomes → result).
            logger.exception("skill %r failed", skill)
            raise ServerError(
                error=InternalError(message=f"{type(e).__name__}: {e}")
            ) from e
        await self._reply(event_queue, context, result)

    async def dispatch_skill(self, data: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a structured A2A data part without advertising internal routes.

        The envelope bridge is intentionally internal and omitted from `_skills`.
        """
        skill = data.get("skill")
        if skill == "x402_http_envelope":
            if self._x402_app is None or not self._x402_public_base_url:
                raise RuntimeError("x402 envelope bridge is not configured")
            return await dispatch_x402_envelope(
                self._x402_app,
                data.get("envelope"),
                expected_public_base_url=self._x402_public_base_url,
            )
        if skill == "negotiate":
            return await self.negotiate(data)
        if skill == "notify_funded":
            return await self.notify_funded(data)
        return self._unknown_skill(skill)

    def _unknown_skill(self, skill: Any) -> dict[str, Any]:
        # Includes a plain text message (no DataPart → skill is None): the seller
        # has no free-form skill, so prose is rejected here.
        result = {
            "error": f"unknown skill: {skill!r}",
            "skills": self._skills(),
        }
        if skill is None:
            # Most common cause: the caller put the JSON envelope in a text part.
            result["hint"] = (
                'send the skill envelope as an A2A data part: '
                'parts:[{"kind":"data","data":{"skill":"negotiate",...}}]'
            )
        return result

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        # negotiate is synchronous; notify_funded acks then delivers on-chain in
        # the background — once submitted it is anchored on-chain and cannot be
        # cancelled via A2A. Nothing to cancel here.
        await self._reply(event_queue, context, {"error": "cancel is not supported"})

    # -- wire helpers ----------------------------------------------------------
    @staticmethod
    def _inbound(context: RequestContext) -> dict[str, Any]:
        parts = context.message.parts if context.message else []
        data_parts = get_data_parts(parts) if parts else []
        return data_parts[0] if data_parts else {}

    @staticmethod
    async def _reply(event_queue: EventQueue, context: RequestContext, data: dict[str, Any]) -> None:
        # a2a-sdk's EventQueue.enqueue_event is a coroutine — must be awaited, or
        # the reply is never enqueued (the queue closes and the caller 500s).
        await event_queue.enqueue_event(
            new_agent_parts_message(
                [Part(root=DataPart(data=data))],
                context_id=context.context_id,
                task_id=context.task_id,
            )
        )
