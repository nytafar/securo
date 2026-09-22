"""Shared SQLAlchemy filter fragments for report/dashboard queries.

Centralizes the "what counts as real income/expense" definition so every
aggregation site agrees. Changes to the rule (e.g. adding a new exclusion
signal) only need to be made here.
"""
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.category import Category
from app.models.group_settlement import GroupSettlement
from app.models.transaction import Transaction
from app.models.workspace import Workspace, WorkspaceMember


def is_contribution_link():
    """SQL filter: this row *is* the bank transaction a group contribution
    is made of, on either side of it.

    A contribution is never a copy of a transaction: the real bank row is
    linked, from the payer side (`transaction_id`) or the receiver side
    (`receiver_transaction_id`). A legacy row that links a credit in the
    payer-side column is the receiver side read the other way round —
    which changes nothing here, because both columns are searched.

    The `IS NOT NULL` inside each subquery matters: `IN (NULL)` is unknown
    rather than false, so without it the whole predicate would go unknown
    as soon as one settlement left a side unlinked.
    """
    return or_(
        Transaction.id.in_(
            select(GroupSettlement.transaction_id).where(
                GroupSettlement.transaction_id.is_not(None)
            )
        ),
        Transaction.id.in_(
            select(GroupSettlement.receiver_transaction_id).where(
                GroupSettlement.receiver_transaction_id.is_not(None)
            )
        ),
    )


async def contribution_linked_ids(
    session: AsyncSession, transaction_ids
) -> set[uuid.UUID]:
    """Which of these transactions `is_contribution_link()` matches.

    For readers that have already loaded their rows and decide what to
    count in Python. Built from the same fragment the SQL readers use, so
    the two cannot answer differently.
    """
    ids = list(transaction_ids)
    if not ids:
        return set()
    result = await session.execute(
        select(Transaction.id).where(Transaction.id.in_(ids), is_contribution_link())
    )
    return {row[0] for row in result.all()}


def is_confirmed():
    """SQL filter: the charge is settled rather than merely authorized.

    One of the two independent axes a transaction sits on. This one is about
    *confirmation*: a pending row is real money already committed, it just
    has not cleared yet. It says nothing about when the row is dated.
    """
    return Transaction.status == "posted"


def is_not_future(as_of: date):
    """SQL filter: the transaction has already happened by ``as_of``.

    The other axis, and a pure date question. A future-dated row is forecast
    no matter how confirmed it is; a past-dated row has happened no matter
    whether the bank has cleared it.
    """
    return Transaction.date <= as_of


def is_inside_provider_snapshot():
    """SQL filter: the provider's balance already accounts for this row.

    A connected account's current balance is the number the provider sends,
    not a sum of our rows, and providers net out the pending charges they
    report. A row typed by hand is ambiguous the same way, since the user is
    usually copying a charge the bank is already showing them.

    A recurring placeholder is the one case we can be sure about: we invented
    the row from a schedule, so no provider has ever seen it. Treating it as
    already counted makes it cancel itself out, leaving a charge that shows up
    in the forecast totals but moves no balance.
    """
    return Transaction.source != "recurring"


def counts_in_current_balance(as_of: date):
    """SQL filter: the row belongs in the balance labelled "current".

    Composed from the two axes above so the definition lives in one place and
    moving the line later is a change here rather than at every query site.

    Today the line sits at "confirmed and not future", with one exception:
    a credit card's balance is the debt owed, and an authorized purchase is
    already owed, so pending card rows stay in. Without that carve-out the
    card's balance understates the debt while its own bill total includes it.
    """
    return and_(
        is_not_future(as_of),
        or_(is_confirmed(), Account.type == "credit_card"),
    )


def reporting_date_col(accounting_mode: str):
    """The date column a transaction should be *bucketed by* in period
    aggregations (dashboard, reports, budgets).

    Honors the manual credit-card cycle override (`effective_bill_date`)
    FIRST — regardless of accounting mode — because that's the whole point
    of the override: the user hand-corrected which invoice a purchase
    belongs to (issue #92). When there's no override, fall back to
    `effective_date` in accrual mode or the raw purchase `date` in cash
    mode.

    This mirrors the ordering used by the transaction list and the credit
    card bill view, so a transaction lands in the same month everywhere the
    user looks. Aggregations that skipped the override summed credit-card
    spend under the purchase month instead of the invoice month (issue
    #232).
    """
    base = (
        Transaction.effective_date
        if accounting_mode == "accrual"
        else Transaction.date
    )
    return func.coalesce(Transaction.effective_bill_date, base)


def reporting_date_of(transaction, accounting_mode: str) -> date:
    """`reporting_date_col` for a row already in memory.

    Same coalesce, same order, so a contribution dated from the
    transaction it is made of lands in the period that transaction lands
    in everywhere else.
    """
    base = (
        transaction.effective_date
        if accounting_mode == "accrual"
        else transaction.date
    )
    return transaction.effective_bill_date or base or transaction.date


def is_not_ignored():
    """SQL filter: the row is not one the user told us to disregard.

    Only the ignore signal, without the transfer/settlement family that
    `counts_as_pnl` folds in, because hiding rows from a *list* is a
    different question from leaving them out of a *total*: a transfer still
    belongs in the ledger the user is reading.

    Matches what the UI badges as ignored, which is the transaction flag or
    its category's — see `TransactionRead.reflect_ignored_category`. A list
    that hid one but not the other would leave visibly-ignored rows behind
    and look broken.
    """
    return and_(
        Transaction.is_ignored.is_(False),
        or_(
            Transaction.category_id.is_(None),
            Transaction.category_id.not_in(
                select(Category.id).where(Category.is_ignored.is_(True))
            ),
        ),
    )


def counts_as_pnl():
    """SQL filter: True when a transaction should contribute to income/expense totals.

    Excludes:
      - paired transfers (both legs were matched; already cancel out),
      - transactions in categories flagged `treat_as_transfer` (one-sided
        movements like investment applications where the counterpart is
        an Asset/Holding, not another Account),
      - transactions flagged `is_ignored=True` (user-marked as not to be reported),
      - transactions flagged `exclude_from_pnl=True` (kept in balance,
        omitted from income and expense calculations),
      - transactions in categories flagged `is_ignored=True` (user-marked as not to be reported),
      - the bank transaction a group contribution is made of, on either
        side: the money one member moves to another to carry their part
        of the common pot is neither income nor spending for either of
        them, exactly as a paired transfer is neither. It still moves the
        account balance and still shows in the transaction list.

    Does NOT exclude `source='opening_balance'` — callers that already
    filter those keep doing so; this helper only handles the transfer-like
    exclusion family so both rules stay visible at each call site.
    """
    return and_(
        Transaction.transfer_pair_id.is_(None),
        ~is_contribution_link(),
        Transaction.is_ignored.is_(False),
        Transaction.exclude_from_pnl.is_(False),
        # Settlement *debits* are repayments of debts that were already
        # booked as an expense via the share. Counting them would
        # double-count. Settlement *credits*, however, represent the
        # receiver actually getting the cash back — they offset the
        # over-recorded expense from when the receiver paid the full
        # parent transaction. So we keep credits, drop debits.
        ~and_(Transaction.source == "settlement", Transaction.type == "debit"),
        or_(
            Transaction.category_id.is_(None),
            Transaction.category_id.not_in(
                select(Category.id).where(
                    or_(
                        Category.treat_as_transfer.is_(True),
                        Category.is_ignored.is_(True),
                    )
                )
            ),
        ),
    )


def counts_on_bill():
    """SQL filter: True when a transaction belongs on a credit-card bill.

    A bill total is an *amount owed*, not a reporting figure, and the two
    answer to different authorities: the bill has to match what the bank
    says you owe, while P/L answers to how the user chose to categorize
    their spending. So the card's cycle total cannot reuse
    `counts_as_pnl` — every judgment that helper makes about what counts
    as *spending* is a judgment the bank never made.

    Kept out, because they are genuinely not charges on this bill:
      - paired transfers (the bill *payment* is not a purchase),
      - settlement debits (a repayment of a share already booked),
      - rows the user flagged `is_ignored`, on the transaction or its
        category — those leave the account balance too, so dropping them
        from the bill keeps the card's two numbers telling one story.

    `treat_as_transfer` categories are handled asymmetrically, and this is
    the whole point of the helper:
      - kept in for *debits*: buying an investment or paying a consortium
        installment with the card still lands on the statement, so a
        charge doesn't stop being owed to the bank because of how it was
        tagged afterwards (issue #647).
      - dropped for *credits*: an unpaired card payment (the payer's
        account isn't connected, the amount doesn't match exactly, or it
        was a partial payment) is normally filed under a transfer-like
        category, and letting it through here would net it against new
        debt instead of being a repayment of it.

    Neither reading is exactly right — there's no field today that tells
    a genuine merchant refund apart from an unpaired bill payment once
    both land as a credit in a transfer-like category, so this is a
    judgment call, not a derived fact. Dropping transfer-tagged credits
    errs toward the more common case (an unmatched payment silently
    shrinking the bill every cycle) over the rarer one (a refund of a
    transfer-tagged purchase failing to shrink it back).

    Deliberately spelled out rather than defined as "`counts_as_pnl`
    minus a clause": a filter for what a *report* excludes will keep
    growing as the product learns new ways to say "don't count this",
    and a bill total must not inherit those. Every clause here is one
    somebody chose for the bill.
    """
    ignored_category = Transaction.category_id.in_(
        select(Category.id).where(Category.is_ignored.is_(True))
    )
    transfer_category = Transaction.category_id.in_(
        select(Category.id).where(Category.treat_as_transfer.is_(True))
    )
    return and_(
        Transaction.transfer_pair_id.is_(None),
        Transaction.is_ignored.is_(False),
        ~and_(Transaction.source == "settlement", Transaction.type == "debit"),
        or_(Transaction.category_id.is_(None), ~ignored_category),
        or_(Transaction.type == "debit", Transaction.category_id.is_(None), ~transfer_category),
    )


def counts_as_user_pnl():
    """SQL filter for *user-level* P/L (dashboard, reports, budgets).

    Stricter than `counts_as_pnl`: also drops settlement *credits*. Under
    the share-only model an owner's expense for a split tx is just their
    share, so the corresponding settlement credits would double-count.
    Per-account stats still use `counts_as_pnl` because account ledgers
    track real cash through the account, not user P/L.
    """
    return and_(
        counts_as_pnl(),
        Transaction.source != "settlement",
    )


# ─────────────────────── the consumption subject ────────────────────────
#
# Consumption is a person's own costs plus their shares of shared costs,
# whoever paid. Which shares count is therefore a question about the
# *subject* of a figure, never about who is looking at it: with the user
# filter on my partner I have to see her consumption from my own login.
#
# Every helper below takes that subject explicitly. Three cases, and
# nothing else:
#
#   user filter    subject = that user, scope = the accounts they own.
#   no filter      subject = everyone in the workspace, scope = the
#                  workspace. Shares between two members of it cancel,
#                  because both sides are the subject; only shares of
#                  members outside it adjust the total.
#   a collection   no subject at all. A collection is cash flow over its
#                  accounts, with no share adjustment, as it always was.


@dataclass(frozen=True)
class ConsumptionSubject:
    """Whose consumption a user-level figure measures.

    ``user_ids`` are the people the figure is about. ``account_ids`` are
    the accounts their own costs sit on — ``None`` means the whole
    workspace, which is the no-filter case.
    """

    workspace_id: uuid.UUID
    user_ids: tuple[uuid.UUID, ...]
    account_ids: Optional[tuple[uuid.UUID, ...]] = None


async def workspace_user_ids(
    session: AsyncSession, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, ...]:
    """Every user who belongs to this workspace.

    Membership rows plus the workspace's creator and its manager: a
    business workspace is operated through `managed_by_user_id` rather
    than a membership row, and a subject that missed its only user would
    read every share as somebody else's.
    """
    rows = await session.execute(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id
        )
    )
    ids = {row[0] for row in rows.all() if row[0] is not None}
    owners = await session.execute(
        select(Workspace.created_by_user_id, Workspace.managed_by_user_id).where(
            Workspace.id == workspace_id
        )
    )
    row = owners.one_or_none()
    if row is not None:
        ids.update(value for value in row if value is not None)
    return tuple(sorted(ids, key=str))


async def user_owned_account_ids(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[uuid.UUID]:
    """The accounts in this workspace that `user_id` owns.

    What the user filter resolves to. Ownership is the same signal the
    pot uses to name a payer, so "her accounts" means the same thing on
    the group page and on the dashboard.

    A closed account is left out, because every figure the filter feeds
    already drops closed accounts. Resolving to one would have put a
    cost in the scope whose own base query could never see it, and the
    share of it that belongs to somebody else would have come off a
    total that never had it.
    """
    result = await session.execute(
        select(Account.id).where(
            Account.workspace_id == workspace_id,
            Account.user_id == user_id,
            Account.is_closed == False,  # noqa: E712
        )
    )
    return [row[0] for row in result.all()]


async def resolve_consumption_scope(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID] = None,
    account_ids: Optional[list[uuid.UUID]] = None,
) -> tuple[Optional[list[uuid.UUID]], Optional[ConsumptionSubject]]:
    """Turn the two filters a page can carry into (accounts, subject).

    The returned account list is what the plain cash-flow queries filter
    on; the subject is what the share helpers adjust for, or ``None``
    when no adjustment is wanted.

    A user filter wins over a collection: the two are alternatives in
    the UI, and reading them as an intersection would give a figure that
    is neither person's consumption nor the collection's cash flow.
    """
    if user_id is not None:
        owned = await user_owned_account_ids(session, workspace_id, user_id)
        return owned, ConsumptionSubject(
            workspace_id=workspace_id,
            user_ids=(user_id,),
            account_ids=tuple(owned),
        )
    if account_ids is not None:
        return account_ids, None
    return None, ConsumptionSubject(
        workspace_id=workspace_id,
        user_ids=await workspace_user_ids(session, workspace_id),
        account_ids=None,
    )


def subject_member_ids(subject: ConsumptionSubject, *, invitations_only: bool = False):
    """The group members that are this subject's own.

    Read from persisted columns only — a member's stored link, or the
    stored self flag inside a group one of the subject's users owns. The
    `is_self` the group service rewrites per request never reaches a
    query, so two people looking at the same figure see the same number.

    `invitations_only` narrows it to memberships somebody else created
    for one of the subject's users: linked, and not the self-membership
    that stands for them in a group of their own. That is the only kind
    of membership that may carry a figure **across workspaces** — a
    self-membership belongs to a group in its own workspace and is
    already counted there, so letting it out would put the household's
    groceries on the personal workspace's dashboard.
    """
    from app.models.group import Group, GroupMember

    conditions = [GroupMember.linked_user_id.in_(subject.user_ids)]
    if not invitations_only:
        conditions.append(
            and_(
                GroupMember.is_self == True,  # noqa: E712
                Group.user_id.in_(subject.user_ids),
            )
        )
    stmt = (
        select(GroupMember.id)
        .outerjoin(Group, Group.id == GroupMember.group_id)
        .where(or_(*conditions))
    )
    if invitations_only:
        stmt = stmt.where(GroupMember.is_self.is_(False))
    return stmt


def in_subject_scope(subject: ConsumptionSubject) -> list:
    """Filters picking the transactions whose full amount the subject's
    own aggregation already counted."""
    filters = [Transaction.workspace_id == subject.workspace_id]
    if subject.account_ids is not None:
        filters.append(Transaction.account_id.in_(subject.account_ids))
    return filters


def outside_subject_scope(subject: ConsumptionSubject):
    """Transactions in this workspace that the subject's own aggregation
    did not count at full amount.

    Deliberately **not** "any other workspace": a workspace is a separate
    set of books, and a figure in one may not be moved by a share in
    another. Money that crosses that line does so only through the
    invitation clause in `subject_share_match`, which is the rule
    upstream had and this one keeps.
    """
    if subject.account_ids is None:
        # The scope is the whole workspace: nothing in it is outside.
        return false()
    return and_(
        Transaction.workspace_id == subject.workspace_id,
        or_(
            Transaction.account_id.is_(None),
            Transaction.account_id.notin_(subject.account_ids),
        ),
    )


def subject_share_match(subject: ConsumptionSubject, *, mine: bool, scope: str):
    """One clause: this share belongs to whom, on a transaction where.

    `scope="in"` is the subject's own scope — their accounts, or the
    whole workspace when nothing is filtered. `mine=False` there picks
    the shares of that money that belong to somebody else, which come
    off the subject's total.

    `scope="out"` is what the subject carries of money counted nowhere
    in their own aggregation, and it has exactly two sources:

    * **this workspace, another account** — my share of the groceries my
      partner paid. Only reachable under the user filter, because with
      no filter the scope is the whole workspace.
    * **another workspace, by invitation** — my share of the concert
      ticket a friend paid, in their books. Bounded by both of the
      guards upstream had: the membership must be one somebody else
      created for me (`is_self` false), and the transaction must not be
      one of the subject's own. Without them, every figure in a
      household would also appear in each member's personal workspace.

    `scope="any"` is both, for the credit term, which asks what the
    subject was given back wherever the cost sat.
    """
    from app.models.transaction_split import TransactionSplit

    if scope == "any":
        return or_(
            subject_share_match(subject, mine=mine, scope="in"),
            subject_share_match(subject, mine=mine, scope="out"),
        )

    if scope == "in":
        member_ids = subject_member_ids(subject)
        belongs = (
            TransactionSplit.group_member_id.in_(member_ids)
            if mine
            else TransactionSplit.group_member_id.notin_(member_ids)
        )
        return and_(*in_subject_scope(subject), belongs)

    # scope == "out"
    if not mine:
        # Whose the rest of somebody else's money is is not a question
        # any figure here asks.
        return false()
    across = and_(
        Transaction.workspace_id != subject.workspace_id,
        Transaction.user_id.notin_(subject.user_ids),
        TransactionSplit.group_member_id.in_(
            subject_member_ids(subject, invitations_only=True)
        ),
    )
    if subject.account_ids is None:
        return across
    return or_(
        across,
        and_(
            outside_subject_scope(subject),
            TransactionSplit.group_member_id.in_(subject_member_ids(subject)),
        ),
    )


def _reporting_date_expr(use_effective_date: bool):
    return func.coalesce(
        Transaction.effective_bill_date,
        Transaction.effective_date if use_effective_date else Transaction.date,
    )


async def _share_pnl(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    mine: bool,
    scope: str,
    use_effective_date: bool,
    primary_currency: Optional[str],
    label_expr=None,
) -> dict:
    """{period label: (credit total, debit total)} of the matching shares.

    Without `label_expr` there is one bucket, keyed ``None`` — the whole
    range. With it, the caller's own period expression decides the keys,
    so a report's series and its totals cannot bucket differently.

    Sums are taken per currency and converted when `primary_currency` is
    given; without it the caller is responsible for currency homogeneity,
    as the single-currency tests are.
    """
    from app.models.transaction_split import TransactionSplit

    date_col = _reporting_date_expr(use_effective_date)
    labels = [label_expr] if label_expr is not None else []
    result = await session.execute(
        select(
            *labels,
            Transaction.currency,
            func.sum(
                case(
                    (Transaction.type == "credit", TransactionSplit.share_amount),
                    else_=0,
                )
            ),
            func.sum(
                case(
                    (Transaction.type == "debit", TransactionSplit.share_amount),
                    else_=0,
                )
            ),
        )
        .select_from(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            subject_share_match(subject, mine=mine, scope=scope),
            # The base totals a share adjusts are all taken over open
            # accounts, so a share of a closed account's cost would come
            # off a figure that never had it.
            Account.is_closed == False,  # noqa: E712
            Transaction.source != "opening_balance",
            date_col >= start,
            date_col < end,
            date_col <= date.today(),
            Transaction.status == "posted",
            counts_as_user_pnl(),
        )
        .group_by(*labels, Transaction.currency)
    )
    from decimal import Decimal as _Decimal

    from app.services.fx_rate_service import convert as _convert

    out: dict = {}
    for row in result.all():
        label = row[0] if label_expr is not None else None
        currency, raw_credit, raw_debit = row[-3], row[-2], row[-1]
        credit = float(raw_credit or 0)
        debit = float(raw_debit or 0)
        if primary_currency is not None:
            if credit:
                converted, _ = await _convert(
                    session, _Decimal(str(raw_credit)), currency, primary_currency
                )
                credit = float(converted)
            if debit:
                converted, _ = await _convert(
                    session, _Decimal(str(raw_debit)), currency, primary_currency
                )
                debit = float(converted)
        previous = out.get(label, (0.0, 0.0))
        out[label] = (previous[0] + credit, previous[1] + debit)
    return out


async def _share_by_category(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    mine: bool,
    scope: str,
    tx_type: str,
    use_effective_date: bool,
    primary_currency: Optional[str],
    label_expr=None,
) -> dict:
    """{category_id: total} of the matching shares, or
    {(period label, category_id): total} when `label_expr` is given.

    A category id is a uuid, or ``None`` for the uncategorized bucket.
    """
    from app.models.transaction_split import TransactionSplit

    date_col = _reporting_date_expr(use_effective_date)
    labels = [label_expr] if label_expr is not None else []
    result = await session.execute(
        select(
            *labels,
            Transaction.category_id,
            Transaction.currency,
            func.sum(TransactionSplit.share_amount),
        )
        .select_from(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            subject_share_match(subject, mine=mine, scope=scope),
            Account.is_closed == False,  # noqa: E712
            Transaction.type == tx_type,
            Transaction.source != "opening_balance",
            date_col >= start,
            date_col < end,
            date_col <= date.today(),
            Transaction.status == "posted",
            counts_as_user_pnl(),
        )
        .group_by(*labels, Transaction.category_id, Transaction.currency)
    )
    from decimal import Decimal as _Decimal

    from app.services.fx_rate_service import convert as _convert

    out: dict = {}
    for row in result.all():
        category_id, currency, total = row[-3], row[-2], row[-1]
        if not total:
            continue
        key = (row[0], category_id) if label_expr is not None else category_id
        amount = float(total)
        if primary_currency is not None:
            converted, _ = await _convert(
                session, _Decimal(str(total)), currency, primary_currency
            )
            amount = float(converted)
        out[key] = out.get(key, 0.0) + amount
    return out


async def foreign_shares_pnl(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
) -> tuple[float, float]:
    """(income offset, expense offset) — what to *subtract* from the
    subject's own full-amount aggregation.

    Every share on a transaction inside the subject's scope that belongs
    to somebody else. Subtracting it leaves the subject carrying only
    their part of what their own accounts paid or received.
    """
    buckets = await _share_pnl(
        session,
        subject,
        start,
        end,
        mine=False,
        scope="in",
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
    )
    return buckets.get(None, (0.0, 0.0))


async def foreign_shares_pnl_by_period(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    label_expr,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
) -> dict:
    """`foreign_shares_pnl`, one bucket per period of the caller's own
    label expression."""
    return await _share_pnl(
        session,
        subject,
        start,
        end,
        mine=False,
        scope="in",
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )


async def foreign_shares_by_category(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    label_expr=None,
    tx_type: str = "debit",
) -> dict:
    """Per category, the shares inside the subject's scope that belong
    to somebody else — subtract from the full amounts. `tx_type` picks
    the debit side (what a category cost) or the credit side (what came
    back into it)."""
    return await _share_by_category(
        session,
        subject,
        start,
        end,
        mine=False,
        scope="in",
        tx_type=tx_type,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )


async def subject_shares_pnl(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
) -> tuple[float, float]:
    """(income, expense) the subject carries on transactions outside
    their own scope — my share of the groceries my partner paid, or of
    the concert tickets a friend in another workspace paid."""
    buckets = await _share_pnl(
        session,
        subject,
        start,
        end,
        mine=True,
        scope="out",
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
    )
    return buckets.get(None, (0.0, 0.0))


async def subject_shares_pnl_by_period(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    label_expr,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
) -> dict:
    """`subject_shares_pnl`, one bucket per period of the caller's own
    label expression."""
    return await _share_pnl(
        session,
        subject,
        start,
        end,
        mine=True,
        scope="out",
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )


async def subject_shares_by_category(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    label_expr=None,
    tx_type: str = "debit",
) -> dict:
    """Per category, the shares the subject carries on transactions
    outside their own scope — add to the full amounts."""
    return await _share_by_category(
        session,
        subject,
        start,
        end,
        mine=True,
        scope="out",
        tx_type=tx_type,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )


async def subject_cost_by_category(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    label_expr=None,
) -> dict:
    """What each category cost the subject: the debits in their scope,
    less the shares of those somebody else carries, plus the shares they
    carry of debits outside it.

    The canonical answer to "does this category have costs here", which
    is what tells an expense category from an income one — nothing on a
    category says so. Each page's own breakdown is built from its own
    base query, so this is not what a page *displays*; it is the one
    figure the credit term is measured against, so that the same
    judgement is made on the pie and on the card above it.
    """
    date_col = _reporting_date_expr(use_effective_date)
    labels = [label_expr] if label_expr is not None else []
    result = await session.execute(
        select(
            *labels,
            Transaction.category_id,
            func.sum(func.coalesce(Transaction.amount_primary, Transaction.amount)),
        )
        .select_from(Transaction)
        .join(Account, Transaction.account_id == Account.id)
        .where(
            *in_subject_scope(subject),
            Account.is_closed == False,  # noqa: E712
            Transaction.type == "debit",
            Transaction.source != "opening_balance",
            date_col >= start,
            date_col < end,
            date_col <= date.today(),
            Transaction.status == "posted",
            counts_as_user_pnl(),
        )
        .group_by(*labels, Transaction.category_id)
    )
    costs: dict = {}
    for row in result.all():
        key = (row[0], row[-2]) if label_expr is not None else row[-2]
        costs[key] = costs.get(key, 0.0) + abs(float(row[-1] or 0))

    foreign = await foreign_shares_by_category(
        session, subject, start, end,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )
    for key, amount in foreign.items():
        costs[key] = costs.get(key, 0.0) - amount
    carried = await subject_shares_by_category(
        session, subject, start, end,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )
    for key, amount in carried.items():
        costs[key] = costs.get(key, 0.0) + amount
    return costs


async def subject_credit_offsets(
    session: AsyncSession,
    subject: ConsumptionSubject,
    start: date,
    end: date,
    *,
    use_effective_date: bool = False,
    primary_currency: Optional[str] = None,
    label_expr=None,
) -> dict:
    """Per category, what the subject was given back of what they spent.

    A share is signed: a debit costs, a credit gives back. The subject's
    share of a shared credit therefore lowers the category it was booked
    to, so a shared purchase and its shared refund leave that category at
    zero — and lowers the expense total by the same amount, instead of
    raising income, so the breakdown still adds up to the card above it.

    Two rules make that safe. It applies **only to a category that cost
    the subject something in the same period and scope**, because nothing
    on a category says whether it is an expense one or an income one and
    a credit in an income category is shared income, not a refund. And it
    is **clamped to that cost**, so a category can reach zero and stop:
    an unclamped subtraction would show a negative category, or a total
    that no longer matched the lines under it.

    Scope is both sides of the subject's own: a refund of a cost they
    carry is theirs whether it landed on their account or on the account
    of the member who paid.
    """
    credits_ = await _share_by_category(
        session,
        subject,
        start,
        end,
        mine=True,
        scope="any",
        tx_type="credit",
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )
    if not credits_:
        return {}
    costs = await subject_cost_by_category(
        session, subject, start, end,
        use_effective_date=use_effective_date,
        primary_currency=primary_currency,
        label_expr=label_expr,
    )
    offsets: dict = {}
    for key, amount in credits_.items():
        cost = costs.get(key, 0.0)
        if cost <= 0:
            continue
        offsets[key] = min(amount, cost)
    return offsets
