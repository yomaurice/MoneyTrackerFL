"""Wallet capture: the phone's background send, and the /add page's save.

Two paths carry the same payment and must land on one row:

- The phone automation POSTs the notification to /api/ingest/wallet with an
  ingest token, the moment it arrives. A safety net: the charge reaches the
  review queue even if the notification is swiped away.
- Tapping the notification opens /add prefilled; saving there goes through
  /api/ingest/wallet/confirm with the browser session.

Both key the staged row on the notification's timestamp, which the
automation passes to both, so retries and the two paths converge through the
(user_id, dedup_hash) unique index instead of duplicating.
"""
import datetime

from flask import Blueprint, g, jsonify, request

from auth import login_required, token_required
from models import (
    STAGED_CONFIRMED,
    STAGED_IGNORED,
    STAGED_MATCHED,
    StagedTransaction,
    Transaction,
    db,
)
from services import categorize, reconcile, wallet_parse
from services.validation import ValidationError, validate_transaction

ingest_bp = Blueprint('ingest', __name__, url_prefix='/api')

MAX_TEXT = 500
LOCAL_TZ = 'Asia/Jerusalem'


def _local_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(LOCAL_TZ)).date()
    except Exception:  # pragma: no cover - no tz database on this host
        return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).date()


def _date_from_ts(ts):
    """The payment's date from the automation's timestamp, else today.

    Accepts epoch seconds or milliseconds, or an ISO date/datetime -- whatever
    the phone app's "system time" turns out to produce.
    """
    text = str(ts or '').strip()
    if text.isdigit():
        seconds = int(text) / (1000 if len(text) > 11 else 1)
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.fromtimestamp(
                seconds, ZoneInfo(LOCAL_TZ)).date()
        except Exception:  # pragma: no cover
            return datetime.datetime.utcfromtimestamp(seconds + 3 * 3600).date()
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        return _local_today()


def _read_notification(body):
    """(title, text, ts) from a request body, trimmed, or raise ValueError."""
    if not isinstance(body, dict):
        raise ValueError('expected a JSON object')
    title = str(body.get('title') or '')[:MAX_TEXT]
    text = str(body.get('text') or '')[:MAX_TEXT]
    ts = str(body.get('ts') or '').strip()[:40]
    if not (title.strip() or text.strip()):
        raise ValueError('title or text is required')
    return title, text, ts


def _staged_for(user_id, title, text, ts):
    """The staged row for this notification: existing, or new and unsaved."""
    parsed = wallet_parse.parse(title, text)
    day = _date_from_ts(ts)
    # With a timestamp, two identical coffees are still two charges; without
    # one, identity falls back to the charge's shape on that day.
    external_id = f'{ts}|{parsed["amount"]}' if ts else None
    dedup = reconcile.compute_dedup_hash(
        'wallet', None, external_id, day, parsed['amount'],
        parsed['currency'], parsed['merchant'],
    )

    existing = StagedTransaction.query.filter_by(
        user_id=user_id, dedup_hash=dedup
    ).first()
    if existing is not None:
        return existing, False

    staged = StagedTransaction(
        user_id=user_id,
        source='wallet',
        external_id=external_id,
        dedup_hash=dedup,
        # Shown on the review card when the parser found no amount.
        raw={'cells': [c for c in (title, text) if c], 'ts': ts},
        txn_date=day,
        amount=parsed['amount'],
        currency=parsed['currency'] or 'ILS',
        type=parsed['type'],
        merchant_raw=parsed['merchant'],
        merchant_clean=categorize.clean_merchant(parsed['merchant']) or None,
    )
    return staged, True


@ingest_bp.route('/ingest/wallet', methods=['POST'])
@token_required
def ingest_wallet():
    """Stage a payment notification for review. Token auth; stages only.

    Takes title / text / ts as a JSON body, a form, or query parameters. The
    phone setup uses query parameters: MacroDroid's key/value rows encode each
    value properly, where a hand-typed JSON body breaks on the first quote or
    line break inside a notification.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {**request.form.to_dict(), **request.args.to_dict()}
    try:
        title, text, ts = _read_notification(body)
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400

    staged, created = _staged_for(g.user_id, title, text, ts)
    if not created:
        # A retry, or the /add page got there first. Either way, done.
        return jsonify({'message': 'Already received', 'id': staged.id,
                        'state': staged.state}), 200

    reconcile.reconcile_batch([staged])
    db.session.add(staged)
    db.session.commit()
    return jsonify({'message': 'Received', 'id': staged.id,
                    'state': staged.state,
                    'parsed': staged.amount is not None}), 201


@ingest_bp.route('/ingest/wallet/confirm', methods=['POST'])
@login_required
def confirm_wallet():
    """Save the /add page's form as a real transaction, exactly once.

    Body: the notification (title, text, ts) plus the form's fields. If this
    payment was already saved, says so instead of saving it twice -- the page
    may retry from its offline queue. If the matcher already paired it with a
    transaction the user typed in by hand, says that too, and only saves on
    `force`.
    """
    body = request.get_json(silent=True)
    try:
        title, text, ts = _read_notification(body)
        row = validate_transaction(body)
    except (ValueError, ValidationError) as exc:
        return jsonify({'message': str(exc)}), 400

    staged, created = _staged_for(g.user_id, title, text, ts)

    if staged.state == STAGED_CONFIRMED:
        return jsonify({'message': 'Already saved', 'already': True,
                        'transaction_id': staged.created_txn_id}), 200

    if created:
        reconcile.reconcile_batch([staged], apply_suggestions=False)
        db.session.add(staged)

    # Matched by the scorer, or marked "Already in" during review.
    if staged.state in (STAGED_MATCHED, STAGED_IGNORED) and not body.get('force'):
        db.session.commit()
        txn = (db.session.get(Transaction, staged.matched_txn_id)
               if staged.matched_txn_id else None)
        return jsonify({
            'message': 'This looks like a transaction you already added',
            'matched': {
                'id': txn.id,
                'date': txn.date.isoformat(),
                'amount': txn.amount,
                'description': txn.description,
                'category': txn.category,
            } if txn else None,
        }), 409

    txn = Transaction(
        user_id=g.user_id,
        type=row['type'],
        category=row['category'],
        amount=row['amount'],
        date=row['date'],
        description=row['description'] or '',
        currency=row['currency'],
        exchange_rate=row['exchange_rate'],
    )
    db.session.add(txn)
    db.session.flush()

    staged.state = STAGED_CONFIRMED
    staged.created_txn_id = txn.id
    staged.matched_txn_id = None
    staged.reviewed_at = datetime.datetime.utcnow()
    categorize.learn(
        user_id=g.user_id,
        merchant_clean=staged.merchant_clean,
        category=row['category'],
        description=row['description'],
        txn_type=row['type'],
    )
    db.session.commit()

    return jsonify({'message': 'Saved', 'transaction_id': txn.id}), 201


@ingest_bp.route('/review/suggest', methods=['GET'])
@login_required
def suggest_category():
    """Category and description guess for a merchant, for the /add form."""
    merchant = (request.args.get('merchant') or '')[:MAX_TEXT]
    txn_type = request.args.get('type') or 'expense'
    guess = categorize.suggest(g.user_id, merchant, None, txn_type)
    return jsonify({
        'category': guess['category'],
        'description': guess['description'],
        'confidence': guess['confidence'],
    })
