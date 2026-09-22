"""One transaction, one contribution: unique indexes on both link columns

A contribution is the real bank transaction, linked from
`group_settlements` on the payer side (`transaction_id`) or the receiver
side (`receiver_transaction_id`). Linking one transaction from two
settlements would count the same money twice, so each column gets a
unique index in place of the plain ones migrations 044 and 045 created.
NULL repeats freely in a unique index on both PostgreSQL and SQLite, so
settlements with an unlinked side are untouched.

The cross-column case — one transaction on the payer side of one
settlement and the receiver side of another — is not expressible as a
single index and is enforced in `settlement_service`. It is still checked
here, because letting it through would leave the database in a state the
service refuses to create.

Existing data is inspected before anything is created. Rows that violate
the rule are reported by id and the migration stops: the duplicates are a
double-counted debt, and which of the two should keep the link is the
user's call, not this script's. Nothing is deleted or rewritten.

Revision ID: 091
Revises: 090
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "091"
down_revision: Union[str, None] = "090"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SAME_COLUMN = sa.text(
    """
    SELECT {column} AS transaction_id, id AS settlement_id
    FROM group_settlements
    WHERE {column} IS NOT NULL
      AND {column} IN (
          SELECT {column} FROM group_settlements
          WHERE {column} IS NOT NULL
          GROUP BY {column} HAVING COUNT(*) > 1
      )
    ORDER BY {column}, id
    """
)

_CROSS_COLUMN = sa.text(
    """
    SELECT payer.transaction_id AS transaction_id,
           payer.id AS payer_settlement_id,
           receiver.id AS receiver_settlement_id
    FROM group_settlements AS payer
    JOIN group_settlements AS receiver
      ON receiver.receiver_transaction_id = payer.transaction_id
    WHERE payer.transaction_id IS NOT NULL
    ORDER BY payer.transaction_id, payer.id
    """
)


def _violations(bind) -> list[str]:
    problems: list[str] = []

    for column, side in (
        ("transaction_id", "payer side"),
        ("receiver_transaction_id", "receiver side"),
    ):
        statement = sa.text(str(_SAME_COLUMN).format(column=column))
        by_transaction: dict[str, list[str]] = {}
        for row in bind.execute(statement):
            by_transaction.setdefault(str(row.transaction_id), []).append(
                str(row.settlement_id)
            )
        for transaction_id, settlement_ids in by_transaction.items():
            problems.append(
                f"transaction {transaction_id} is linked on the {side} by "
                f"{len(settlement_ids)} settlements: {', '.join(settlement_ids)}"
            )

    for row in bind.execute(_CROSS_COLUMN):
        problems.append(
            f"transaction {row.transaction_id} is linked on the payer side by "
            f"settlement {row.payer_settlement_id} and on the receiver side by "
            f"settlement {row.receiver_settlement_id}"
        )

    return problems


def _drop_if_exists(bind, name: str) -> None:
    inspector = sa.inspect(bind)
    existing = {index["name"] for index in inspector.get_indexes("group_settlements")}
    if name in existing:
        op.drop_index(name, table_name="group_settlements")


def upgrade() -> None:
    bind = op.get_bind()

    problems = _violations(bind)
    if problems:
        raise RuntimeError(
            "group_settlements already links a transaction from more than one "
            "contribution, so the unique indexes cannot be created. Nothing "
            "has been changed. Decide which settlement keeps each link, clear "
            "the other, and run the migration again.\n  - "
            + "\n  - ".join(problems)
        )

    _drop_if_exists(bind, "ix_group_settlements_transaction_id")
    _drop_if_exists(bind, "ix_group_settlements_receiver_transaction_id")

    op.create_index(
        "uq_group_settlements_transaction_id",
        "group_settlements",
        ["transaction_id"],
        unique=True,
    )
    op.create_index(
        "uq_group_settlements_receiver_transaction_id",
        "group_settlements",
        ["receiver_transaction_id"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()

    _drop_if_exists(bind, "uq_group_settlements_transaction_id")
    _drop_if_exists(bind, "uq_group_settlements_receiver_transaction_id")

    op.create_index(
        "ix_group_settlements_transaction_id",
        "group_settlements",
        ["transaction_id"],
    )
    op.create_index(
        "ix_group_settlements_receiver_transaction_id",
        "group_settlements",
        ["receiver_transaction_id"],
    )
