"""
Dice rolling utilities for the Lancer bot.

Lancer uses d20 attack rolls and various damage dice (d3, d6, d8, etc.).

Accuracy / Difficulty
---------------------
Each point of Accuracy adds 1d6 to a SEPARATE pool.
Each point of Difficulty adds 1d6 to the SAME pool.
Net the two sides:  net = accuracy - difficulty
  net > 0  →  roll |net| d6, keep the HIGHEST, add to the d20
  net < 0  →  roll |net| d6, keep the HIGHEST, subtract from the d20
  net = 0  →  no bonus dice
"""
from __future__ import annotations
import random
import re
from dataclasses import dataclass, field


# ── DiceResult ────────────────────────────────────────────────────────────────

@dataclass
class DiceResult:
    """Holds the outcome of a single dice expression."""
    expression: str       # e.g. "2d6+3"
    rolls: list[int]      # individual die faces
    modifier: int         # flat +/- added after rolling
    total: int            # final value
    label: str = ""       # e.g. "Kinetic", "Energy", "Heat"

    def __str__(self) -> str:
        roll_str = " + ".join(str(r) for r in self.rolls)
        parts = [f"[{roll_str}]"]
        if self.modifier > 0:
            parts.append(f"+ {self.modifier}")
        elif self.modifier < 0:
            parts.append(f"- {abs(self.modifier)}")
        return " ".join(parts) + f" = **{self.total}**"


@dataclass
class AccuracyResult:
    """Outcome of the accuracy/difficulty bonus dice."""
    net: int              # positive = accuracy, negative = difficulty
    rolls: list[int]      # all d6s rolled
    kept: int             # highest die value (0 if net == 0)
    applied: int          # +kept or -kept depending on net sign

    @property
    def label(self) -> str:
        if self.net == 0:
            return ""
        kind = "Accuracy" if self.net > 0 else "Difficulty"
        dice_str = ", ".join(str(r) for r in self.rolls)
        return f"{kind} ({abs(self.net)}d6: [{dice_str}] → kept {self.kept})"


@dataclass
class AttackRollResult:
    """Full attack roll: d20 + grit ± accuracy/difficulty."""
    d20: int
    grit: int
    accuracy_result: AccuracyResult
    total: int
    crit: bool   # natural 20

    def __str__(self) -> str:
        parts = [f"d20: **{self.d20}**", f"Grit: +{self.grit}"]
        if self.accuracy_result.net != 0:
            sign = "+" if self.accuracy_result.applied >= 0 else ""
            parts.append(f"Acc/Diff: {sign}{self.accuracy_result.applied}")
        parts.append(f"= **{self.total}**")
        if self.crit:
            parts.append("🎯 **CRITICAL HIT!**")
        return "  ".join(parts)


# ── Parsing ───────────────────────────────────────────────────────────────────

_DICE_RE = re.compile(
    r"""
    (?:(\d+)d(\d+))          # NdM  (group 1=count, 2=sides)
    |                         # OR
    (?:([+-]?\s*\d+))        # flat modifier (group 3)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_INLINE_RE = re.compile(
    r"""
    \b
    (\d+)                    # die count
    d                        # literal 'd'
    (\d+)                    # die sides
    (?:                      # optional modifier
        \s*([+-])\s*(\d+)
    )?
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)


def roll_expression(expr: str, label: str = "") -> DiceResult:
    """
    Roll a dice expression like "2d6+3", "1d6", "3", "1d3+3".
    Returns a DiceResult with each die shown and the total.
    """
    expr = str(expr).strip()

    # Pure integer — no rolling needed
    if re.fullmatch(r"[+-]?\d+", expr):
        val = int(expr)
        return DiceResult(
            expression=expr,
            rolls=[],
            modifier=val,
            total=val,
            label=label,
        )

    rolls: list[int] = []
    modifier = 0

    for m in _DICE_RE.finditer(expr.replace(" ", "")):
        count_s, sides_s, flat_s = m.group(1), m.group(2), m.group(3)
        if count_s and sides_s:
            count = int(count_s)
            sides = int(sides_s)
            for _ in range(count):
                rolls.append(random.randint(1, sides))
        elif flat_s:
            modifier += int(flat_s.replace(" ", ""))

    total = sum(rolls) + modifier
    return DiceResult(
        expression=expr,
        rolls=rolls,
        modifier=modifier,
        total=total,
        label=label,
    )


def roll_accuracy(accuracy: int, difficulty: int) -> AccuracyResult:
    """
    Roll the net accuracy/difficulty pool.
    net > 0 → add highest d6; net < 0 → subtract highest d6.
    """
    net = accuracy - difficulty
    if net == 0:
        return AccuracyResult(net=0, rolls=[], kept=0, applied=0)

    pool_size = abs(net)
    rolls = [random.randint(1, 6) for _ in range(pool_size)]
    kept = max(rolls)
    applied = kept if net > 0 else -kept
    return AccuracyResult(net=net, rolls=rolls, kept=kept, applied=applied)


def roll_attack(grit: int, accuracy: int = 0, difficulty: int = 0) -> AttackRollResult:
    """Roll a full Lancer attack: 1d20 + grit ± acc/diff."""
    d20 = random.randint(1, 20)
    acc_result = roll_accuracy(accuracy, difficulty)
    total = d20 + grit + acc_result.applied
    return AttackRollResult(
        d20=d20,
        grit=grit,
        accuracy_result=acc_result,
        total=total,
        crit=(d20 == 20),
    )


def find_dice_in_text(text: str) -> list[re.Match]:
    """Return all regex matches of dice expressions found in a block of text."""
    return list(_INLINE_RE.finditer(text))


def roll_all_dice_in_text(text: str) -> tuple[str, list[DiceResult]]:
    """
    Replace every NdM±X in text with 'NdM±X (rolled → TOTAL)'
    and return (annotated_text, list_of_DiceResults).
    """
    results: list[DiceResult] = []

    def replacer(m: re.Match) -> str:
        count = int(m.group(1))
        sides = int(m.group(2))
        sign = m.group(3) or ""
        flat = int(m.group(4) or 0)
        modifier = int(sign + str(flat)) if sign else 0

        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + modifier

        roll_str = "+".join(str(r) for r in rolls)
        mod_str = f"{sign}{flat}" if sign else ""
        result_str = f"`{m.group(0)}` → [{roll_str}]{mod_str} = **{total}**"

        dr = DiceResult(
            expression=m.group(0),
            rolls=rolls,
            modifier=modifier,
            total=total,
        )
        results.append(dr)
        return result_str

    annotated = _INLINE_RE.sub(replacer, text)
    return annotated, results
