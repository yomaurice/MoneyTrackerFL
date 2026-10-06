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


# --- candidates, already in, back, re-check --------------------------------

def _txn(uid, amount=52.90, day=DAY, description='groceries'):
    txn = Transaction(user_id=uid, type='expense', category='zzGroceries',
                      amount=amount, date=day, description=description,
                      currency='ILS', exchange_rate=1.0)
    db.session.add(txn)
    db.session.commit()
    return txn.id


def test_queue_carries_candidate_details_and_the_raw_line(app, uid, login):
    """The wizard shows "is it this one?" without a second round-trip."""
    with app.app_context():
        txn_id = _txn(uid, description='weekly shop')
        stage(uid, candidate_ids=[txn_id],
              raw={'cells': ['03/04/2026', 'שופרסל דיל', None, '52.90'],
                   'file': 'max.xlsx'})

    item = login(USER).get('/api/review/queue',
                           base_url=BASE).get_json()['items'][0]

    assert item['candidates'][0]['id'] == txn_id
    assert item['candidates'][0]['description'] == 'weekly shop'
    assert item['raw_cells'] == ['03/04/2026', 'שופרסל דיל', '52.90']


def test_already_in_takes_the_row_out_of_the_queue(app, uid, login):
    from models import STAGED_IGNORED

    with app.app_context():
        sid = stage(uid)

    client = login(USER)
    res = client.post('/api/review/already', json={'id': sid}, base_url=BASE)

    assert res.status_code == 200
    assert client.get('/api/review/queue',
                      base_url=BASE).get_json()['total'] == 0
    with app.app_context():
        row = db.session.get(StagedTransaction, sid)
        assert row.state == STAGED_IGNORED
        assert row.ignored_reason == 'already_tracked'
        assert row.reviewed_at is not None


def test_already_in_refuses_rows_not_awaiting_review(app, uid, login):
    with app.app_context():
        sid = stage(uid, state=STAGED_CONFIRMED)
    res = login(USER).post('/api/review/already', json={'id': sid},
                           base_url=BASE)
    assert res.status_code == 409


def test_back_restores_the_most_recently_skipped(app, uid, login):
    with app.app_context():
        first = stage(uid, 10.0, dedup='k1')
        second = stage(uid, 20.0, dedup='k2')

    client = login(USER)
    client.post('/api/review/skip', json={'id': first}, base_url=BASE)
    client.post('/api/review/skip', json={'id': second}, base_url=BASE)

    body = client.post('/api/review/unskip', json={'latest': True},
                       base_url=BASE).get_json()

    assert [i['id'] for i in body['items']] == [second]
    with app.app_context():
        assert db.session.get(StagedTransaction, second).state == STAGED_PROPOSED
        assert db.session.get(StagedTransaction, first).state == STAGED_SKIPPED


def test_unskip_all_puts_every_skipped_row_back(app, uid, login):
    with app.app_context():
        ids = [stage(uid, float(n), dedup=f'u{n}') for n in (1, 2, 3)]

    client = login(USER)
    client.post('/api/review/skip', json={'ids': ids}, base_url=BASE)
    assert client.get('/api/review/queue',
                      base_url=BASE).get_json()['skipped'] == 3

    client.post('/api/review/unskip', json={'all': True}, base_url=BASE)

    queue = client.get('/api/review/queue', base_url=BASE).get_json()
    assert queue['total'] == 3
    assert queue['skipped'] == 0


def test_unskip_restores_an_ambiguous_row_as_ambiguous(app, uid, login):
    with app.app_context():
        a, b = _txn(uid), _txn(uid)
        sid = stage(uid, state=STAGED_SKIPPED, candidate_ids=[a, b],
                    match_score=0.8)

    login(USER).post('/api/review/unskip', json={'id': sid}, base_url=BASE)

    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_AMBIGUOUS


def test_unskip_will_not_touch_another_users_rows(app, uid, login, make_user):
    other = make_user(OTHER)
    with app.app_context():
        sid = stage(other, state=STAGED_SKIPPED)

    body = login(USER).post('/api/review/unskip', json={'id': sid},
                            base_url=BASE).get_json()

    assert body['count'] == 0
    with app.app_context():
        assert db.session.get(StagedTransaction, sid).state == STAGED_SKIPPED


def test_unskip_rejects_a_body_that_names_nothing(app, uid, login):
    res = login(USER).post('/api/review/unskip', json={}, base_url=BASE)
    assert res.status_code == 400


def test_a_link_teaches_the_merchant_name_for_next_month(app, uid, login):
    """Link "hot mobile" to "phone bill" once; next month it matches alone."""
    from services import reconcile

    with app.app_context():
        this_month = _txn(uid, 89.90, day=DAY, description='phone bill')
        sid = stage(uid, 89.90, merchant='hot mobile', dedup='m1')

    login(USER).post('/api/review/link',
                     json={'id': sid, 'transaction_id': this_month},
                     base_url=BASE)

    with app.app_context():
        next_day = DAY + datetime.timedelta(days=31)
        _txn(uid, 89.90, day=next_day - datetime.timedelta(days=8),
             description='phone bill')
        nxt = db.session.get(StagedTransaction, stage(
            uid, 89.90, merchant='hot mobile', day=next_day, dedup='m2'))
        reconcile.reconcile_row(nxt)
        assert nxt.state == STAGED_MATCHED
        db.session.rollback()


def test_recheck_matches_rows_whose_counterpart_now_exists(app, uid, login):
    with app.app_context():
        stage(uid, 52.90, dedup='r1')
        stage(uid, 77.00, dedup='r2', merchant='רמי לוי')
        _txn(uid, 52.90, description='supermarket')

    client = login(USER)
    body = client.post('/api/review/rematch', base_url=BASE).get_json()

    assert body['matched'] == 1
    assert body['remaining'] == 1
    assert client.get('/api/review/queue',
                      base_url=BASE).get_json()['total'] == 1


# --- learning during review ----------------------------------------------

def test_a_link_resolves_the_same_merchants_other_charges(app, uid, login):
    """Link one Wolt order to "dinner"; the next Wolt order matches at once."""
    with app.app_context():
        first_txn = _txn(uid, 45.00, day=DAY, description='dinner')
        _txn(uid, 62.00, day=DAY + datetime.timedelta(days=12),
             description='dinner')
        first = stage(uid, 45.00, merchant='wolt', dedup='w1')
        second = stage(uid, 62.00, merchant='wolt', dedup='w2',
                       day=DAY + datetime.timedelta(days=6))

    body = login(USER).post('/api/review/link',
                            json={'id': first, 'transaction_id': first_txn},
                            base_url=BASE).get_json()

    assert body['auto_matched'] == [second]
    with app.app_context():
        assert db.session.get(StagedTransaction, second).state == STAGED_MATCHED


def test_a_linked_transaction_is_not_handed_to_an_identical_charge(app, uid,
                                                                   login):
    """Two ₪45 orders the same evening, one tracked: the other stays missing."""
    with app.app_context():
        txn = _txn(uid, 45.00, day=DAY, description='dinner')
        first = stage(uid, 45.00, merchant='wolt', dedup='t1')
        second = stage(uid, 45.00, merchant='wolt', dedup='t2')

    body = login(USER).post('/api/review/link',
                            json={'id': first, 'transaction_id': txn},
                            base_url=BASE).get_json()

    assert body['auto_matched'] == []
    assert [u['id'] for u in body['updated']] == [second]
    with app.app_context():
        assert db.session.get(StagedTransaction, second).state == STAGED_PROPOSED


def test_a_confirm_updates_the_suggestion_for_the_same_merchant(app, uid,
                                                                login):
    with app.app_context():
        first = stage(uid, 45.00, merchant='wolt', dedup='c1',
                      suggested_category=None)
        second = stage(uid, 62.00, merchant='wolt', dedup='c2',
                       suggested_category=None)

    body = login(USER).post('/api/review/confirm', json={'items': [{
        'id': first, 'category': 'zzEatingOut', 'description': 'dinner',
    }]}, base_url=BASE).get_json()

    updated = {u['id']: u for u in body['updated']}
    assert updated[second]['suggested_category'] == 'zzEatingOut'
    assert updated[second]['suggested_description'] == 'dinner'


def test_a_link_teaches_the_category_for_future_charges(app, uid, login):
    with app.app_context():
        txn = _txn(uid, 45.00, description='dinner')
        sid = stage(uid, 45.00, merchant='wolt', dedup='l1')

    login(USER).post('/api/review/link',
                     json={'id': sid, 'transaction_id': txn}, base_url=BASE)

    with app.app_context():
        rule = MerchantRule.query.filter_by(user_id=uid, pattern='wolt').one()
        assert (rule.category, rule.description) == ('zzGroceries', 'dinner')
