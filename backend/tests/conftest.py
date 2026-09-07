"""Shared fixtures. See test_auth_session.py for why these hit a real database."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = 'https://localhost'
PASSWORD = 'pytest-pass-12345'


@pytest.fixture(scope='session')
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

    _mount_token_probe(appmod.app)
    return appmod.app


def _mount_token_probe(flask_app):
    """A throwaway token-guarded route, for testing the decorator itself.

    Mounted here because Flask refuses new routes once the app has served a
    request, and every test client call counts. No real ingest route exists
    until Phase 5, so the decorator would otherwise be untestable.
    """
    from flask import g, jsonify

    from auth import token_required

    @flask_app.route('/api/_probe', methods=['POST'])
    @token_required
    def _token_probe():
        return jsonify({'user_id': g.user_id})


@pytest.fixture
def make_user(app):
    """Create throwaway users, cleaned up with everything they own."""
    created = []

    def _make(username):
        from models import db, User

        with app.app_context():
            User.query.filter_by(username=username).delete()
            db.session.commit()
            u = User(username=username, email=f'{username}@example.invalid')
            u.set_password(PASSWORD)
            db.session.add(u)
            db.session.commit()
            created.append(u.id)
            return u.id

    yield _make

    from models import (
        db, User, RefreshToken, IngestToken, StagedTransaction,
        ImportBatch, SourceProfile, MerchantRule, Transaction,
    )

    with app.app_context():
        for uid in created:
            StagedTransaction.query.filter_by(user_id=uid).delete()
            ImportBatch.query.filter_by(user_id=uid).delete()
            SourceProfile.query.filter_by(user_id=uid).delete()
            MerchantRule.query.filter_by(user_id=uid).delete()
            IngestToken.query.filter_by(user_id=uid).delete()
            RefreshToken.query.filter_by(user_id=uid).delete()
            Transaction.query.filter_by(user_id=uid).delete()
            User.query.filter_by(id=uid).delete()
        db.session.commit()


@pytest.fixture
def login(app):
    def _login(username):
        client = app.test_client()
        res = client.post(
            '/api/login',
            json={'username': username, 'password': PASSWORD},
            base_url=BASE,
        )
        assert res.status_code == 200, res.data
        return client
    return _login
