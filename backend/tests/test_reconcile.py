"""The matching engine. These are the tests that protect the user's data.

A false match silently swallows a real expense; a false proposal duplicates one.
Both are invisible without tests, so the edges are covered deliberately.
"""
import datetime

import pytest

from models import (
    STAGED_AMBIGUOUS,
    STAGED_IGNORED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    StagedTransaction,
    Transaction,
    db,
)
from services import reconcile

BASE = 'https://localhost'
USER = '__pytest_reconcile_user__'

DAY = datetime.date(2026, 8, 12)


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def add_txn(uid, amount, day=DAY, description='', type_='expense',
            currency='ILS', category='Groceries'):
    txn = Transaction(
        user_id=uid, type=type_, category=category, amount=amount,
        date=day, description=description, currency=currency,
        exchange_rate=1.0,
    )
    db.session.add(txn)
    db.session.flush()
    return txn


def staged(uid, amount, day=DAY, merchant='שופרסל דיל', type_='expense',
           currency='ILS', **over):
    row = StagedTransaction(
        user_id=uid, source='upload', amount=amount, txn_date=day,
        merchant_raw=merchant, merchant_clean=merchant, type=type_,
        currency=currency,
        dedup_hash=over.pop('dedup_hash', None) or reconcile.compute_dedup_hash(
            'upload', None, over.pop('external_id', None), day, amount,
            currency, merchant,
        ),
        **over,
    )
    db.session.add(row)
    db.session.flush()
    return row


# --- the product: a missed expense ---------------------------------------

def test_a_charge_with_no_counterpart_is_proposed(app, uid):
    with app.app_context():
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        assert row.matched_txn_id is None
        db.session.rollback()


def test_an_exact_counterpart_is_matched(app, uid):
    with app.app_context():
        txn = add_txn(uid, 52.90, description='שופרסל דיל')
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_MATCHED
        assert row.matched_txn_id == txn.id
        assert row.match_score >= reconcile.MATCH_THRESHOLD
        db.session.rollback()


def test_posting_date_drift_still_matches(app, uid):
    """The card's purchase date and the tracked date routinely differ."""
    with app.app_context():
        txn = add_txn(uid, 52.90, day=DAY - datetime.timedelta(days=3),
                      description='שופרסל דיל')
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_MATCHED
        assert row.matched_txn_id == txn.id
        db.session.rollback()


def test_drift_beyond_the_window_is_proposed(app, uid):
    with app.app_context():
        add_txn(uid, 52.90, day=DAY - datetime.timedelta(days=30),
                description='שופרסל דיל')
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


def test_a_different_amount_is_not_a_match(app, uid):
    with app.app_context():
        add_txn(uid, 100.00, description='שופרסל דיל')
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


def test_income_and_expense_do_not_match_each_other(app, uid):
    with app.app_context():
        add_txn(uid, 52.90, description='שופרסל דיל', type_='income')
        row = staged(uid, 52.90, type_='expense')
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


def test_another_users_transaction_is_never_a_candidate(app, uid, make_user):
    other = make_user('__pytest_reconcile_other__')
    with app.app_context():
        add_txn(other, 52.90, description='שופרסל דיל')
        row = staged(uid, 52.90)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


# --- never guess between two plausible matches ---------------------------

def test_two_identical_charges_the_same_day_are_ambiguous(app, uid):
    """Two ₪50 coffees. Guessing either would corrupt the data invisibly."""
    with app.app_context():
        a = add_txn(uid, 50.00, description='קפה')
        b = add_txn(uid, 50.00, description='קפה')
        row = staged(uid, 50.00, merchant='קפה')
        reconcile.reconcile_row(row)

        assert row.state == STAGED_AMBIGUOUS
        assert row.matched_txn_id is None
        assert set(row.candidate_ids) == {a.id, b.id}
        db.session.rollback()


def test_a_clear_winner_is_not_ambiguous(app, uid):
    with app.app_context():
        good = add_txn(uid, 50.00, description='שופרסל דיל')
        add_txn(uid, 50.00, day=DAY - datetime.timedelta(days=4),
                description='something entirely different')
        row = staged(uid, 50.00, merchant='שופרסל דיל')
        reconcile.reconcile_row(row)

        assert row.state == STAGED_MATCHED
        assert row.matched_txn_id == good.id
        db.session.rollback()


def test_one_tracked_transaction_cannot_satisfy_two_staged_rows(app, uid):
    """A genuine second charge must still be proposed, not matched away."""
    with app.app_context():
        txn = add_txn(uid, 50.00, description='שופרסל דיל')
        first = staged(uid, 50.00, dedup_hash='h1')
        second = staged(uid, 50.00, dedup_hash='h2')

        claimed = set()
        reconcile.reconcile_row(first, claimed_ids=claimed)
        reconcile.reconcile_row(second, claimed_ids=claimed)

        assert first.state == STAGED_MATCHED
        assert first.matched_txn_id == txn.id
        assert second.state == STAGED_PROPOSED
        db.session.rollback()


# --- the double-count guard ----------------------------------------------

@pytest.mark.parametrize('description', [
    'מקס 3,214.00', 'MAX', 'ישראכרט חיוב חודשי', 'isracard',
    'כאל', 'VISA CAL', 'לאומי קארד', 'American Express',
])
def test_bank_card_settlement_lines_are_suppressed(app, uid, description):
    """One bank line is the same money as ~40 card rows."""
    with app.app_context():
        row = staged(uid, 3214.00, merchant=description)
        reconcile.reconcile_row(row, profile_kind='bank')
        assert row.state == STAGED_IGNORED
        assert row.ignored_reason == reconcile.IGNORED_CARD_AGGREGATE
        db.session.rollback()


def test_the_same_line_on_a_card_statement_is_not_suppressed(app, uid):
    """A purchase at a shop called MAX is not a settlement line."""
    with app.app_context():
        row = staged(uid, 3214.00, merchant='מקס')
        reconcile.reconcile_row(row, profile_kind='card')
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


def test_pending_authorisations_are_suppressed(app, uid):
    """Pre-auth holds change amount or vanish, then reappear as completed."""
    with app.app_context():
        row = staged(uid, 1.00, issuer_status='pending')
        reconcile.reconcile_row(row)
        assert row.state == STAGED_IGNORED
        assert row.ignored_reason == reconcile.IGNORED_PENDING
        db.session.rollback()


def test_a_custom_exclude_pattern_is_honoured(app, uid):
    with app.app_context():
        row = staged(uid, 500.00, merchant='הוראת קבע ועד בית')
        reconcile.reconcile_row(
            row, profile_kind='bank', exclude_patterns=[r'ועד\s*בית']
        )
        assert row.state == STAGED_IGNORED
        db.session.rollback()


def test_a_broken_user_pattern_does_not_break_the_run(app, uid):
    with app.app_context():
        row = staged(uid, 10.00)
        reconcile.reconcile_row(
            row, profile_kind='bank', exclude_patterns=['[unclosed']
        )
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


# --- foreign currency ----------------------------------------------------

def test_foreign_currency_tolerates_a_rate_difference(app, uid):
    """The app's exchange_rate will not equal the issuer's."""
    with app.app_context():
        txn = add_txn(uid, 100.00, description='amazon', currency='USD')
        row = staged(uid, 101.50, merchant='amazon', currency='USD')
        reconcile.reconcile_row(row)
        assert row.state == STAGED_MATCHED
        assert row.matched_txn_id == txn.id
        db.session.rollback()


def test_shekel_amounts_are_matched_exactly(app, uid):
    """No rate is involved in ILS, so a 1.50 gap is a different charge."""
    with app.app_context():
        add_txn(uid, 100.00, description='שופרסל דיל')
        row = staged(uid, 101.50)
        reconcile.reconcile_row(row)
        assert row.state == STAGED_PROPOSED
        db.session.rollback()


# --- dedup hashing -------------------------------------------------------

def test_the_issuer_id_drives_identity_when_present():
    a = reconcile.compute_dedup_hash('upload', 1, 'ABC123', DAY, 10, 'ILS', 'x')
    b = reconcile.compute_dedup_hash('upload', 1, 'ABC123', DAY, 99, 'ILS', 'y')
    assert a == b  # same issuer row, however the rest is described


def test_without_an_issuer_id_the_shape_drives_identity():
    a = reconcile.compute_dedup_hash('wallet', None, None, DAY, 52.9, 'ILS',
                                     'שופרסל דיל רמת אביב')
    b = reconcile.compute_dedup_hash('wallet', None, None, DAY, 52.9, 'ILS',
                                     'שופרסל  דיל  רמת אביב')
    c = reconcile.compute_dedup_hash('wallet', None, None, DAY, 52.9, 'ILS',
                                     'רמי לוי')
    assert a == b  # whitespace is not identity
    assert a != c


def test_the_unique_constraint_makes_reingest_a_no_op(app, uid):
    """The idempotency guarantee, enforced by the database rather than by code."""
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        staged(uid, 52.90, dedup_hash='same-hash')
        db.session.commit()

        # Raised on write, so the second row can never reach the table.
        with pytest.raises(IntegrityError):
            staged(uid, 52.90, dedup_hash='same-hash')
        db.session.rollback()


# --- the batch entry point (what Phase 4 will call) ----------------------

def test_batch_counts_every_verdict(app, uid):
    with app.app_context():
        add_txn(uid, 10.00, description='שופרסל דיל')

        rows = [
            staged(uid, 10.00, dedup_hash='b-matched'),
            staged(uid, 77.00, dedup_hash='b-proposed', merchant='רמי לוי'),
            staged(uid, 1.00, dedup_hash='b-pending', issuer_status='pending'),
            staged(uid, 3214.00, dedup_hash='b-agg', merchant='מקס'),
        ]
        counts = reconcile.reconcile_batch(rows, profile_kind='bank')

        assert counts == {
            'received': 4, 'matched': 1, 'proposed': 1,
            'ambiguous': 0, 'suppressed': 2,
        }
        db.session.rollback()


def test_batch_fills_in_suggestions_only_for_reviewable_rows(app, uid):
    """A matched row needs no suggestion; nobody is going to be asked about it."""
    from models import Category

    with app.app_context():
        db.session.add(Category(user_id=uid, name='zzGroceries',
                                type='expense'))
        add_txn(uid, 10.00, description='שופרסל דיל')

        matched = staged(uid, 10.00, dedup_hash='s-matched')
        proposed = staged(uid, 77.00, dedup_hash='s-proposed',
                          merchant='שופרסל דיל רמת אביב 442')
        reconcile.reconcile_batch([matched, proposed])

        assert matched.state == STAGED_MATCHED
        assert matched.suggested_category is None

        assert proposed.state == STAGED_PROPOSED
        assert proposed.suggested_category == 'zzGroceries'
        assert proposed.merchant_clean == 'שופרסל דיל רמת אביב'
        assert proposed.category_confidence == 0.6
        db.session.rollback()
