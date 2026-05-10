"""
DRL navigator (Phase 13, deferred per Q1).

The original spec proposed PPO for multi-hop pathfinding. Our Q1 decision
deferred this because 1inch / 0x / CowSwap aggregators already solve
deterministic split-routing, and they don't add latency or
nondeterminism.

This module is the lightweight stub that lets future Phase-13 work plug
in cleanly:
  - Action space: 0=direct swap, 1=triangular USDT->BTC->Token, 2=hold
  - Observation space: [OBI, spread_bps, gas_gwei, dex_liquidity_usd]
  - Reward: net_pnl_usd

For Phase 6-12 we use the DummyDrlNavigator that always picks Action 0
(direct swap). When/if real DRL training happens, replace with a
TrainedDrlNavigator that loads a Stable-Baselines3 PPO checkpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

log = logging.getLogger(__name__)

ActionT = Literal["direct", "triangular", "hold"]


class DrlNavigator(Protocol):
    def choose_action(self, observation: Sequence[float]) -> ActionT: ...


@dataclass
class DummyDrlNavigator:
    """Picks 'direct' always. Equivalent to Phase 0-12 behavior."""
    def choose_action(self, observation: Sequence[float]) -> ActionT:
        return "direct"


@dataclass
class HoldOnHighGasNavigator:
    """Tiny rule-based stand-in: HOLD when gas is high. Validates the
    Action-space wiring without needing PPO. Phase 13.X replaces with a
    real PPO policy."""
    gas_threshold_gwei: float = 5.0     # Base never approaches this; demo only

    def choose_action(self, observation: Sequence[float]) -> ActionT:
        # observation order: [obi, spread_bps, gas_gwei, dex_liquidity_usd]
        if len(observation) < 3:
            return "direct"
        gas_gwei = float(observation[2])
        if gas_gwei > self.gas_threshold_gwei:
            return "hold"
        return "direct"


def make_observation(opportunity: dict) -> tuple[float, float, float, float]:
    """Build the canonical 4-feature DRL observation from an opportunity."""
    return (
        float(opportunity.get("weighted_obi", 0.0)),
        float(opportunity.get("spread_bps", 0.0)),
        float(opportunity.get("gas_gwei", 0.0)),
        float(opportunity.get("dex_liquidity_usd", 0.0)),
    )
