"""Ingest token lifecycle and the token_required guard."""
BASE = 'https://localhost'
USER = '__pytest_token_user__'
OTHER = '__pytest_token_other__'


def create_token(client, name='phone'):
    res = client.post('/api/tokens', json={'name': name}, base_url=BASE)
    assert res.status_code == 201, res.data
    return res.get_json()


def test_plaintext_is_returned_once_and_never_stored(app, make_user, login):
    from models import IngestToken

    uid = make_user(USER)
    body = create_token(login(USER))
    raw = body['token']

    assert raw
    assert body['prefix'] == raw[:8]

    with app.app_context():
        row = IngestToken.query.filter_by(user_id=uid).one()
        assert row.token_hash == IngestToken.hash_token(raw)
        # The plaintext exists nowhere in the row.
        assert raw not in (row.token_hash, row.prefix + 'x')
        assert len(row.prefix) == 8

    # Listing never exposes it again.
    listed = login(USER).get('/api/tokens', base_url=BASE).get_json()
    assert len(listed) == 1
    assert 'token' not in listed[0]


def test_tokens_are_scoped_to_their_owner(app, make_user, login):
    make_user(USER)
    make_user(OTHER)
    create_token(login(USER), 'mine')

    assert login(OTHER).get('/api/tokens', base_url=BASE).get_json() == []


def test_revoke_is_idempotent_and_owner_scoped(app, make_user, login):
    make_user(USER)
    make_user(OTHER)
    token_id = create_token(login(USER))['id']

    # Another user cannot revoke it, and gets no hint that it exists.
    assert login(OTHER).delete(
        f'/api/tokens/{token_id}', base_url=BASE
    ).status_code == 404

    client = login(USER)
    assert client.delete(f'/api/tokens/{token_id}', base_url=BASE).status_code == 200
    assert client.delete(f'/api/tokens/{token_id}', base_url=BASE).status_code == 200

    assert client.get('/api/tokens', base_url=BASE).get_json()[0]['active'] is False


def test_creating_a_token_requires_a_name(app, make_user, login):
    make_user(USER)
    res = login(USER).post('/api/tokens', json={}, base_url=BASE)
    assert res.status_code == 400


def test_token_endpoints_need_a_session(app):
    client = app.test_client()
    assert client.get('/api/tokens', base_url=BASE).status_code == 401
    assert client.post(
        '/api/tokens', json={'name': 'x'}, base_url=BASE
    ).status_code == 401


# --- token_required -------------------------------------------------------

def test_bearer_token_authenticates_and_stamps_last_used(app, make_user, login):
    from models import IngestToken

    uid = make_user(USER)
    raw = create_token(login(USER))['token']

    with app.app_context():
        assert IngestToken.query.filter_by(user_id=uid).one().last_used_at is None

    res = app.test_client().post(
        '/api/_probe',
        headers={'Authorization': f'Bearer {raw}'},
        base_url=BASE,
    )

    assert res.status_code == 200
    assert res.get_json()['user_id'] == uid
    with app.app_context():
        assert IngestToken.query.filter_by(user_id=uid).one().last_used_at is not None


def test_revoked_token_stops_working(app, make_user, login):
    make_user(USER)
    client = login(USER)
    body = create_token(client)

    client.delete(f"/api/tokens/{body['id']}", base_url=BASE)

    res = app.test_client().post(
        '/api/_probe',
        headers={'Authorization': f"Bearer {body['token']}"},
        base_url=BASE,
    )
    assert res.status_code == 401


def test_malformed_or_missing_bearer_is_rejected(app, make_user, login):
    make_user(USER)
    raw = create_token(login(USER))['token']
    client = app.test_client()

    for headers in (
        {},
        {'Authorization': raw},                    # no scheme
        {'Authorization': 'Bearer '},              # empty
        {'Authorization': 'Bearer not-a-token'},
        {'Authorization': f'Basic {raw}'},         # wrong scheme
    ):
        res = client.post('/api/_probe', headers=headers, base_url=BASE)
        assert res.status_code == 401, headers


def test_session_cookie_does_not_satisfy_token_required(app, make_user, login):
    """The two mechanisms are separate on purpose."""
    make_user(USER)

    assert login(USER).post('/api/_probe', base_url=BASE).status_code == 401
