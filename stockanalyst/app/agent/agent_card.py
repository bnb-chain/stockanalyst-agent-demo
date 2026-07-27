"""A2A AgentCard — the seller agent's outward, discoverable identity.

Built by ``main.py`` and served by AgentCore's ``serve_a2a`` at
``/.well-known/agent-card.json``. ``serve_a2a`` OVERWRITES ``card.url`` at runtime
with the deployed AgentCore runtime URL (``$AGENTCORE_RUNTIME_URL``), so the
``url`` here is only a local-dev placeholder.

The card advertises exactly two skills — ``negotiate`` and ``notify_funded`` —
and the OAuth2 (Cognito) security scheme buyers must satisfy: AgentCore A2A endpoints
require an inbound OAuth2 bearer (there is no anonymous mode). The token URL +
scope come from the Cognito user pool ``bag deploy provision-cognito`` creates
(env ``OAUTH_TOKEN_URL`` / ``OAUTH_SCOPE``, injected at deploy); the runtime's
inbound JWT authorizer validates the same pool. Locally (no Cognito env) the
card omits the scheme so ``bag dev`` is reachable without a token.

You own this file — edit the skill descriptions / card metadata for your seller.
"""
from __future__ import annotations

import os

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    ClientCredentialsOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityScheme,
)

_NEGOTIATE = AgentSkill(
    id="negotiate",
    name="Negotiate an ERC-8183 job",
    description=(
        'Send a data part {"skill": "negotiate", "task_description": "...", '
        '"terms": {"deliverables": "...", "quality_standards": "...", '
        '"success_criteria": "uomp_notify_context_required_v1"}} (all three terms '
        "keys are REQUIRED) and receive a wallet-signed price quote. The exact "
        "success_criteria value must be copied from the signed response into the "
        "on-chain job description before funding. "
        "Anchor the returned envelope on-chain via createJob + fund, then use the "
        "buyer client's notifyFunded helper with the job-creation wallet. It sends "
        "the job_id with an EIP-712 authorization for the exact delivery context."
    ),
    tags=["erc8183", "negotiation", "bnb-chain"],
    input_modes=["application/json"],
    output_modes=["application/json"],
)

_NOTIFY_FUNDED = AgentSkill(
    id="notify_funded",
    name="Notify the seller a job is funded (request delivery)",
    description=(
        "After you fund a named job on-chain, use the buyer client's "
        "notifyFunded helper to send its decimal job_id plus an authorization "
        "envelope. The EIP-712 authorization signs the exact context string "
        "(delivery gateway, token, portfolio, and risk profile) and must recover "
        "to the on-chain job client; the seller-owned chain and contract domain "
        "values are never supplied by the request. A bare {\"skill\": "
        '"notify_funded"} call requests only a context-free funded-job sweep and '
        "returns status and note without job_id. A signed named call returns status, "
        "reason, and job_id as applicable after the seller verifies the funded job "
        "carries its signed quote; delivery then "
        "runs in the background (work takes time). Do NOT wait on this call for "
        "the result — read the deliverable back from the CHAIN once the job "
        "reaches SUBMITTED (the `submit` tx carries the deliverable_url; "
        "ERC-8183 `get_deliverable_url`). The agent serves no job-query endpoint."
    ),
    tags=["erc8183", "delivery", "bnb-chain"],
    input_modes=["application/json"],
    output_modes=["application/json"],
)

def _agent_name() -> str:
    """Card name from studio.toml ``[project].name`` (best-effort)."""
    try:
        from bnbagent_studio_core import config

        name = str(((config.load_studio_toml() or {}).get("project") or {}).get("name") or "")
    except Exception:
        name = ""
    return name or "bnbagent-seller"


def _oauth2_scheme() -> SecurityScheme | None:
    """OAuth2 (Cognito client-credentials) scheme from env, or None locally.

    ``bag deploy provision-cognito`` emits a Cognito user pool + app client and
    injects ``OAUTH_TOKEN_URL`` + ``OAUTH_SCOPE``; the AgentCore runtime's inbound
    JWT authorizer is wired to the same pool. Absent (local ``bag dev``) → return
    None so the card advertises no auth requirement.
    """
    token_url = os.environ.get("OAUTH_TOKEN_URL")
    scope = os.environ.get("OAUTH_SCOPE")
    if not token_url or not scope:
        return None
    return SecurityScheme(
        root=OAuth2SecurityScheme(
            flows=OAuthFlows(
                client_credentials=ClientCredentialsOAuthFlow(
                    token_url=token_url,
                    scopes={scope: "Invoke the seller agent"},
                )
            )
        )
    )


def build_agent_card() -> AgentCard:
    """Build the A2A AgentCard advertising the two seller skills (+ OAuth2 if set)."""
    name = _agent_name()
    extra: dict = {}
    scheme = _oauth2_scheme()
    if scheme is not None:
        scope = os.environ["OAUTH_SCOPE"]
        extra["security_schemes"] = {"oauth2": scheme}
        extra["security"] = [{"oauth2": [scope]}]
    skills = [_NEGOTIATE, _NOTIFY_FUNDED]
    return AgentCard(
        name=name,
        description=f"ERC-8183 seller agent ({name}) — negotiate + notify_funded over A2A.",
        # serve_a2a overwrites this with $AGENTCORE_RUNTIME_URL at runtime.
        # Local-dev fallback: a client-routable localhost URL (not the 0.0.0.0
        # bind address). Host via AGENT_HOST (default localhost); port via the
        # same AGENT_PORT → 9000 resolution main.py serves on. Do not honor the
        # AgentCore HTTP $PORT=8080 convention for this A2A runtime.
        url=os.environ.get(
            "AGENTCORE_RUNTIME_URL",
            f"http://{os.environ.get('AGENT_HOST', 'localhost')}:"
            f"{os.environ.get('AGENT_PORT') or '9000'}/",
        ),
        version="1.0.0",
        protocol_version="0.3.0",
        preferred_transport="JSONRPC",
        # Non-streaming: negotiate / notify_funded are request/response
        # (message/send). Do NOT flip this on to satisfy the AgentCore inspector's
        # chat box — that box can't drive a seller agent (it can only send plain
        # text, never the {"skill": ...} DataPart these skills require, and its
        # streaming view expects Task events). Test locally with curl / an A2A
        # client sending a DataPart (see the operating skill).
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=skills,
        **extra,
    )
