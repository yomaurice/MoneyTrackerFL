"""POST /api/transactions/bulk and the shared validator."""
import pytest

from services.validation import (
    MAX_BULK_ROWS,
    ValidationError,
    validate_transaction,
    validate_transaction_list,
)

BASE = 'https://localhost'
USER = '__pytest_bulk_user__'
OTHER = '__pytest_bulk_other__'


def row(**over):
    base = {
        'type': 'expense',
        'category': 'Groceries',
        'amount': 52.9,
        'date': '2026-09-01',
        'description': 'שופרסל דיל',
    }
    base.update(over)
    return base


# --- the endpoint ---------------------------------------------------------

def test_bulk_insert_creates_rows_for_the_caller(app, make_user, login):
    from models import Transaction

    uid = make_user(USER)
    res = login(USER).post(
        '/api/transactions/bulk',
        json=[row(), row(amount=12.5, category='Transport')],
        base_url=BASE,
    )

    assert res.status_code == 201, res.data
    body = res.get_json()
    assert body['count'] == 2
    assert len(body['ids']) == 2

    with app.app_context():
        rows = Transaction.query.filter_by(user_id=uid).all()
        assert len(rows) == 2
        assert {r.category for r in rows} == {'Groceries', 'Transport'}
        assert all(r.currency == 'ILS' and r.exchange_rate == 1.0 for r in rows)


def test_a_wrapped_object_is_accepted_too(app, make_user, login):
    make_user(USER)
    res = login(USER).post(
        '/api/transactions/bulk',
        json={'transactions': [row()]},
        base_url=BASE,
    )
    assert res.status_code == 201


def test_one_bad_row_rejects_the_whole_batch(app, make_user, login):
    """Partial success would leave the caller unable to retry safely."""
    from models import Transaction

    uid = make_user(USER)
    res = login(USER).post(
        '/api/transactions/bulk',
        json=[row(), row(amount=-5), row(type='banana')],
        base_url=BASE,
    )

    assert res.status_code == 400
    errors = res.get_json()['errors']
    # Every bad row is reported, not just the first.
    assert [e['index'] for e in errors] == [1, 2]

    with app.app_context():
        assert Transaction.query.filter_by(user_id=uid).count() == 0


def test_batch_size_is_capped(app, make_user, login):
    make_user(USER)
    res = login(USER).post(
        '/api/transactions/bulk',
        json=[row() for _ in range(MAX_BULK_ROWS + 1)],
        base_url=BASE,
    )
    assert res.status_code == 400
    assert 'at most' in res.get_json()['message']


def test_empty_and_malformed_bodies_are_400_not_500(app, make_user, login):
    make_user(USER)
    client = login(USER)

    for body in ([], {}, 'not-a-list', 42):
        res = client.post('/api/transactions/bulk', json=body, base_url=BASE)
        assert res.status_code == 400, body


def test_bulk_requires_a_session(app):
    res = app.test_client().post(
        '/api/transactions/bulk', json=[row()], base_url=BASE
    )
    assert res.status_code == 401


def test_rows_land_on_the_authenticated_user_only(app, make_user, login):
    from models import Transaction

    make_user(USER)
    other_uid = make_user(OTHER)

    login(USER).post('/api/transactions/bulk', json=[row()], base_url=BASE)

    with app.app_context():
        assert Transaction.query.filter_by(user_id=other_uid).count() == 0


# --- the validator --------------------------------------------------------

def test_valid_payload_is_normalised():
    out = validate_transaction(row(currency='usd', exchange_rate='3.7'))
    assert out['currency'] == 'USD'
    assert out['exchange_rate'] == 3.7
    assert str(out['date']) == '2026-09-01'


def test_description_is_optional_and_blank_becomes_null():
    assert validate_transaction(row(description='  '))['description'] is None
    payload = row()
    del payload['description']
    assert validate_transaction(payload)['description'] is None


@pytest.mark.parametrize('bad, reason', [
    (row(type=None), 'type'),
    (row(type='transfer'), 'type'),
    (row(category=''), 'category'),
    (row(category=None), 'category'),
    (row(amount='abc'), 'amount'),
    (row(amount=0), 'amount'),
    (row(amount=-1), 'amount'),
    (row(amount=float('inf')), 'amount'),
    (row(amount=float('nan')), 'amount'),
    (row(date='01/09/2026'), 'date'),
    (row(date=''), 'date'),
    (row(exchange_rate=0), 'exchange_rate'),
    (row(exchange_rate=-2), 'exchange_rate'),
    (row(description='x' * 301), 'description'),
])
def test_rejected_payloads(bad, reason):
    with pytest.raises(ValidationError) as exc:
        validate_transaction(bad)
    assert reason in str(exc.value)


def test_list_validator_reports_every_bad_row():
    with pytest.raises(ValidationError) as exc:
        validate_transaction_list([row(), row(amount=0), row(), row(type='x')])

    detail = exc.value.args[0]
    assert [e['index'] for e in detail] == [1, 3]
