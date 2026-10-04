"""Wallet capture: the background send and the /add page's save converge."""
import datetime

import pytest

from models import (
    STAGED_CONFIRMED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    StagedTransaction,
    Transaction,
    db,
)

BASE = 'https://localhost'
USER = '__pytest_wallet_user__'
OTHER = '__pytest_wallet_other__'

TS = '1790000000'  # 2026-09-21, Asia/Jerusalem
NOTE = {'title': 'שופרסל דיל', 'text': '₪52.90 עם Visa •••• 1234', 'ts': TS}
FORM = {'type': 'expense', 'category': 'zzGroceries', 'amount': 52.9,
        'date': '2026-09-21', 'description': 'weekly shop', 'currency': 'ILS'}


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def token_for(login, username=USER):
    client = login(username)
    res = client.post('/api/tokens', json={'name': 'phone'}, base_url=BASE)
    assert res.status_code == 201
    return res.get_json()['token']


def send(app, token, body=NOTE):
    return app.test_client().post(
        '/api/ingest/wallet', json=body, base_url=BASE,
        headers={'Authorization': f'Bearer {token}'},
    )


# --- background send -------------------------------------------------------

def test_a_notification_is_staged_for_review(app, uid, login):
    res = send(app, token_for(login))
    assert res.status_code == 201
    assert res.get_json()['parsed'] is True

    with app.app_context():
        row = StagedTransaction.query.filter_by(user_id=uid).one()
        assert (row.source, row.amount, row.merchant_raw) == (
            'wallet', 52.9, 'שופרסל דיל')
        assert row.txn_date == datetime.date(2026, 9, 21)
        assert row.state == STAGED_PROPOSED


def test_retries_land_on_one_row(app, uid, login):
    token = token_for(login)
    assert send(app, token).status_code == 201
    assert send(app, token).status_code == 200
    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 1


def test_two_identical_payments_are_two_rows(app, uid, login):
    """Same coffee twice: different notification times, two charges."""
    token = token_for(login)
    send(app, token)
    send(app, token, {**NOTE, 'ts': '1790000600'})
    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 2


def test_an_unreadable_notification_is_kept_with_its_text(app, uid, login):
    send(app, token_for(login), {'title': 'Google Wallet',
                                 'text': 'Something new', 'ts': TS})
    with app.app_context():
        row = StagedTransaction.query.filter_by(user_id=uid).one()
        assert row.amount is None
        assert row.raw['cells'] == ['Google Wallet', 'Something new']


def test_the_send_needs_a_live_token(app, uid, login):
    assert send(app, 'not-a-token').status_code == 401
    res = app.test_client().post('/api/ingest/wallet', json=NOTE,
                                 base_url=BASE)
    assert res.status_code == 401


def test_a_session_cookie_is_not_enough_for_the_send(app, uid, login):
    res = login(USER).post('/api/ingest/wallet', json=NOTE, base_url=BASE)
    assert res.status_code == 401


def test_an_empty_notification_is_refused(app, uid, login):
    assert send(app, token_for(login), {'ts': TS}).status_code == 400


# --- /add save -------------------------------------------------------------

def confirm(client, **over):
    return client.post('/api/ingest/wallet/confirm',
                       json={**NOTE, **FORM, **over}, base_url=BASE)


def test_saving_creates_the_transaction_and_closes_the_staged_row(app, uid,
                                                                  login):
    res = confirm(login(USER))
    assert res.status_code == 201
    txn_id = res.get_json()['transaction_id']
    with app.app_context():
        txn = db.session.get(Transaction, txn_id)
        assert (txn.amount, txn.description) == (52.9, 'weekly shop')
        row = StagedTransaction.query.filter_by(user_id=uid).one()
        assert row.state == STAGED_CONFIRMED
        assert row.created_txn_id == txn_id


def test_the_send_and_the_save_converge_either_order(app, uid, login):
    token = token_for(login)
    client = login(USER)

    send(app, token)
    assert confirm(client).status_code == 201
    # A late retry of the send changes nothing.
    assert send(app, token).status_code == 200

    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 1
        assert Transaction.query.filter_by(user_id=uid).count() == 1


def test_saving_twice_saves_once(app, uid, login):
    """The page's offline queue may resend a save that did land."""
    client = login(USER)
    assert confirm(client).status_code == 201
    second = confirm(client)
    assert second.status_code == 200
    assert second.get_json()['already'] is True
    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 1


def test_a_charge_already_typed_in_by_hand_asks_first(app, uid, login):
    with app.app_context():
        db.session.add(Transaction(
            user_id=uid, type='expense', category='zzGroceries', amount=52.9,
            date=datetime.date(2026, 9, 21), description='supermarket',
            currency='ILS', exchange_rate=1.0,
        ))
        db.session.commit()

    client = login(USER)
    res = confirm(client)
    assert res.status_code == 409
    assert res.get_json()['matched']['description'] == 'supermarket'

    assert confirm(client, force=True).status_code == 201
    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 2


def test_saving_teaches_the_merchants_category(app, uid, login):
    client = login(USER)
    confirm(client)
    body = client.get('/api/review/suggest',
                      query_string={'merchant': 'שופרסל דיל'},
                      base_url=BASE).get_json()
    assert body['category'] == 'zzGroceries'


def test_a_bad_form_is_a_400_and_saves_nothing(app, uid, login):
    assert confirm(login(USER), amount='lots').status_code == 400
    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 0


def test_saving_needs_a_session_not_a_token(app, uid, login):
    token = token_for(login)
    res = app.test_client().post(
        '/api/ingest/wallet/confirm', json={**NOTE, **FORM}, base_url=BASE,
        headers={'Authorization': f'Bearer {token}'},
    )
    assert res.status_code == 401


def test_rows_stay_with_their_owner(app, uid, login, make_user):
    make_user(OTHER)
    send(app, token_for(login, OTHER))
    # The same notification saved by another user is theirs alone.
    assert confirm(login(USER)).status_code == 201
    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 1
