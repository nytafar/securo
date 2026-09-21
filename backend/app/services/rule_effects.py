"""What a rule does in the common pot: shares and contributions.

The two group actions a rule can carry — `share_in_group` and
`mark_as_contribution` — are not fields on a transaction. They are rows
in other tables, written by the services that own them, so a rule cannot
build a set of shares or a contribution the rest of the app would refuse.

Rules therefore *plan* these effects while they run (`rule_engine`
collects them, knowing nothing about the database) and the caller
persists them inside its own database transaction. A preview plans and
writes nothing. A failing effect takes that transaction's other rule
changes down with it, and nothing else: one awkward row must never abort
a bank sync.

Two kinds of outcome, deliberately told apart:

*Skipped* — the effect is about a row the pot already has an answer for:
a transaction that carries shares (including one taken out of the pot,
shared 100 % on its payer), one that is already a contribution, one whose
account belongs to the member the rule names, one where two contributions
could be the other leg. Nothing is written, nothing is rolled back, and
running the rule again skips it again. This is what makes rules
idempotent across sync, import and apply-to-existing.

*Failed* — the effect cannot be written at all: the group or the members
the rule names are gone, or its distribution no longer materializes. The
rule itself is broken, so its other changes to that transaction are
rolled back rather than half-applied.
"""

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Optional, Sequence

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.models.transaction_split import TransactionSplit
from app.schemas.rule import RuleContributionAction, RuleShareAction
from app.schemas.transaction_split import (
    TransactionSplitInput,
    TransactionSplitsInput,
)

logger = logging.getLogger(__name__)

SHARE_ACTION = "share_in_group"
CONTRIBUTION_ACTION = "mark_as_contribution"


@dataclass(frozen=True)
class PlannedShare:
    """Shares a rule would write on the transaction it matched."""

    group_id: uuid.UUID
    # "equal" or "percent" — a rule carries no exact amounts, since it
    # fires on transactions of every size.
    share_type: Literal["equal", "percent"]
    splits: tuple[tuple[uuid.UUID, Optional[Decimal]], ...]

    def payload(self) -> TransactionSplitsInput:
        return TransactionSplitsInput(
            share_type=self.share_type,
            splits=[
                TransactionSplitInput(group_member_id=member_id, share_pct=pct)
                for member_id, pct in self.splits
            ],
        )


@dataclass(frozen=True)
class PlannedContribution:
    """A contribution a rule would make of the transaction it matched.

    `member_id` is the member on the other side, exactly as marking by
    hand takes it.
    """

    group_id: uuid.UUID
    member_id: uuid.UUID


@dataclass
class EffectReport:
    """What came of one transaction's planned effects."""

    shares_written: int = 0
    contributions_written: int = 0
    skipped: list[str] = field(default_factory=list)
    failure: Optional[str] = None

    @property
    def wrote_anything(self) -> bool:
        return bool(self.shares_written or self.contributions_written)


def parse_share_action(value: Any) -> RuleShareAction:
    """The action's stored value as a distribution, or a ValueError."""
    try:
        return RuleShareAction.model_validate(value)
    except ValidationError as exc:
        raise ValueError("Invalid group sharing action") from exc


def parse_contribution_action(value: Any) -> RuleContributionAction:
    try:
        return RuleContributionAction.model_validate(value)
    except ValidationError as exc:
        raise ValueError("Invalid contribution action") from exc


def plan_from_action(action_op: str, value: Any):
    """The effect one action plans, or None when it plans none.

    Called from the engine while rules run, so a malformed value — a rule
    hand-edited in the database, or imported from elsewhere — is silently
    no-op rather than an exception in the middle of a bank sync. Creating
    or editing a rule validates the same values properly.
    """
    try:
        if action_op == SHARE_ACTION:
            parsed = parse_share_action(value)
            return PlannedShare(
                group_id=parsed.group_id,
                share_type=parsed.share_type,
                splits=tuple(
                    (split.group_member_id, split.share_pct) for split in parsed.splits
                ),
            )
        if action_op == CONTRIBUTION_ACTION:
            parsed_contribution = parse_contribution_action(value)
            return PlannedContribution(
                group_id=parsed_contribution.group_id,
                member_id=parsed_contribution.member_id,
            )
    except ValueError:
        logger.warning("Rule action %s carries a value it cannot act on", action_op)
    return None


async def validate_action(
    session: AsyncSession, workspace_id: uuid.UUID, action_op: str, value: Any
) -> None:
    """Refuse a group action a rule could never carry out.

    Runs where every other rule action is validated, so a rule naming a
    group that is not this workspace's, a member of another group, or a
    distribution that does not add up, is turned down when it is written
    rather than every time it fires.
    """
    from app.models.group import Group, GroupMember

    async def _group(group_id: uuid.UUID) -> None:
        found = await session.execute(
            select(Group.id).where(
                Group.id == group_id, Group.workspace_id == workspace_id
            )
        )
        if found.scalar_one_or_none() is None:
            raise ValueError("Group not found")

    async def _members(group_id: uuid.UUID, member_ids: Sequence[uuid.UUID]) -> None:
        rows = await session.execute(
            select(GroupMember.id).where(
                GroupMember.group_id == group_id, GroupMember.id.in_(list(member_ids))
            )
        )
        if {row[0] for row in rows.all()} != set(member_ids):
            raise ValueError("One or more group members not found")

    if action_op == SHARE_ACTION:
        parsed = parse_share_action(value)
        await _group(parsed.group_id)
        member_ids = [split.group_member_id for split in parsed.splits]
        if not member_ids:
            raise ValueError("A sharing rule needs at least one member")
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("Each member can appear at most once in a sharing rule")
        await _members(parsed.group_id, member_ids)
        if parsed.share_type == "percent":
            total = Decimal("0")
            for split in parsed.splits:
                if split.share_pct is None:
                    raise ValueError("Split percentages must sum to 100")
                total += split.share_pct
            if total != Decimal("100"):
                raise ValueError("Split percentages must sum to 100")
    elif action_op == CONTRIBUTION_ACTION:
        parsed_contribution = parse_contribution_action(value)
        await _group(parsed_contribution.group_id)
        await _members(parsed_contribution.group_id, [parsed_contribution.member_id])


async def transaction_has_shares(
    session: AsyncSession, transaction_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(TransactionSplit.id)
        .where(TransactionSplit.transaction_id == transaction_id)
        .limit(1)
    )
    return result.first() is not None


async def _write_share(
    session: AsyncSession,
    transaction: Transaction,
    plan: PlannedShare,
    user_id: uuid.UUID,
    report: EffectReport,
) -> None:
    from app.services import split_service
    from app.services.settlement_service import is_contribution_link

    # Sharing by rule never overwrites a distribution that is already
    # there. That one rule covers three things at once: a second run of
    # the same rule changes nothing, a distribution set by hand survives,
    # and a transaction taken out of the pot — shared 100 % on its payer —
    # stays out.
    if await transaction_has_shares(session, transaction.id):
        report.skipped.append("This transaction already carries shares")
        return
    if await is_contribution_link(session, transaction.id):
        report.skipped.append(
            "This transaction is a contribution, so it cannot also be shared"
        )
        return

    await split_service.replace_splits(session, transaction, plan.payload(), user_id)
    report.shares_written += 1


async def _write_contribution(
    session: AsyncSession,
    transaction: Transaction,
    plan: PlannedContribution,
    user_id: uuid.UUID,
    report: EffectReport,
) -> None:
    from app.schemas.group_settlement import MarkContributionFromTransaction
    from app.services import settlement_service

    # Everything marking decides belongs to the settlement service: which
    # side the transaction is, whether the other leg of the same transfer
    # is already a contribution to attach to, and every reason to refuse.
    # A rule that built a `GroupSettlement` itself would make a second
    # contribution out of the second leg of one transfer.
    try:
        contribution_plan = await settlement_service.plan_contribution_from_transaction(
            session,
            plan.group_id,
            transaction.workspace_id,
            user_id,
            MarkContributionFromTransaction(
                transaction_id=transaction.id, member_id=plan.member_id
            ),
        )
    except (ValueError, PermissionError) as exc:
        # Every refusal here is a statement about this one bank row — it
        # is already a contribution, it carries shares, it sits on the
        # other member's account, two contributions could be its other
        # leg — and never about the rule. Skipping keeps a rule
        # idempotent and keeps a sync going.
        report.skipped.append(str(exc))
        return
    if contribution_plan is None:
        report.skipped.append("Group not found or not visible to this user")
        return

    await settlement_service.write_contribution_plan(
        session, plan.group_id, transaction.workspace_id, contribution_plan
    )
    report.contributions_written += 1


# Everything a rule can write on the transaction itself. A failing
# effect puts these back where the caller found them.
RULE_MANAGED_FIELDS = (
    "category_id",
    "payee_id",
    "description",
    "original_description",
    "description_is_rule_managed",
    "notes",
    "is_ignored",
)


def snapshot_rule_fields(transaction: Transaction) -> dict[str, Any]:
    """The transaction's rule-managed fields as they stand, to be handed
    back to `apply_planned_effects` as what to restore on failure."""
    return {field_name: getattr(transaction, field_name) for field_name in RULE_MANAGED_FIELDS}


async def apply_planned_effects(
    session: AsyncSession,
    transaction: Transaction,
    effects: Sequence[Any],
    user_id: uuid.UUID,
    restore_to: Optional[dict[str, Any]] = None,
) -> EffectReport:
    """Persist one transaction's planned effects, all or none.

    They are written inside a SAVEPOINT of the caller's transaction, so a
    failure takes back everything written for them and nothing else: the
    caller's own database transaction is still usable, and the sync or
    import that called this carries on with the next row.

    `restore_to` is the transaction's rule-managed fields as they were
    before the rules touched them — `snapshot_rule_fields` takes it. A
    failing effect puts them back, which is the other half of "a failing
    effect rolls back that transaction's other rule changes": SQLAlchemy
    flushes pending state when a SAVEPOINT opens, so the field changes
    are not inside it and have to be undone by hand.
    """
    report = EffectReport()
    if not effects:
        return report

    try:
        async with session.begin_nested():
            for effect in effects:
                if isinstance(effect, PlannedShare):
                    await _write_share(session, transaction, effect, user_id, report)
                elif isinstance(effect, PlannedContribution):
                    await _write_contribution(
                        session, transaction, effect, user_id, report
                    )
    except (ValueError, PermissionError) as exc:
        report.failure = str(exc)
        report.shares_written = 0
        report.contributions_written = 0
        logger.warning(
            "Rule effect failed on transaction %s: %s", transaction.id, exc
        )
        if restore_to is not None:
            for field_name, value in restore_to.items():
                setattr(transaction, field_name, value)
    return report
