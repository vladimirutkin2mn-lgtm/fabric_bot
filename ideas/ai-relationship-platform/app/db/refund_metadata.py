"""Normalize legacy ORM metadata for purchase-refund ledger support.

The original credit column used ``unique=True`` before purchase refunds existed. The
migration replaces that global constraint with a purchase-only partial index. This helper
keeps ``Base.metadata.create_all`` test schemas identical to the migrated PostgreSQL schema.
"""

from sqlalchemy import Index, UniqueConstraint, text

from app.db.models import CreditTransaction, RefundRequest


def configure_refund_metadata() -> None:
    credit_table = CreditTransaction.__table__
    payment_order_column = credit_table.c.payment_order_id
    payment_order_column.unique = False
    for constraint in tuple(credit_table.constraints):
        if isinstance(constraint, UniqueConstraint) and tuple(
            column.name for column in constraint.columns
        ) == ("payment_order_id",):
            credit_table.constraints.remove(constraint)
    if "uq_credit_transactions_purchase_order" not in {
        index.name for index in credit_table.indexes
    }:
        Index(
            "uq_credit_transactions_purchase_order",
            payment_order_column,
            unique=True,
            postgresql_where=text("type = 'purchase'"),
        )

    refund_table = RefundRequest.__table__
    if "ix_refund_requests_payment_order_id" not in {index.name for index in refund_table.indexes}:
        Index(
            "ix_refund_requests_payment_order_id",
            refund_table.c.payment_order_id,
        )
