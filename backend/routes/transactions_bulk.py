"""Validated array insert.

The app has only ever inserted one transaction at a time, which is why a
forgotten expense stays forgotten -- there was no way to accept a batch. Every
later phase confirms reviewed rows through here.
"""
from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import db, Transaction
from services.validation import ValidationError, validate_transaction_list

transactions_bulk_bp = Blueprint('transactions_bulk', __name__)


@transactions_bulk_bp.route('/api/transactions/bulk', methods=['POST'])
@login_required
def create_transactions_bulk():
    body = request.get_json(silent=True)

    if isinstance(body, dict):
        body = body.get('transactions')

    try:
        rows = validate_transaction_list(body)
    except ValidationError as exc:
        detail = exc.args[0]
        # A list of per-row errors, or a single message about the batch itself.
        if isinstance(detail, list):
            return jsonify({
                'message': f'{len(detail)} of {len(body or [])} rows rejected',
                'errors': detail,
            }), 400
        return jsonify({'message': str(detail)}), 400

    # One commit for the batch: a partial insert would leave the caller unable
    # to tell what landed, and retrying would then duplicate the rows that did.
    created = [
        Transaction(
            user_id=g.user_id,
            type=row['type'],
            category=row['category'],
            amount=row['amount'],
            date=row['date'],
            description=row['description'],
            currency=row['currency'],
            exchange_rate=row['exchange_rate'],
        )
        for row in rows
    ]

    db.session.add_all(created)
    db.session.commit()

    return jsonify({
        'message': f'{len(created)} transactions created',
        'count': len(created),
        'ids': [t.id for t in created],
    }), 201
