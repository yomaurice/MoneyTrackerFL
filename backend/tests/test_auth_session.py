"""Integration tests for session issue, rotation, reuse and logout.

These run against a real database because the behaviour under test *is* the
database interaction -- rotation, revocation and reuse detection are rows, not
logic that can be faked. Point DATABASE_URL at a local Postgres and run
`flask db upgrade` first; the tests skip rather than fail if no database is
reachable, so they stay safe to run anywhere.

    cd backend && python -m pytest tests -v
"""
import datetime
import os
import sys

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = 'https://localhost'
USERNAME = '__pytest_session_user__'
PASSWORD = 'pytest-pass-12345'


@pytest.fixture(scope='module')
def app():
    try:
        import App as appmod
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f'backend app could not be imported: {exc}')

    from models import db

    with appmod.app.app_context():
        try:
            db.session.execute(db.text('select 1'))
        except Exception as exc:  # pragma: no cover - environment guard
            pytest.skip(f'no database reachable: {exc}')

    return appmod.app


@pytest.fixture
def user(app):
    """A throwaway user, removed with its tokens afterwards."""
    from models import db, User, RefreshToken

    with app.app_context():
        User.query.filter_by(username=USERNAME).delete()
        db.session.commit()
        u = User(username=USERNAME, email='pytest@example.invalid')
        u.set_password(PASSWORD)
        db.session.add(u)
        db.session.commit()
        uid = u.id

    yield uid

    with app.app_context():
        RefreshToken.query.filter_by(user_id=uid).delete()
        User.query.filter_by(username=USERNAME).delete()
        db.session.commit()


def cookie_value(client, name):
    for jar in client._cookies.values():
        for ck in (jar.values() if isinstance(jar, dict) else [jar]):
            if getattr(ck, 'key', None) == name:
                return ck.value
    return None


def login(app):
    client = app.test_client()
    res = client.post(
        '/api/login',
        json={'username': USERNAME, 'password': PASSWORD},
        base_url=BASE,
    )
    assert res.status_code == 200, res.data
    return client, res


def test_login_sets_hardened_cookies(app, user):
    client, res = login(app)
    headers = ' | '.join(v for k, v in res.headers.items()
                         if k.lower() == 'set-cookie')

    assert 'SameSite=Lax' in headers
    assert 'HttpOnly' in headers
    assert 'Secure' in headers
    # 90 days, so a regularly used app never asks for a password again.
    assert 'Max-Age=7776000' in headers
    assert client.get('/api/me', base_url=BASE).status_code == 200


def test_refresh_token_is_stored_hashed(app, user):
    from models import RefreshToken

    client, _ = login(app)
    raw = cookie_value(client, 'refresh_token')

    with app.app_context():
        rows = RefreshToken.query.filter_by(user_id=user).all()
        assert len(rows) == 1
        assert rows[0].token_hash != raw
        assert rows[0].token_hash == RefreshToken.hash_token(raw)


def test_transactions_route_does_not_raise(app, user):
    """Regression: the route ordered by a created_at column that did not exist."""
    client, _ = login(app)
    assert client.get('/api/transactions', base_url=BASE).status_code == 200


def test_refresh_rotates_and_slides(app, user):
    from models import RefreshToken

    client, _ = login(app)
    first = cookie_value(client, 'refresh_token')

    assert client.post('/api/refresh', base_url=BASE).status_code == 200
    second = cookie_value(client, 'refresh_token')

    assert second and second != first
    with app.app_context():
        old = RefreshToken.query.filter_by(
            token_hash=RefreshToken.hash_token(first)).first()
        new = RefreshToken.query.filter_by(
            token_hash=RefreshToken.hash_token(second)).first()
        assert old.rotated_to == RefreshToken.hash_token(second)
        assert new.is_active

    assert client.get('/api/me', base_url=BASE).status_code == 200


def test_replay_within_grace_window_is_tolerated(app, user):
    """Two tabs refreshing at once must not look like theft."""
    from models import RefreshToken

    client, _ = login(app)
    first = cookie_value(client, 'refresh_token')
    client.post('/api/refresh', base_url=BASE)

    other_tab = app.test_client()
    other_tab.set_cookie('refresh_token', first, domain='localhost')
    res = other_tab.post('/api/refresh', base_url=BASE)

    assert res.status_code == 200
    # Only an access token comes back; the browser already holds the successor.
    assert cookie_value(other_tab, 'refresh_token') in (None, first)
    with app.app_context():
        assert RefreshToken.query.filter_by(
            user_id=user, revoked_at=None).count() >= 1


def test_stale_replay_revokes_the_family(app, user):
    from models import db, RefreshToken

    client, _ = login(app)
    first = cookie_value(client, 'refresh_token')
    client.post('/api/refresh', base_url=BASE)
    second = cookie_value(client, 'refresh_token')

    with app.app_context():
        successor = RefreshToken.query.filter_by(
            token_hash=RefreshToken.hash_token(second)).first()
        successor.issued_at = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=600)
        )
        db.session.commit()

    thief = app.test_client()
    thief.set_cookie('refresh_token', first, domain='localhost')
    assert thief.post('/api/refresh', base_url=BASE).status_code == 401

    with app.app_context():
        assert RefreshToken.query.filter_by(
            user_id=user, revoked_at=None).count() == 0

    # The legitimate holder is signed out too -- deliberate, since which side
    # holds the stolen copy is unknowable.
    assert client.post('/api/refresh', base_url=BASE).status_code == 401


def _mint(app, uid, **extra):
    payload = {
        'user_id': uid,
        'type': 'refresh',
        'exp': datetime.datetime.utcnow() + datetime.timedelta(days=7),
    }
    payload.update(extra)
    return jwt.encode(payload, app.config['SECRET_KEY'], algorithm='HS256')


def test_pre_rotation_token_is_adopted(app, user):
    """Sessions issued before rotation shipped must not all be logged out."""
    client = app.test_client()
    client.set_cookie('refresh_token', _mint(app, user), domain='localhost')

    res = client.post('/api/refresh', base_url=BASE)

    assert res.status_code == 200
    adopted = cookie_value(client, 'refresh_token')
    assert adopted and adopted != _mint(app, user)


def test_unknown_token_with_jti_is_refused(app, user):
    """The adoption path is narrow: only tokens the old code could have made."""
    client = app.test_client()
    client.set_cookie('refresh_token', _mint(app, user, jti='deadbeef'),
                      domain='localhost')

    assert client.post('/api/refresh', base_url=BASE).status_code == 401


def test_logout_revokes_only_this_session(app, user):
    from models import RefreshToken

    phone, _ = login(app)
    desktop, _ = login(app)
    phone_token = cookie_value(phone, 'refresh_token')

    assert phone.post('/api/logout', base_url=BASE).status_code == 200

    with app.app_context():
        row = RefreshToken.query.filter_by(
            token_hash=RefreshToken.hash_token(phone_token)).first()
        assert row.revoked_at is not None

    assert desktop.post('/api/refresh', base_url=BASE).status_code == 200
