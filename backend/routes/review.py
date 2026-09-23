"""The review queue: staged proposals in, real transactions out.

This is the only path by which ingested data becomes a real transaction, and it
always runs through a human. Nothing here writes to `transaction` without an
explicit confirm.
"""
import datetime

from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import (
    STAGED_AMBIGUOUS,
    STAGED_CONFIRMED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    STAGED_SKIPPED,
    StagedTransaction,
    Transaction,
    db,
)
from services import categorize
from services.validation import ValidationError, validate_transaction

review_bp = Blueprint('review', __name__, url_prefix='/api/review')

# The wizard walks one row at a time; this caps a single page of the queue.
QUEUE_LIMIT = 200
MAX_CONFIRM_ROWS = 200

REVIEWABLE_STATES = (STAGED_PROPOSED, STAGED_AMBIGUOUS)


def _serialise(staged, position=None, total=None):
    body = {
        'id': staged.id,
        'state': staged.state,
        'source': staged.source,
        'batch_id': staged.batch_id,
        'date': staged.txn_date.isoformat() if staged.txn_date else None,
        'posted_date': (
            staged.posted_date.isoformat() if staged.posted_date else None
        ),
        'amount': staged.amount,
        'currency': staged.currency,
        'exchange_rate': staged.exchange_rate,
        'type': staged.type,
        'merchant_raw': staged.merchant_raw,
        'merchant_clean': staged.merchant_clean,
        'memo': staged.memo,
        'installment_no': staged.installment_no,
        'installment_total': staged.installment_total,
        'match_score': staged.match_score,
        'candidate_ids': staged.candidate_ids,
        # What the form should be pre-filled with. Both stay editable.
        'suggested_category': staged.suggested_category,
        'suggested_description': staged.suggested_description,
        'category_confidence': staged.category_confidence,
    }
    if position is not None:
        body['position'] = position
        body['total'] = total
    return body


def _queue_query(user_id):
    return (
        StagedTransaction.query
        .filter(
            StagedTransaction.user_id == user_id,
            StagedTransaction.state.in_(REVIEWABLE_STATES),
        )
        # Oldest charge first, so reviewing reads chronologically rather than
        # jumping around the month.
        .order_by(
            StagedTransaction.txn_date.asc().nullslast(),
            StagedTransaction.id.asc(),
        )
    )


@review_bp.route('/queue', methods=['GET'])
@login_required
def review_queue():
    query = _queue_query(g.user_id)

    batch_id = request.args.get('batch_id', type=int)
    if batch_id is not None:
        query = query.filter(StagedTransaction.batch_id == batch_id)

    total = query.count()
    rows = query.limit(QUEUE_LIMIT).all()

    return jsonify({
        'total': total,
        # The wizard's "4 / 9" comes straight from these, so the count and the
        # positions can never disagree.
        'items': [
            _serialise(row, position=i + 1, total=total)
            for i, row in enumerate(rows)
        ],
        'ambiguous': sum(1 for r in rows if r.state == STAGED_AMBIGUOUS),
    })


@review_bp.route('/confirm', methods=['POST'])
@login_required
def review_confirm():
    """Turn staged rows into real transactions, in one commit.

    Accepts the edits the user made in the wizard rather than the suggestions,
    since the whole point of review is that the suggestion may be wrong.
    """
    body = request.get_json(silent=True)
    items = body.get('items') if isinstance(body, dict) else body

    if not isinstance(items, list) or not items:
        return jsonify({'message': 'items must be a non-empty list'}), 400
    if len(items) > MAX_CONFIRM_ROWS:
        return jsonify({
            'message': f'at most {MAX_CONFIRM_ROWS} rows per confirm'
        }), 400

    ids = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), int):
            return jsonify({'message': 'each item needs an integer id'}), 400
        ids.append(item['id'])

    if len(set(ids)) != len(ids):
        return jsonify({'message': 'duplicate ids in request'}), 400

    staged_rows = {
        row.id: row for row in StagedTransaction.query.filter(
            StagedTransaction.user_id == g.user_id,
            StagedTransaction.id.in_(ids),
        ).all()
    }

    missing = [i for i in ids if i not in staged_rows]
    if missing:
        return jsonify({
            'message': 'unknown staged rows', 'ids': missing
        }), 404

    already = [
        i for i in ids if staged_rows[i].state not in REVIEWABLE_STATES
    ]
    if already:
        # Re-confirming would insert the transaction twice, which is exactly
        # the failure this pipeline exists to prevent.
        return jsonify({
            'message': 'rows are not awaiting review',
            'ids': already,
            'states': {i: staged_rows[i].state for i in already},
        }), 409

    prepared, errors = [], []
    for index, item in enumerate(items):
        staged = staged_rows[item['id']]
        payload = {
            'type': item.get('type') or staged.type,
            'category': item.get('category') or staged.suggested_category,
            'amount': item.get('amount', staged.amount),
            'date': item.get('date') or staged.txn_date,
            'description': (
                item.get('description')
                if item.get('description') is not None
                else staged.suggested_description
            ),
            'currency': item.get('currency') or staged.currency or 'ILS',
            'exchange_rate': item.get('exchange_rate', staged.exchange_rate),
        }
        try:
            prepared.append((staged, validate_transaction(payload, index)))
        except ValidationError as exc:
            errors.append({'index': index, 'id': item['id'],
                           'error': str(exc)})

    if errors:
        return jsonify({
            'message': f'{len(errors)} of {len(items)} rows rejected',
            'errors': errors,
        }), 400

    created = []
    for staged, row in prepared:
        txn = Transaction(
            user_id=g.user_id,
            type=row['type'],
            category=row['category'],
            amount=row['amount'],
            date=row['date'],
            description=row['description'],
            currency=row['currency'],
            exchange_rate=row['exchange_rate'],
        )
        db.session.add(txn)
        created.append((staged, txn, row))

    # Needed before linking created_txn_id, since ids are assigned on flush.
    db.session.flush()

    for staged, txn, row in created:
        staged.state = STAGED_CONFIRMED
        staged.created_txn_id = txn.id
        # Learning from the accepted pairing is what makes the next import
        # better than this one.
        categorize.learn(
            user_id=g.user_id,
            merchant_clean=staged.merchant_clean,
            category=row['category'],
            description=row['description'],
            txn_type=row['type'],
        )

    db.session.commit()

    return jsonify({
        'message': f'{len(created)} transactions created',
        'count': len(created),
        'created': [
            {'staged_id': s.id, 'transaction_id': t.id}
            for s, t, _ in created
        ],
    }), 201


@review_bp.route('/skip', methods=['POST'])
@login_required
def review_skip():
    """Skip rows, permanently.

    Recorded on the staged row itself, and since `dedup_hash` is unique per
    user, a later import of the same charge collides with this row rather than
    creating a fresh proposal. A skip therefore sticks.
    """
    body = request.get_json(silent=True) or {}
    ids = body.get('ids')

    if isinstance(body.get('id'), int):
        ids = [body['id']]
    if not isinstance(ids, list) or not ids:
        return jsonify({'message': 'ids must be a non-empty list'}), 400
    if not all(isinstance(i, int) for i in ids):
        return jsonify({'message': 'ids must be integers'}), 400

    rows = StagedTransaction.query.filter(
        StagedTransaction.user_id == g.user_id,
        StagedTransaction.id.in_(ids),
        StagedTransaction.state.in_(REVIEWABLE_STATES),
    ).all()

    for row in rows:
        row.state = STAGED_SKIPPED
    db.session.commit()

    return jsonify({
        'message': f'{len(rows)} rows skipped',
        'count': len(rows),
        'ids': [r.id for r in rows],
    })


@review_bp.route('/link', methods=['POST'])
@login_required
def review_link():
    """Resolve an ambiguous row by naming which tracked transaction it is.

    Also used to correct a wrong proposal: saying "this already exists" marks
    it matched instead of adding a duplicate.
    """
    body = request.get_json(silent=True) or {}
    staged_id = body.get('id')
    transaction_id = body.get('transaction_id')

    if not isinstance(staged_id, int) or not isinstance(transaction_id, int):
        return jsonify({
            'message': 'id and transaction_id are required integers'
        }), 400

    staged = StagedTransaction.query.filter_by(
        id=staged_id, user_id=g.user_id
    ).first()
    if staged is None:
        return jsonify({'message': 'Staged row not found'}), 404
    if staged.state not in REVIEWABLE_STATES:
        return jsonify({
            'message': 'row is not awaiting review', 'state': staged.state
        }), 409

    txn = Transaction.query.filter_by(
        id=transaction_id, user_id=g.user_id
    ).first()
    if txn is None:
        return jsonify({'message': 'Transaction not found'}), 404

    staged.state = STAGED_MATCHED
    staged.matched_txn_id = txn.id
    staged.candidate_ids = None
    staged.match_score = 1.0  # a person said so; that outranks the scorer
    db.session.commit()

    return jsonify({
        'message': 'Linked',
        'id': staged.id,
        'transaction_id': txn.id,
    })
