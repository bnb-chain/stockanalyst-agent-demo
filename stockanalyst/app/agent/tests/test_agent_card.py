"""Protocol claims advertised by the A2A agent card."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


class _A2AType:
    """Tiny stand-in for card types; avoids importing the full A2A runtime."""

    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def _load_agent_card():
    a2a = ModuleType("a2a")
    a2a_types = ModuleType("a2a.types")
    for name in (
        "AgentCapabilities",
        "AgentCard",
        "AgentSkill",
        "ClientCredentialsOAuthFlow",
        "OAuth2SecurityScheme",
        "OAuthFlows",
        "SecurityScheme",
    ):
        setattr(a2a_types, name, _A2AType)
    a2a.types = a2a_types

    old_a2a = sys.modules.get("a2a")
    old_a2a_types = sys.modules.get("a2a.types")
    sys.modules["a2a"] = a2a
    sys.modules["a2a.types"] = a2a_types
    try:
        path = Path(__file__).parents[1] / "agent_card.py"
        spec = importlib.util.spec_from_file_location("agent_card_under_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old_a2a is None:
            sys.modules.pop("a2a", None)
        else:
            sys.modules["a2a"] = old_a2a
        if old_a2a_types is None:
            sys.modules.pop("a2a.types", None)
        else:
            sys.modules["a2a.types"] = old_a2a_types


class NotifyFundedCardTests(unittest.TestCase):
    def test_card_does_not_advertise_internal_x402_envelope(self) -> None:
        card = _load_agent_card().build_agent_card()
        skill_ids = {skill.id for skill in card.skills}
        self.assertEqual(skill_ids, {"negotiate", "notify_funded"})

    def test_negotiate_handoff_requires_signed_notify_helper(self) -> None:
        agent_card = _load_agent_card()
        description = agent_card._NEGOTIATE.description

        self.assertIn("notifyFunded", description)
        self.assertIn("EIP-712", description)
        self.assertIn("authorization", description.lower())
        self.assertIn("uomp_notify_context_required_v1", description)
        self.assertNotIn("`notify_funded` skill with the job_id", description)

    def test_card_requires_eip712_job_client_authorization(self) -> None:
        agent_card = _load_agent_card()
        description = agent_card._NOTIFY_FUNDED.description

        self.assertIn("EIP-712", description)
        self.assertIn("job client", description.lower())
        self.assertIn("authorization", description.lower())
        self.assertNotIn(
            'send {"skill": "notify_funded", "job_id": <int>}', description
        )

    def test_card_distinguishes_bare_sweep_and_named_ack_shapes(self) -> None:
        agent_card = _load_agent_card()
        description = agent_card._NOTIFY_FUNDED.description

        self.assertIn("returns status and note without job_id", description)
        self.assertIn(
            "signed named call returns status, reason, and job_id as applicable",
            description,
        )
