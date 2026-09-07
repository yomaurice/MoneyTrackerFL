"""Request authentication: browser cookies and machine bearer tokens.

Two callers, two mechanisms. A browser holds a 15-minute access cookie backed
by a rotating refresh cookie. A phone automation rule or a CI runner can do
neither, so ingest routes accept a long-lived bearer token instead.

Kept out of App.py so both decorators live together and the blueprints in
`routes/` can import them without importing the app.
"""
import datetime
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from models import db, IngestToken


def decode_token(token, expected_type):
    """Return the user id in a valid token of the expected type, else None."""
    try:
        payload = jwt.decode(
            token, current_app.config['SECRET_KEY'], algorithms=['HS256']
        )
        if payload.get('type') != expected_type:
            return None
        return payload['user_id']
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def login_required(f):
    """Require a valid access cookie. For browser traffic."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get('access_token')
        if not token:
            return jsonify({'message': 'Unauthorized'}), 401

        user_id = decode_token(token, 'access')
        if not user_id:
            return jsonify({'message': 'Access token expired'}), 401

        g.user_id = user_id
        return f(*args, **kwargs)
    return decorated


def token_required(f):
    """Require a live ingest token in `Authorization: Bearer <token>`.

    Deliberately narrow. This is the credential that sits in a phone
    automation rule and in CI secrets, where it is far more exposed than a
    browser cookie, so it must only ever reach routes that *stage* data for
    review. It must never guard a read, and never anything that mutates
    confirmed transactions -- a leaked token should be able to add noise to a
    review queue and nothing else.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return jsonify({'message': 'Bearer token required'}), 401

        raw = header[len('Bearer '):].strip()
        if not raw:
            return jsonify({'message': 'Bearer token required'}), 401

        record = IngestToken.query.filter_by(
            token_hash=IngestToken.hash_token(raw)
        ).first()

        if record is None or not record.is_active:
            return jsonify({'message': 'Invalid or revoked token'}), 401

        # Written on every use so a token that has gone quiet is visibly stale
        # in the UI and can be revoked with confidence.
        record.last_used_at = datetime.datetime.utcnow()
        db.session.commit()

        g.user_id = record.user_id
        g.ingest_token_id = record.id
        return f(*args, **kwargs)
    return decorated
