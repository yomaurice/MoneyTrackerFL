"""The review queue endpoints: the only path from staged data to real rows."""
import datetime

import pytest

from models import (
    STAGED_AMBIGUOUS,
    STAGED_CONFIRMED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    STAGED_SKIPPED,
    Category,
    MerchantRule,
    StagedTransaction,
    Transaction,
    db,
)

BASE = 'https://localhost'
USER = '__pytest_review_user__'
OTHER = '__pytest_review_other__'
DAY = datetime.date(2026, 8, 12)


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def stage(uid, amount=52.90, state=STAGED_PROPOSED, merchant='שופרסל דיל',
          day=DAY, dedup=None, **over):
    row = StagedTransaction(
        user_id=uid, source='upload', amount=amount, txn_date=day,
        merchant_raw=merchant, merchant_clean=merchant, type='expense',
        currency='ILS', state=state,
        dedup_hash=dedup or f'{uid}-{amount}-{merchant}-{state}-{day}',
        suggested_category=over.pop('suggested_category', 'zzGroceries'),
        suggested_description=over.pop('suggested_description', merchant),
        **over,
    )
    db.session.add(row)
    db.session.commit()
    return row.id


# --- queue ---------------------------------------------------------------

def test_queue_returns_proposals_with_position_and_total(app, uid, login):
    with app.app_context():
        stage(uid, 10.0, dedup='a', day=DAY)
        stage(uid, 20.0, dedup='b', day=DAY - datetime.timedelta(days=1))
        stage(uid, 30.0, dedup='c', state=STAGED_AMBIGUOUS)

    body = login(USER).get('/api/review/queue', base_url=BASE).get_json()

    assert body['total'] == 3
    assert [i['position'] for i in body['items']] == [1, 2, 3]
    assert all(i['total'] == 3 for i in body['items'])
    # Oldest charge first, so review reads chronologically.
    assert body['items'][0]['amount'] == 20.0
    assert body['ambiguous'] == 1


def test_queue_hides_states_that_are_not_awaiting_review(app, uid, login):
    with app.app_context():
        stage(uid, 10.0, dedup='m', state=STAGED_MATCHED)
        stage(uid, 20.0, dedup='s', state=STAGED_SKIPPED)
        stage(uid, 30.0, dedup='c', state=STAGED_CONFIRMED)

    body = login(USER).get('/api/review/queue', base_url=BASE).get_json()
    assert body['total'] == 0


def test_queue_is_scoped_to_the_caller(app, uid, login, make_user):
    other = make_user(OTHER)
    with app.app_context():
        stage(other, 10.0, dedup='other')

    assert login(USER).get(
        '/api/review/queue', base_url=BASE
    ).get_json()['total'] == 0


def test_queue_needs_a_session(app):
    assert app.test_client().get(
        '/api/review/queue', base_url=BASE
    ).status_code == 401


# --- confirm -------------------------------------------------------------

def test_confirm_creates_transactions_and_links_them(app, uid, login):
    with app.app_context():
        sid = stage(uid)

    res = login(USER).post(
        '/api/review/confirm',
        json={'items': [{'id': sid, 'category': 'zzGroceries'}]},
        base_url=BASE,
    )

    assert res.status_code == 201, res.data
    with app.app_context():
        row = db.session.get(StagedTransaction, sid)
        assert row.state == STAGED_CONFIRMED
        assert row.created_txn_id is not None
        txn = db.session.get(Transaction, row.created_txn_id)
        assert txn.amount == 52.90
        assert txn.category == 'zzGroceries'
        assert txn.user_id == uid


def test_confirm_uses_the_users_edits_over_the_suggestion(app, uid, login):
    """The whole point of review is that the suggestion may be wrong."""
    with app.app_context():
        sid = stage(uid, suggested_category='zzWrong',
                    suggested_description='wrong')

    login(USER).post(
        '/api/review/confirm',
        json={'items': [{
            'id': sid, 'category': 'zzRight', 'description': 'corrected',
            'amount': 99.5, 'date': '2026-08-01',
        }]},
        base_url=BASE,
    )

    with app.app_context():
        row = db.session.get(StagedTransaction, sid)
        txn = db.session.get(Transaction, row.created_txn_id)
        assert txn.category == 'zzRight'
        assert txn.description == 'corrected'
        assert txn.amount == 99.5
        assert str(txn.date) == '2026-08-01'


def test_confirming_twice_is_refused(app, uid, login):
    """Re-confirming would insert the transaction twice."""
    with app.app_context():
        sid = stage(uid)

    client = login(USER)
    first = client.post('/api/review/confirm',
                        json={'items': [{'id': sid}]}, base_url=BASE)
    second = client.post('/api/review/confirm',
                         json={'items': [{'id': sid}]}, base_url=BASE)

    assert first.status_code == 201
    assert second.status_code == 409
    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 1


def test_confirm_rejects_the_batch_if_any_row_is_invalid(app, uid, login):
    with app.app_context():
        good = stage(uid, 10.0, dedup='g')
        bad = stage(uid, 20.0, dedup='b', suggested_category=None)

    res = login(USER).post(
        '/api/review/confirm',
        json={'items': [{'id': good}, {'id': bad, 'category': ''}]},
        base_url=BASE,
    )

    assert res.status_code == 400
    assert res.get_json()['errors'][0]['id'] == bad
    with app.app_context():
        # Nothing landed, so the caller can retry the whole batch safely.
        assert Transaction.query.filter_by(user_id=uid).count() == 0
        assert db.session.get(StagedTransaction, good).state == STAGED_PROPOSED


def test_confirm_will_not_touch_another_users_rows(app, uid, login, make_user):
    other = make_user(OTHER)
    with app.app_context():
        sid = stage(other, 10.0, dedup='theirs')

    res = login(USER).post('/api/review/confirm',
                           json={'items': [{'id': sid}]}, base_url=BASE)

    assert res.status_code == 404
    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_PROPOSED


def test_confirm_rejects_duplicate_ids_in_one_request(app, uid, login):
    with app.app_context():
        sid = stage(uid)

    res = login(USER).post(
        '/api/review/confirm',
        json={'items': [{'id': sid}, {'id': sid}]},
        base_url=BASE,
    )
    assert res.status_code == 400


def test_confirm_learns_the_merchant_pairing(app, uid, login):
    """What makes the next import better than this one."""
    with app.app_context():
        sid = stage(uid, merchant='פז דלק')

    login(USER).post(
        '/api/review/confirm',
        json={'items': [{'id': sid, 'category': 'zzFuel',
                         'description': 'Petrol'}]},
        base_url=BASE,
    )

    with app.app_context():
        rule = MerchantRule.query.filter_by(user_id=uid).one()
        assert rule.pattern == 'פז דלק'
        assert rule.category == 'zzFuel'
        assert rule.description == 'Petrol'
        assert rule.auto_learned is True


def test_confirm_rejects_empty_and_malformed_bodies(app, uid, login):
    client = login(USER)
    for body in ({'items': []}, {'items': 'x'}, {}, [{'id': 'not-an-int'}]):
        res = client.post('/api/review/confirm', json=body, base_url=BASE)
        assert res.status_code == 400, body


# --- skip ----------------------------------------------------------------

def test_skip_marks_rows_and_removes_them_from_the_queue(app, uid, login):
    with app.app_context():
        sid = stage(uid)

    client = login(USER)
    res = client.post('/api/review/skip', json={'ids': [sid]}, base_url=BASE)

    assert res.status_code == 200
    assert res.get_json()['count'] == 1
    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_SKIPPED
    assert client.get('/api/review/queue',
                      base_url=BASE).get_json()['total'] == 0


def test_skip_accepts_a_single_id(app, uid, login):
    with app.app_context():
        sid = stage(uid)
    res = login(USER).post('/api/review/skip', json={'id': sid},
                           base_url=BASE)
    assert res.status_code == 200


def test_skip_ignores_another_users_rows(app, uid, login, make_user):
    other = make_user(OTHER)
    with app.app_context():
        sid = stage(other, 10.0, dedup='theirs')

    res = login(USER).post('/api/review/skip', json={'ids': [sid]},
                           base_url=BASE)

    assert res.get_json()['count'] == 0
    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_PROPOSED


def test_skip_rejects_malformed_input(app, uid, login):
    client = login(USER)
    for body in ({}, {'ids': []}, {'ids': ['x']}):
        assert client.post('/api/review/skip', json=body,
                           base_url=BASE).status_code == 400


# --- link ----------------------------------------------------------------

def test_link_resolves_an_ambiguous_row(app, uid, login):
    with app.app_context():
        sid = stage(uid, state=STAGED_AMBIGUOUS)
        txn = Transaction(user_id=uid, type='expense', category='zzGroceries',
                          amount=52.90, date=DAY, currency='ILS',
                          exchange_rate=1.0)
        db.session.add(txn)
        db.session.commit()
        txn_id = txn.id

    res = login(USER).post(
        '/api/review/link',
        json={'id': sid, 'transaction_id': txn_id},
        base_url=BASE,
    )

    assert res.status_code == 200
    with app.app_context():
        row = db.session.get(StagedTransaction, sid)
        assert row.state == STAGED_MATCHED
        assert row.matched_txn_id == txn_id
        # A person said so, which outranks the scorer.
        assert row.match_score == 1.0
        assert row.candidate_ids is None


def test_link_will_not_point_at_another_users_transaction(app, uid, login,
                                                          make_user):
    other = make_user(OTHER)
    with app.app_context():
        sid = stage(uid, state=STAGED_AMBIGUOUS)
        txn = Transaction(user_id=other, type='expense', category='x',
                          amount=52.90, date=DAY, currency='ILS',
                          exchange_rate=1.0)
        db.session.add(txn)
        db.session.commit()
        txn_id = txn.id

    res = login(USER).post('/api/review/link',
                           json={'id': sid, 'transaction_id': txn_id},
                           base_url=BASE)

    assert res.status_code == 404
    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_AMBIGUOUS


def test_link_refuses_a_row_already_dealt_with(app, uid, login):
    with app.app_context():
        sid = stage(uid, state=STAGED_CONFIRMED)
        txn = Transaction(user_id=uid, type='expense', category='x',
                          amount=1.0, date=DAY, currency='ILS',
                          exchange_rate=1.0)
        db.session.add(txn)
        db.session.commit()
        txn_id = txn.id

    res = login(USER).post('/api/review/link',
                           json={'id': sid, 'transaction_id': txn_id},
                           base_url=BASE)
    assert res.status_code == 409


def test_link_rejects_malformed_input(app, uid, login):
    client = login(USER)
    for body in ({}, {'id': 1}, {'transaction_id': 1}, {'id': 'a',
                                                        'transaction_id': 'b'}):
        assert client.post('/api/review/link', json=body,
                           base_url=BASE).status_code == 400
