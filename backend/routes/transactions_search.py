"""Search tracked transactions by month, date range, amount and text.

Built for the review wizard: deciding whether a statement line is already
tracked means looking at that month's entries, and scrolling the dashboard's
latest-100 list does not get you there.
"""
import datetime

from flask import Blueprint, g, jsonify, request

from auth import login_required
from models import Transaction, db

transactions_search_bp = Blueprint('transactions_search', __name__)

MAX_RESULTS = 1000
DEFAULT_AMOUNT_TOLERANCE = 2.0


def _parse_day(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _month_bounds(value):
    """'2026-09' -> (1 Sep, 30 Sep), or None."""
    try:
        start = datetime.datetime.strptime(value, '%Y-%m').date()
    except (TypeError, ValueError):
        return None
    following = (start.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return start, following - datetime.timedelta(days=1)


@transactions_search_bp.route('/api/transactions/search', methods=['GET'])
@login_required
def search_transactions():
    """Filters, all optional and combined with AND:

    month=YYYY-MM, or from=YYYY-MM-DD and/or to=YYYY-MM-DD
    q=text          substring of description or category, case-insensitive
    amount=N        within `tolerance` of N (default 2)
    type=expense|income
    """
    args = request.args
    query = Transaction.query.filter(Transaction.user_id == g.user_id)

    if args.get('month'):
        bounds = _month_bounds(args['month'])
        if bounds is None:
            return jsonify({'message': 'month must look like 2026-09'}), 400
        query = query.filter(Transaction.date.between(*bounds))

    for name, op in (('from', '__ge__'), ('to', '__le__')):
        if args.get(name):
            day = _parse_day(args[name])
            if day is None:
                return jsonify({
                    'message': f'{name} must be a date like 2026-09-01'
                }), 400
            query = query.filter(getattr(Transaction.date, op)(day))

    if args.get('q'):
        pattern = f"%{args['q'].strip()}%"
        query = query.filter(db.or_(
            Transaction.description.ilike(pattern),
            Transaction.category.ilike(pattern),
        ))

    if args.get('amount'):
        try:
            amount = float(args['amount'])
            tolerance = float(args.get('tolerance', DEFAULT_AMOUNT_TOLERANCE))
        except ValueError:
            return jsonify({'message': 'amount and tolerance must be numbers'}), 400
        query = query.filter(
            Transaction.amount.between(amount - tolerance, amount + tolerance)
        )

    if args.get('type') in ('expense', 'income'):
        query = query.filter(Transaction.type == args['type'])

    rows = (
        query.order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(MAX_RESULTS + 1)
        .all()
    )

    return jsonify({
        'items': [{
            'id': t.id,
            'date': t.date.isoformat(),
            'amount': t.amount,
            'currency': t.currency or 'ILS',
            'type': t.type,
            'category': t.category,
            'description': t.description,
        } for t in rows[:MAX_RESULTS]],
        # Said out loud so the UI can ask for a narrower filter rather than
        # silently showing a partial month.
        'truncated': len(rows) > MAX_RESULTS,
        'total_amount': round(sum(
            t.amount * (1 if t.type == 'expense' else -1)
            for t in rows[:MAX_RESULTS]
        ), 2),
    })
