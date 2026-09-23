"""Statement upload, source profiles, and undo."""
import datetime
import io
import json

import pytest

from models import (
    STAGED_CONFIRMED,
    STAGED_IGNORED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    ImportBatch,
    SourceProfile,
    StagedTransaction,
    Transaction,
    db,
)

BASE = 'https://localhost'
USER = '__pytest_import_user__'
OTHER = '__pytest_import_other__'


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def xlsx(rows):
    from openpyxl import Workbook

    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


CARD_STATEMENT = [
    ['תאריך עסקה', 'שם בית עסק', 'סכום חיוב', 'מטבע'],
    ['03/04/2026', 'שופרסל דיל רמת אביב 442', '52.90', '₪'],
    ['05/04/2026', 'IKEA 3/12', '1,200.00', '₪'],
    ['07/04/2026', 'פז דלק', '300.00', '₪'],
]


def upload(client, rows=None, filename='max.xlsx', data=None, **form):
    payload = data if data is not None else xlsx(rows or CARD_STATEMENT)
    return client.post(
        '/api/import/upload',
        data={'file': (io.BytesIO(payload), filename), **form},
        content_type='multipart/form-data',
        base_url=BASE,
    )


def make_profile(client, **over):
    body = {'name': 'Max ···1234', 'kind': 'card', 'company_id': 'max'}
    body.update(over)
    res = client.post('/api/source-profiles', json=body, base_url=BASE)
    assert res.status_code == 201, res.data
    return res.get_json()


# --- upload --------------------------------------------------------------

def test_upload_stages_and_reconciles(app, uid, login):
    res = upload(login(USER))

    assert res.status_code == 201, res.data
    body = res.get_json()
    assert body['batch']['received'] == 3
    assert body['batch']['proposed'] == 3      # nothing tracked yet
    assert body['batch']['status'] == 'done'

    with app.app_context():
        rows = StagedTransaction.query.filter_by(user_id=uid).all()
        assert len(rows) == 3
        assert all(r.state == STAGED_PROPOSED for r in rows)
        assert all(r.source == 'upload' for r in rows)
        # The audit copy of the original line is kept.
        assert rows[0].raw['file'] == 'max.xlsx'


def test_an_already_tracked_charge_is_matched_not_proposed(app, uid, login):
    with app.app_context():
        db.session.add(Transaction(
            user_id=uid, type='expense', category='zzGroceriesImp',
            amount=52.90, date=datetime.date(2026, 4, 3),
            description='שופרסל דיל', currency='ILS', exchange_rate=1.0,
        ))
        db.session.commit()

    body = upload(login(USER)).get_json()

    assert body['batch']['matched'] == 1
    assert body['batch']['proposed'] == 2


def test_reuploading_the_same_file_stages_nothing_new(app, uid, login):
    """The idempotency guarantee, end to end."""
    client = login(USER)
    upload(client)
    second = upload(client)

    assert second.status_code == 201
    body = second.get_json()
    assert body['duplicates_skipped'] == 3
    assert body['batch']['new'] == 0

    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 3


def test_a_bank_profile_suppresses_the_card_settlement_line(app, uid, login):
    """The double-count guard, through the real upload path."""
    client = login(USER)
    profile = make_profile(client, name='Leumi', kind='bank',
                           company_id='leumi')

    rows = [
        ['תאריך', 'תאור', 'חובה'],
        ['03/04/2026', 'מקס', '3,214.00'],
        ['04/04/2026', 'ארנונה', '450.00'],
    ]
    body = upload(client, rows, source_profile_id=str(profile['id'])).get_json()

    assert body['batch']['suppressed'] == 1
    assert body['batch']['proposed'] == 1

    with app.app_context():
        ignored = StagedTransaction.query.filter_by(
            user_id=uid, state=STAGED_IGNORED
        ).one()
        assert ignored.ignored_reason == 'card_aggregate'


def test_a_working_mapping_is_remembered_on_the_profile(app, uid, login):
    client = login(USER)
    profile = make_profile(client)

    upload(client, source_profile_id=str(profile['id']))

    with app.app_context():
        saved = db.session.get(SourceProfile, profile['id']).column_mapping
        assert saved['txn_date'] == 0
        assert saved['amount'] == 2


def test_an_explicit_mapping_is_honoured(app, uid, login):
    rows = [
        ['col a', 'col b', 'col c'],
        ['03/04/2026', 'שופרסל', '52.90'],
    ]
    res = upload(
        login(USER), rows,
        mapping=json.dumps({'txn_date': 0, 'merchant_raw': 1, 'amount': 2}),
    )

    assert res.status_code == 201
    assert res.get_json()['batch']['received'] == 1


def test_a_bad_mapping_payload_is_rejected(app, uid, login):
    res = upload(login(USER), mapping='not json')
    assert res.status_code == 400


def test_oversized_uploads_are_refused(app, uid, login):
    # Larger than MAX_UPLOAD_BYTES but under Flask's own request cap, so the
    # view's own check is what rejects it.
    res = upload(login(USER), data=b'x' * (6 * 1024 * 1024), filename='big.csv')
    assert res.status_code == 413


@pytest.mark.parametrize('filename', ['statement.pdf', 'old.xls', 'notes.docx'])
def test_unsupported_file_types_are_refused(app, uid, login, filename):
    res = upload(login(USER), data=b'irrelevant', filename=filename)
    assert res.status_code == 400
    assert 'xlsx' in res.get_json()['message']


def test_a_missing_file_is_a_400_not_a_500(app, uid, login):
    res = login(USER).post('/api/import/upload', data={},
                           content_type='multipart/form-data', base_url=BASE)
    assert res.status_code == 400


def test_a_garbage_file_is_a_400_not_a_500(app, uid, login):
    res = upload(login(USER), data=b'not a spreadsheet', filename='x.xlsx')
    assert res.status_code == 400


def test_upload_requires_a_session(app):
    res = upload(app.test_client())
    assert res.status_code == 401


def test_upload_cannot_target_another_users_profile(app, uid, login, make_user):
    make_user(OTHER)
    theirs = make_profile(login(OTHER))

    res = upload(login(USER), source_profile_id=str(theirs['id']))
    assert res.status_code == 404


# --- preview -------------------------------------------------------------

def test_preview_shows_the_mapping_without_staging_anything(app, uid, login):
    res = login(USER).post(
        '/api/import/preview',
        data={'file': (io.BytesIO(xlsx(CARD_STATEMENT)), 'max.xlsx')},
        content_type='multipart/form-data',
        base_url=BASE,
    )

    assert res.status_code == 200
    body = res.get_json()
    assert body['row_count'] == 3
    assert len(body['sample']) == 3
    assert body['mapping']['txn_date'] == 0

    with app.app_context():
        # Nothing written, so correcting a bad guess needs no undo.
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 0


# --- batches and undo ----------------------------------------------------

def test_batches_are_listed_newest_first(app, uid, login):
    client = login(USER)
    upload(client)
    upload(client, [
        ['תאריך', 'תאור', 'סכום'],
        ['09/04/2026', 'רמי לוי', '80.00'],
    ])

    batches = client.get('/api/import-batches', base_url=BASE).get_json()
    assert len(batches) == 2
    assert batches[0]['id'] > batches[1]['id']


def test_undo_removes_staged_rows(app, uid, login):
    client = login(USER)
    batch_id = upload(client).get_json()['batch']['id']

    res = client.delete(f'/api/import-batches/{batch_id}', base_url=BASE)

    assert res.status_code == 200
    assert res.get_json()['removed'] == 3
    with app.app_context():
        assert StagedTransaction.query.filter_by(user_id=uid).count() == 0
        assert db.session.get(ImportBatch, batch_id) is None


def test_undo_keeps_rows_the_user_already_confirmed(app, uid, login):
    """Silently deleting reviewed work would be worse than a partial undo."""
    client = login(USER)
    batch_id = upload(client).get_json()['batch']['id']

    queue = client.get('/api/review/queue', base_url=BASE).get_json()
    first = queue['items'][0]['id']
    client.post('/api/review/confirm',
                json={'items': [{'id': first, 'category': 'zzImpCat'}]},
                base_url=BASE)

    res = client.delete(f'/api/import-batches/{batch_id}', base_url=BASE)

    body = res.get_json()
    assert body['kept_confirmed'] == 1
    assert body['removed'] == 2
    with app.app_context():
        remaining = StagedTransaction.query.filter_by(user_id=uid).all()
        assert [r.state for r in remaining] == [STAGED_CONFIRMED]
        # The real transaction survives too.
        assert Transaction.query.filter_by(user_id=uid).count() == 1


def test_undo_cannot_reach_another_users_batch(app, uid, login, make_user):
    make_user(OTHER)
    batch_id = upload(login(OTHER)).get_json()['batch']['id']

    res = login(USER).delete(f'/api/import-batches/{batch_id}', base_url=BASE)
    assert res.status_code == 404


# --- source profiles -----------------------------------------------------

def test_a_bank_profile_gets_the_settlement_exclusions_by_default(app, uid,
                                                                  login):
    """Nobody would think to add these by hand, and omitting them double-counts."""
    profile = make_profile(login(USER), name='Leumi', kind='bank',
                           company_id='leumi')

    assert profile['exclude_patterns']
    assert any('מקס' in p for p in profile['exclude_patterns'])


def test_a_card_profile_gets_no_exclusions(app, uid, login):
    profile = make_profile(login(USER))
    assert profile['exclude_patterns'] == []


def test_an_invalid_regex_is_refused_when_entered(app, uid, login):
    """Silently ignoring an exclusion the user thinks is active means double money."""
    client = login(USER)
    profile = make_profile(client)

    res = client.put(f"/api/source-profiles/{profile['id']}",
                     json={'exclude_patterns': ['[unclosed']}, base_url=BASE)

    assert res.status_code == 400
    assert 'invalid regular expression' in res.get_json()['message']


@pytest.mark.parametrize('body', [
    {'name': '', 'kind': 'card', 'company_id': 'max'},
    {'name': 'x', 'kind': 'wallet', 'company_id': 'max'},
    {'name': 'x', 'kind': 'card', 'company_id': 'nope'},
])
def test_invalid_profiles_are_refused(app, uid, login, body):
    res = login(USER).post('/api/source-profiles', json=body, base_url=BASE)
    assert res.status_code == 400


def test_profiles_are_scoped_to_their_owner(app, uid, login, make_user):
    make_user(OTHER)
    make_profile(login(OTHER))

    assert login(USER).get('/api/source-profiles', base_url=BASE).get_json() == []


def test_a_profile_with_history_is_not_deletable(app, uid, login):
    client = login(USER)
    profile = make_profile(client)
    upload(client, source_profile_id=str(profile['id']))

    res = client.delete(f"/api/source-profiles/{profile['id']}", base_url=BASE)

    assert res.status_code == 409
    assert res.get_json()['staged_rows'] == 3


def test_an_unused_profile_is_deletable(app, uid, login):
    client = login(USER)
    profile = make_profile(client)

    res = client.delete(f"/api/source-profiles/{profile['id']}", base_url=BASE)
    assert res.status_code == 200


def test_a_profile_can_be_updated(app, uid, login):
    client = login(USER)
    profile = make_profile(client)

    res = client.put(f"/api/source-profiles/{profile['id']}",
                     json={'name': 'Max renamed', 'active': False},
                     base_url=BASE)

    assert res.status_code == 200
    assert res.get_json()['name'] == 'Max renamed'
    assert res.get_json()['active'] is False


def test_profile_endpoints_require_a_session(app):
    client = app.test_client()
    assert client.get('/api/source-profiles', base_url=BASE).status_code == 401
    assert client.post('/api/source-profiles', json={},
                       base_url=BASE).status_code == 401


# --- error disclosure ----------------------------------------------------

def test_a_server_error_does_not_leak_its_detail(app):
    """The handler used to return str(e).

    With statements being parsed server-side, an exception can carry the
    contents of a bank export -- merchant names, amounts, account references.
    """
    res = app.test_client().get('/api/_boom', base_url=BASE)

    assert res.status_code == 500
    body = res.get_data(as_text=True)
    for leak in ('staged_transaction', 'שופרסל', '52.90', 'DETAIL', 'Key (id)'):
        assert leak not in body, leak


def test_a_404_still_says_what_went_wrong(app, uid, login):
    """Generic 500s must not flatten genuine HTTP errors into nonsense."""
    res = login(USER).delete('/api/import-batches/99999999', base_url=BASE)
    assert res.status_code == 404
    assert 'not found' in res.get_json()['message'].lower()
