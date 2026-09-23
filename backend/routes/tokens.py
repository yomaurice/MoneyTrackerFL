"""Ingest token management. Browser-authenticated: you mint tokens, machines use them."""
import datetime
import secrets

from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import db, IngestToken

tokens_bp = Blueprint('tokens', __name__, url_prefix='/api/tokens')

MAX_TOKENS_PER_USER = 20
TOKEN_BYTES = 32
PREFIX_LENGTH = 8


def _serialise(token):
    return {
        'id': token.id,
        'name': token.name,
        'prefix': token.prefix,
        'scope': token.scope,
        'created_at': token.created_at.isoformat() if token.created_at else None,
        'last_used_at': (
            token.last_used_at.isoformat() if token.last_used_at else None
        ),
        'revoked_at': token.revoked_at.isoformat() if token.revoked_at else None,
        'active': token.is_active,
    }


@tokens_bp.route('', methods=['GET'])
@login_required
def list_tokens():
    tokens = (
        IngestToken.query
        .filter_by(user_id=g.user_id)
        .order_by(IngestToken.created_at.desc())
        .all()
    )
    return jsonify([_serialise(t) for t in tokens])


@tokens_bp.route('', methods=['POST'])
@login_required
def create_token():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()

    if not name:
        return jsonify({'message': 'name is required'}), 400
    if len(name) > 120:
        return jsonify({'message': 'name exceeds 120 characters'}), 400

    live = IngestToken.query.filter_by(
        user_id=g.user_id, revoked_at=None
    ).count()
    if live >= MAX_TOKENS_PER_USER:
        return jsonify({
            'message': f'at most {MAX_TOKENS_PER_USER} active tokens; '
                       'revoke one first'
        }), 409

    raw = secrets.token_urlsafe(TOKEN_BYTES)
    token = IngestToken(
        user_id=g.user_id,
        name=name,
        token_hash=IngestToken.hash_token(raw),
        prefix=raw[:PREFIX_LENGTH],
        scope='ingest',
    )
    db.session.add(token)
    db.session.commit()

    body = _serialise(token)
    # The only time the plaintext exists outside the caller's hands. Nothing
    # stores it, so a lost token is replaced rather than recovered.
    body['token'] = raw
    body['warning'] = 'Copy this token now; it cannot be shown again.'
    return jsonify(body), 201


@tokens_bp.route('/<int:token_id>', methods=['DELETE'])
@login_required
def revoke_token(token_id):
    token = IngestToken.query.filter_by(
        id=token_id, user_id=g.user_id
    ).first()

    if token is None:
        return jsonify({'message': 'Token not found'}), 404

    if token.revoked_at is None:
        token.revoked_at = datetime.datetime.utcnow()
        db.session.commit()

    return jsonify({'message': 'Token revoked', 'id': token_id})
