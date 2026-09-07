"""Validation for incoming transaction payloads.

Written for the bulk insert, but deliberately shared: `POST /api/transactions`
does `data['type']` straight off an unchecked body today, so a malformed
request becomes a 500 rather than a 400. One validator means one definition of
what a transaction is allowed to be, whether it arrives one at a time or a
hundred at once.
"""
import datetime

TRANSACTION_TYPES = ('income', 'expense')

MAX_BULK_ROWS = 500
MAX_AMOUNT = 10_000_000
MAX_DESCRIPTION = 300
MAX_CATEGORY = 120
MAX_CURRENCY = 10
MAX_RECURRENCE_MONTHS = 120


class ValidationError(ValueError):
    """A rejected payload, with a message safe to return to the client."""


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def parse_date(value, field='date'):
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    _require(isinstance(value, str) and value.strip(), f'{field} is required')
    try:
        return datetime.datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        raise ValidationError(f'{field} must be YYYY-MM-DD, got {value!r}')


def validate_transaction(payload, index=None):
    """Return a normalised transaction dict, or raise ValidationError.

    Amounts are stored as positive numbers with direction carried by `type`,
    matching how the existing rows are shaped, so a negative input is a sign
    error rather than an expense.
    """
    where = '' if index is None else f' (row {index})'

    if not isinstance(payload, dict):
        raise ValidationError(f'each transaction must be an object{where}')

    txn_type = payload.get('type')
    _require(
        txn_type in TRANSACTION_TYPES,
        f"type must be one of {', '.join(TRANSACTION_TYPES)}{where}",
    )

    category = payload.get('category')
    _require(
        isinstance(category, str) and category.strip(),
        f'category is required{where}',
    )
    category = category.strip()
    _require(
        len(category) <= MAX_CATEGORY,
        f'category exceeds {MAX_CATEGORY} characters{where}',
    )

    raw_amount = payload.get('amount')
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        raise ValidationError(f'amount must be a number{where}')
    _require(amount == amount and abs(amount) != float('inf'),
             f'amount must be a finite number{where}')
    _require(amount > 0, f'amount must be greater than zero{where}')
    _require(amount <= MAX_AMOUNT, f'amount exceeds the maximum{where}')

    date = parse_date(payload.get('date'), f'date{where}')

    description = payload.get('description') or ''
    _require(isinstance(description, str), f'description must be text{where}')
    description = description.strip()
    _require(
        len(description) <= MAX_DESCRIPTION,
        f'description exceeds {MAX_DESCRIPTION} characters{where}',
    )

    currency = (payload.get('currency') or 'ILS')
    _require(isinstance(currency, str) and currency.strip(),
             f'currency must be text{where}')
    currency = currency.strip().upper()
    _require(len(currency) <= MAX_CURRENCY,
             f'currency exceeds {MAX_CURRENCY} characters{where}')

    raw_rate = payload.get('exchange_rate', 1.0)
    if raw_rate in (None, ''):
        raw_rate = 1.0
    try:
        exchange_rate = float(raw_rate)
    except (TypeError, ValueError):
        raise ValidationError(f'exchange_rate must be a number{where}')
    _require(exchange_rate > 0, f'exchange_rate must be positive{where}')

    return {
        'type': txn_type,
        'category': category,
        'amount': amount,
        'date': date,
        'description': description or None,
        'currency': currency,
        'exchange_rate': exchange_rate,
    }


def validate_transaction_list(payload):
    """Validate a whole batch, reporting *every* bad row rather than the first.

    A 40-row import that fails on row 1 and again on row 12 should say so once,
    not force twelve round-trips to discover the second problem.
    """
    _require(isinstance(payload, list), 'expected a list of transactions')
    _require(payload, 'no transactions supplied')
    _require(
        len(payload) <= MAX_BULK_ROWS,
        f'at most {MAX_BULK_ROWS} transactions per request, got {len(payload)}',
    )

    cleaned, errors = [], []
    for i, row in enumerate(payload):
        try:
            cleaned.append(validate_transaction(row, index=i))
        except ValidationError as exc:
            errors.append({'index': i, 'error': str(exc)})

    if errors:
        raise ValidationError(errors)

    return cleaned


def validate_recurrence_months(value):
    """Cap recurrence so one request cannot fan out into unbounded inserts."""
    if value in (None, ''):
        return 0
    try:
        months = int(value)
    except (TypeError, ValueError):
        raise ValidationError('recurrence_months must be a whole number')
    _require(months >= 0, 'recurrence_months cannot be negative')
    _require(
        months <= MAX_RECURRENCE_MONTHS,
        f'recurrence_months cannot exceed {MAX_RECURRENCE_MONTHS}',
    )
    return months
