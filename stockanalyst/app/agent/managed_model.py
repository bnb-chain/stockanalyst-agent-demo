"""ADK model factory — emitted user code.

This file is **your project's code**, scaffolded by ``bag`` from the
``frameworks/adk`` recipe (``frameworks/adk/code/agent/managed_model.py.tmpl``)
and emitted at ``agent/managed_model.py`` (i.e. ``agent/managed_model.py``
in the standard layout) for every ADK project, whatever ``[llm].provider`` you
chose. It is yours to edit, fork, or replace — studio will not silently
rewrite it.

What it does:

- Exposes :func:`build_model`, the factory called by the sibling ``main.py``
  to construct the right ADK model for the project's ``[llm]`` config. For
  every provider it builds a plain ``LiteLlm``; for ``pieverse-llm`` (with
  auto-renew on) it additionally wraps it in :class:`PieverseManagedModel`.
- Defines :class:`PieverseManagedModel`, a thin ADK ``LiteLlm`` subclass that
  awaits a Pieverse credit-ensure hook before every ``generate_content_async``
  call — inert unless the provider is ``pieverse-llm``.

The credit-refresh / auto-allocate / auto-topup logic itself lives in the
library at :class:`bnbagent_studio_core.pieverse.PieverseCreditEnsurer` — this shell
just wires it into ADK's generate-call path. That keeps the framework
adapter tiny: you can fork *this file* (e.g. to swap ADK for another
framework) without forking ``studio`` itself.

Subclassing rationale: ADK's ``LlmAgent.canonical_model`` enforces
``isinstance(model, BaseLlm)``, so a delegation wrapper would not plug into
the agent runner. ``LiteLlm`` is a Pydantic ``BaseModel`` with
``arbitrary_types_allowed=True``; Pydantic v2 treats class-level
``_attr: Type = default`` declarations as ``PrivateAttr`` automatically.
We follow that convention for the ``_ensurer`` reference.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

from bnbagent_studio_core import config
from bnbagent_studio_core.llm import _resolve_provider_config
from bnbagent_studio_core.pieverse import PieverseCreditEnsurer
from bnbagent_studio_core.pieverse.policy import BudgetPolicy, PieversePolicy
from bnbagent_studio_core.wallet import get_wallet
from google.adk.models.lite_llm import LiteLlm

if TYPE_CHECKING:  # pragma: no cover
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse


class PieverseManagedModel(LiteLlm):
    """``LiteLlm`` with a Pieverse credit-ensure hook on each generate call.

    Holds one :class:`PieverseCreditEnsurer` (framework-neutral logic) as a
    Pydantic private attribute and calls ``await self._ensurer.ensure_credits()``
    before every ``generate_content_async`` invocation.
    """

    # PrivateAttr (Pydantic v2 auto-detects leading-underscore class attrs).
    _ensurer: Any = None  # PieverseCreditEnsurer | None

    def __init__(
        self,
        inner: LiteLlm,
        *,
        wallet: Any,
        key_hash: str,
        network_name: str,
        policy: PieversePolicy,
        budget_policy: BudgetPolicy | None = None,
        http_client: Any = None,
        session_token: str | None = None,
        clock: Callable[[], float] | None = None,
    ):
        # Re-construct the ``LiteLlm`` half from the inner's public fields +
        # private ``_additional_args``. We don't mutate ``inner`` — we own a
        # fresh instance with the same configuration.
        super().__init__(
            model=inner.model,
            **(getattr(inner, "_additional_args", None) or {}),
        )
        self._ensurer = PieverseCreditEnsurer(
            model_id=inner.model,
            wallet=wallet,
            key_hash=key_hash,
            network_name=network_name,
            policy=policy,
            budget_policy=budget_policy,
            http_client=http_client,
            session_token=session_token,
            clock=clock,
        )

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        await self._ensurer.ensure_credits()
        async for chunk in super().generate_content_async(llm_request, stream=stream):
            yield chunk


def build_model() -> LiteLlm:
    """Build the ADK model object for this project's ``[llm]`` config.

    Called by the sibling ``main.py``. Reads ``studio.toml`` via
    :func:`bnbagent_studio_core.config.load_studio_toml`, resolves the provider via
    :func:`bnbagent_studio_core.llm._resolve_provider_config`, constructs a base
    ``LiteLlm``, and (when the provider is ``pieverse-llm`` and auto-renew
    is enabled) wraps it in :class:`PieverseManagedModel`.

    For non-Pieverse providers — or when ``[llm.auto_renew].enabled = false``
    — returns the raw inner ``LiteLlm`` unwrapped.
    """
    cfg = config.load_studio_toml()
    llm_cfg = cfg.get("llm", {})
    provider_cfg = _resolve_provider_config(llm_cfg)

    # Construct the base LiteLlm, skipping None kwargs.
    kwargs: dict[str, Any] = {}
    if provider_cfg.api_key:
        kwargs["api_key"] = provider_cfg.api_key
    if provider_cfg.base_url:
        kwargs["api_base"] = provider_cfg.base_url
    inner = LiteLlm(model=provider_cfg.litellm_model, **kwargs)

    if provider_cfg.provider != "pieverse-llm":
        return inner

    # Pieverse path — wrap in PieverseManagedModel unless auto-renew opted out.
    auto_renew_cfg = (cfg.get("llm") or {}).get("auto_renew") or {}
    pieverse_cfg = llm_cfg.get("pieverse") or {}
    budget_cfg = cfg.get("budget") or {}

    policy = PieversePolicy.from_toml(auto_renew_cfg)
    if not policy.enabled:
        return inner

    key_hash = pieverse_cfg.get("key_hash")
    if not key_hash:
        raise RuntimeError(
            "[llm.pieverse].key_hash is missing in studio.toml. "
            "Run `bag llm activate` to create a Pieverse key first. "
            "(After activate, you may need to restart the agent process "
            "for changes to take effect.)"
        )
    network_name = pieverse_cfg.get("network") or "bsc-mainnet"
    budget_policy = BudgetPolicy.from_toml(budget_cfg)

    return PieverseManagedModel(
        inner,
        wallet=get_wallet(),
        key_hash=key_hash,
        network_name=network_name,
        policy=policy,
        budget_policy=budget_policy,
    )
