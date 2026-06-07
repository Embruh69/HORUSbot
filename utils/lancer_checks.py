"""
lancer_checks.py — Structure and Stress check logic for the Lancer bot.

Structure check (triggered when HP reaches 0)
----------------------------------------------
- Spend 1 structure, refill HP to max, carry any overflow damage forward
- Roll 1d6 per structure point already MISSING (including the one just lost)
  → take the single LOWEST result
- If the mech is at 0 structure it is destroyed (no roll)
- If carrying an NHP system (tg_ai tag), also roll a d20 CASCADE check

Stress check (triggered when heat EXCEEDS heat cap, i.e. heat > heatcap)
-------------------------------------------------------------------------
- Spend 1 stress, reset heat to 0, carry overflow heat forward
- Roll 1d6 per stress point already MISSING (including the one just lost)
  → take the single LOWEST result
- If the mech is at 0 stress it suffers a reactor meltdown at end of next turn
- NHP cascade d20 check applies here too
"""
from __future__ import annotations
import random
from dataclasses import dataclass
from typing import Optional


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class StructureResult:
    dice_rolled: list[int]      # all d6s rolled
    lowest: int                 # the result that matters (lowest = worst)
    structure_before: int       # structure count before taking the hit
    structure_after: int        # structure count after (before = after + 1)
    hp_overflow: int            # any HP damage that spills into next structure
    destroyed: bool             # True if structure_after == 0
    # cascade
    nhp_present: bool
    cascade_roll: Optional[int]  # d20 roll, None if no NHP
    cascade_triggered: bool      # True if cascade roll <= 10

    @property
    def result_name(self) -> str:
        if self.destroyed:
            return "💀 MECH DESTROYED"
        if len([d for d in self.dice_rolled if d == 1]) >= 2:
            return "☠️ Multiple 1s — Crushing Hit"
        table = {1: "💀 Direct Hit", 2: "⚠️ System Trauma", 3: "⚠️ System Trauma",
                 4: "⚠️ System Trauma", 5: "🛡️ Glancing Blow", 6: "🛡️ Glancing Blow"}
        return table.get(self.lowest, "❓ Unknown")

    @property
    def result_detail(self) -> str:
        if self.destroyed:
            return (
                "Your mech has been reduced to **0 Structure** and is **destroyed**. "
                "You may exit it as normal."
            )
        # Multiple 1s overrides the individual result
        ones = [d for d in self.dice_rolled if d == 1]
        if len(ones) >= 2:
            return (
                "*Ouch.* Your mech is damaged beyond repair — it is **destroyed**. "
                "You may still exit it as normal."
            )
        if self.lowest in (5, 6):
            return (
                "Emergency systems kick in and stabilize your mech, but it's "
                "**IMPAIRED** until the end of your next turn."
            )
        if self.lowest in (2, 3, 4):
            return (
                "Roll 1d6. **1–3:** your choice of weapon mount is destroyed; "
                "**4–6:** a system of your choice is destroyed. "
                "(LIMITED systems and weapons out of charges are not valid choices.) "
                "If there are no valid choices remaining, it becomes the other result. "
                "If neither is possible, this becomes a **Direct Hit** instead."
            )
        if self.lowest == 1:
            return (
                "Depends on your mech's remaining **STRUCTURE**.\n"
                "**3+ Structure:** Your mech is **STUNNED** until the end of your next turn.\n"
                "**2 Structure:** Roll a HULL check. On a success, your mech is STUNNED. "
                "On a failure, your mech is **destroyed!**\n"
                "**1 Structure:** Your mech is **destroyed!**"
            )
        return "Consult the Structure Damage table."


@dataclass
class StressResult:
    dice_rolled: list[int]
    lowest: int
    stress_before: int
    stress_after: int
    heat_overflow: int          # heat carried into next round after reset
    meltdown: bool              # True if stress_after == 0 (reactor meltdown)
    # cascade
    nhp_present: bool
    cascade_roll: Optional[int]
    cascade_triggered: bool

    @property
    def result_name(self) -> str:
        if self.meltdown:
            return "☢️ REACTOR MELTDOWN"
        if len([d for d in self.dice_rolled if d == 1]) >= 2:
            return "☢️ Multiple 1s — Irreversible Meltdown"
        table = {1: "☢️ Meltdown", 2: "⚡ Destabilised Power Plant",
                 3: "⚡ Destabilised Power Plant", 4: "⚡ Destabilised Power Plant",
                 5: "🌡️ Emergency Shunt", 6: "🌡️ Emergency Shunt"}
        return table.get(self.lowest, "❓ Unknown")

    @property
    def result_detail(self) -> str:
        if self.meltdown:
            return (
                "Your mech has been reduced to **0 Stress** and suffers a "
                "**reactor meltdown** at the end of your next turn."
            )
        ones = [d for d in self.dice_rolled if d == 1]
        if len(ones) >= 2:
            return (
                "The reactor goes critical — your mech suffers a "
                "**reactor meltdown** at the end of your next turn."
            )
        if self.lowest in (5, 6):
            return (
                "Emergency cooling kicks in. Your mech becomes **IMPAIRED** "
                "until the end of your next turn."
            )
        if self.lowest in (2, 3, 4):
            return (
                "The power plant becomes unstable, beginning to eject jets of plasma. "
                "Your mech becomes **EXPOSED**, taking double Kinetic, Energy and "
                "Explosive damage until the status is cleared."
            )
        if self.lowest == 1:
            return (
                "Depends on your mech's remaining **STRESS**.\n"
                "**3+ Stress:** Your mech becomes **EXPOSED**.\n"
                "**2 Stress:** Roll an ENGINEERING check. On a success, your mech is EXPOSED. "
                "On a failure, it suffers a **reactor meltdown** after 1d6 of your turns "
                "(rolled by the GM).\n"
                "**1 Stress:** Your mech suffers a **reactor meltdown** at the end of your next turn!"
            )
        return "Consult the Reactor Stress table."


# ── NHP helper ────────────────────────────────────────────────────────────────

def _has_nhp(mech) -> bool:
    """Return True if the mech has at least one system with the tg_ai tag."""
    for sys in mech.systems:
        if "tg_ai" in sys.tag_ids:
            return True
    return False


def _nhp_names(mech) -> list[str]:
    return [s.name for s in mech.systems if "tg_ai" in s.tag_ids]


# ── Main roll functions ───────────────────────────────────────────────────────

def roll_structure_check(
    structure_before: int,
    max_structure: int,
    hp_overflow: int = 0,
) -> StructureResult:
    """
    Roll a structure check.

    structure_before: current structure BEFORE taking this hit
    max_structure:    the mech's max structure (usually 4)
    hp_overflow:      any HP damage that spilled past 0 HP

    Returns a StructureResult.
    """
    structure_after = structure_before - 1

    if structure_after <= 0:
        # No roll needed — mech is destroyed
        return StructureResult(
            dice_rolled=[],
            lowest=0,
            structure_before=structure_before,
            structure_after=0,
            hp_overflow=hp_overflow,
            destroyed=True,
            nhp_present=False,
            cascade_roll=None,
            cascade_triggered=False,
        )

    # Number of dice = structure points missing (including the one just lost)
    missing = max_structure - structure_after
    dice = [random.randint(1, 6) for _ in range(missing)]
    lowest = min(dice)

    return StructureResult(
        dice_rolled=dice,
        lowest=lowest,
        structure_before=structure_before,
        structure_after=structure_after,
        hp_overflow=hp_overflow,
        destroyed=False,
        nhp_present=False,       # caller fills in after
        cascade_roll=None,
        cascade_triggered=False,
    )


def roll_stress_check(
    stress_before: int,
    max_stress: int,
    heat_overflow: int = 0,
) -> StressResult:
    """
    Roll a stress check.

    stress_before:  current stress BEFORE this overload
    max_stress:     the mech's max stress (usually 4)
    heat_overflow:  any heat that spilled past heat cap
    """
    stress_after = stress_before - 1

    if stress_after <= 0:
        return StressResult(
            dice_rolled=[],
            lowest=0,
            stress_before=stress_before,
            stress_after=0,
            heat_overflow=heat_overflow,
            meltdown=True,
            nhp_present=False,
            cascade_roll=None,
            cascade_triggered=False,
        )

    missing = max_stress - stress_after
    dice = [random.randint(1, 6) for _ in range(missing)]
    lowest = min(dice)

    return StressResult(
        dice_rolled=dice,
        lowest=lowest,
        stress_before=stress_before,
        stress_after=stress_after,
        heat_overflow=heat_overflow,
        meltdown=False,
        nhp_present=False,
        cascade_roll=None,
        cascade_triggered=False,
    )


def attach_cascade(result: StructureResult | StressResult, mech) -> None:
    """
    If the mech is carrying an NHP (tg_ai system), roll d20 cascade check
    and attach results in-place.  Called after roll_structure/stress_check.
    """
    if _has_nhp(mech):
        roll = random.randint(1, 20)
        result.nhp_present = True
        result.cascade_roll = roll
        result.cascade_triggered = (roll == 1)
