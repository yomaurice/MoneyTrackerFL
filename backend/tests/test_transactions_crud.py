"""The original add / edit routes and the years list, after the Phase 7 fixes."""
import datetime

import pytest

from models import Transaction, db

BASE = 'https://localhost'
USER = '__pytest_crud_user__'
OTHER = '__pytest_crud_other__'

GOOD = {
    'type': 'expense', 'category': 'zzFood', 'amount': 52.9,
    'date': '2026-04-03', 'description': 'groceries',
}


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def add(client, **over):
    return client.post('/api/transactions', json={**GOOD, **over},
                       base_url=BASE)


# --- add -------------------------------------------------------------------

def test_a_valid_transaction_is_added(app, uid, login):
    res = add(login(USER))
    assert res.status_code == 201
    with app.app_context():
        tx = Transaction.query.filter_by(user_id=uid).one()
        assert (tx.amount, tx.date) == (52.9, datetime.date(2026, 4, 3))
        assert tx.currency == 'ILS'


@pytest.mark.parametrize('over', [
    {'type': 'gift'},
    {'amount': 'lots'},
    {'amount': -5},
    {'date': 'yesterday'},
    {'category': ''},
])
def test_bad_input_is_a_400_not_a_500(app, uid, login, over):
    res = add(login(USER), **over)
    assert res.status_code == 400
    assert res.get_json()['error']


def test_missing_fields_are_a_400_not_a_500(app, uid, login):
    client = login(USER)
    assert client.post('/api/transactions', json={'type': 'expense'},
                       base_url=BASE).status_code == 400
    assert client.post('/api/transactions', data='not json',
                       base_url=BASE).status_code == 400


def test_recurring_creates_one_row_per_month(app, uid, login):
    res = add(login(USER), is_recurring=True, recurrence_months=3)
    assert res.status_code == 201
    assert len(res.get_json()['ids']) == 3
    with app.app_context():
        dates = sorted(t.date for t in Transaction.query.filter_by(user_id=uid))
        assert dates == [datetime.date(2026, 4, 3), datetime.date(2026, 5, 3),
                         datetime.date(2026, 6, 3)]


def test_recurrence_is_capped(app, uid, login):
    res = add(login(USER), is_recurring=True, recurrence_months=100000)
    assert res.status_code == 400
    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 0


# --- edit ------------------------------------------------------------------

def _existing(app, uid, currency='USD'):
    with app.app_context():
        tx = Transaction(user_id=uid, type='expense', category='zzFood',
                         amount=10, date=datetime.date(2026, 4, 1),
                         description='x', currency=currency, exchange_rate=3.7)
        db.session.add(tx)
        db.session.commit()
        return tx.id


def test_an_edit_keeps_currency_it_was_not_given(app, uid, login):
    tx_id = _existing(app, uid)
    res = login(USER).put(f'/api/transactions/{tx_id}', json=GOOD, base_url=BASE)
    assert res.status_code == 200
    with app.app_context():
        tx = db.session.get(Transaction, tx_id)
        assert (tx.amount, tx.currency, tx.exchange_rate) == (52.9, 'USD', 3.7)


def test_a_bad_edit_is_a_400_and_changes_nothing(app, uid, login):
    tx_id = _existing(app, uid)
    res = login(USER).put(f'/api/transactions/{tx_id}',
                          json={**GOOD, 'amount': 'lots'}, base_url=BASE)
    assert res.status_code == 400
    with app.app_context():
        assert db.session.get(Transaction, tx_id).amount == 10


def test_another_users_transaction_cannot_be_edited(app, uid, login, make_user):
    other = make_user(OTHER)
    tx_id = _existing(app, other)
    res = login(USER).put(f'/api/transactions/{tx_id}', json=GOOD, base_url=BASE)
    assert res.status_code == 404


# --- years -----------------------------------------------------------------

def test_years_lists_only_the_callers_data(app, uid, login, make_user):
    other = make_user(OTHER)
    with app.app_context():
        for user, year in ((uid, 2024), (uid, 2026), (other, 2019)):
            db.session.add(Transaction(
                user_id=user, type='expense', category='zzFood', amount=1,
                date=datetime.date(year, 1, 1), currency='ILS',
                exchange_rate=1.0,
            ))
        db.session.commit()

    body = login(USER).get('/api/analytics/years', base_url=BASE).get_json()
    assert body['years'] == [2024, 2026]


def test_years_needs_a_session(app):
    client = app.test_client()
    assert client.get('/api/analytics/years', base_url=BASE).status_code == 401
    # The old unauthenticated path is gone.
    assert client.get('/years', base_url=BASE).status_code == 404
