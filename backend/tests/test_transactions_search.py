"""Searching tracked transactions, for the review wizard's month list."""
import datetime

import pytest

from models import Transaction, db

BASE = 'https://localhost'
USER = '__pytest_search_user__'
OTHER = '__pytest_search_other__'


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def add(uid, amount, day, description='', category='zzFood', type_='expense'):
    db.session.add(Transaction(
        user_id=uid, type=type_, category=category, amount=amount,
        date=day, description=description, currency='ILS', exchange_rate=1.0,
    ))


@pytest.fixture
def seeded(app, uid):
    with app.app_context():
        add(uid, 52.90, datetime.date(2026, 9, 3), 'weekly shop')
        add(uid, 300.00, datetime.date(2026, 9, 20), 'fuel', category='zzCar')
        add(uid, 53.50, datetime.date(2026, 8, 30), 'coffee beans')
        add(uid, 9000, datetime.date(2026, 9, 1), 'salary', type_='income')
        db.session.commit()
    return uid


def search(client, **params):
    res = client.get('/api/transactions/search', query_string=params,
                     base_url=BASE)
    assert res.status_code == 200, res.data
    return res.get_json()


def test_month_filter_returns_that_month_newest_first(app, seeded, login):
    body = search(login(USER), month='2026-09')
    assert [i['description'] for i in body['items']] == [
        'fuel', 'weekly shop', 'salary']
    assert body['truncated'] is False


def test_text_matches_description_or_category(app, seeded, login):
    client = login(USER)
    assert [i['description'] for i in search(client, q='SHOP')['items']] == [
        'weekly shop']
    assert [i['description'] for i in search(client, q='zzcar')['items']] == [
        'fuel']


def test_amount_matches_within_two_units_by_default(app, seeded, login):
    body = search(login(USER), amount='52.90')
    assert sorted(i['amount'] for i in body['items']) == [52.90, 53.50]


def test_filters_combine(app, seeded, login):
    body = search(login(USER), month='2026-09', amount='52.90', type='expense')
    assert [i['description'] for i in body['items']] == ['weekly shop']


def test_date_range(app, seeded, login):
    body = search(login(USER), **{'from': '2026-08-29', 'to': '2026-09-02'})
    assert sorted(i['description'] for i in body['items']) == [
        'coffee beans', 'salary']


def test_another_users_transactions_never_appear(app, seeded, login,
                                                 make_user):
    other = make_user(OTHER)
    with app.app_context():
        add(other, 52.90, datetime.date(2026, 9, 3), 'not yours')
        db.session.commit()
    body = search(login(USER), q='not yours')
    assert body['items'] == []


@pytest.mark.parametrize('params', [
    {'month': 'September'}, {'from': 'yesterday'}, {'amount': 'lots'},
])
def test_malformed_filters_are_a_400(app, uid, login, params):
    res = login(USER).get('/api/transactions/search', query_string=params,
                          base_url=BASE)
    assert res.status_code == 400


def test_search_requires_a_session(app):
    res = app.test_client().get('/api/transactions/search', base_url=BASE)
    assert res.status_code == 401
