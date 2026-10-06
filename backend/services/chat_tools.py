"""Read-only data tools for the chat assistant.

The model never sees the database directly. It calls these functions by name,
and every one of them is pinned to the asking user's id here, not by anything
the model sends, so a question cannot reach another user's rows. None of them
write.

Totals are computed in SQL so the model reports sums instead of adding up rows
itself, which is where language models make arithmetic mistakes. Amounts are
converted the same way Analytics does it: `amount * exchange_rate`.
"""
import datetime

from sqlalchemy import func, or_

from models import Category, Transaction

MAX_ROWS = 100
MAX_GROUPS = 200
GROUP_BY = ('month', 'year', 'category', 'description', 'none')
TYPES = ('income', 'expense')


class ToolError(ValueError):
    """A bad argument from the model. Returned to it so it can retry."""


def _date(value, field):
    if value in (None, ''):
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f'{field} must be YYYY-MM-DD, got {value!r}')


def _converted():
    return Transaction.amount * func.coalesce(Transaction.exchange_rate, 1.0)


def _filtered(user_id, start_date=None, end_date=None, type=None,
              categories=None, keywords=None):
    start = _date(start_date, 'start_date')
    end = _date(end_date, 'end_date')
    if type not in (None, '') and type not in TYPES:
        raise ToolError(f'type must be one of {TYPES}')

    q = Transaction.query.filter(Transaction.user_id == user_id)
    if start:
        q = q.filter(Transaction.date >= start)
    if end:
        q = q.filter(Transaction.date <= end)
    if type:
        q = q.filter(Transaction.type == type)
    if categories:
        q = q.filter(func.lower(Transaction.category).in_(
            [c.lower() for c in categories]))
    if keywords:
        # Any keyword, in either the description or the category name. Wide on
        # purpose: "car" should find a "Car insurance" category and a "Paz
        # fuel car wash" description alike.
        clauses = []
        for kw in keywords:
            pattern = f'%{kw}%'
            clauses.append(Transaction.description.ilike(pattern))
            clauses.append(Transaction.category.ilike(pattern))
        q = q.filter(or_(*clauses))
    return q


def list_categories(user_id):
    rows = (
        _filtered(user_id)
        .with_entities(
            Transaction.category, Transaction.type,
            func.count(Transaction.id), func.min(Transaction.date),
            func.max(Transaction.date),
        )
        .group_by(Transaction.category, Transaction.type)
        .order_by(Transaction.type, Transaction.category)
        .all()
    )
    used = {(r[0], r[1]) for r in rows}
    result = [{
        'category': r[0], 'type': r[1], 'transactions': r[2],
        'first': r[3].isoformat(), 'last': r[4].isoformat(),
    } for r in rows]
    # Categories that exist but have never been used still say something about
    # how the user organises their money.
    for c in Category.query.filter_by(user_id=user_id).all():
        if (c.name, c.type) not in used:
            result.append({'category': c.name, 'type': c.type,
                           'transactions': 0})
    return {'categories': result}


def search_transactions(user_id, start_date=None, end_date=None, type=None,
                        categories=None, keywords=None, limit=50):
    q = _filtered(user_id, start_date, end_date, type, categories, keywords)
    limit = max(1, min(int(limit or 50), MAX_ROWS))

    count, total = q.with_entities(
        func.count(Transaction.id), func.coalesce(func.sum(_converted()), 0.0)
    ).one()
    rows = (
        q.order_by(Transaction.date.desc(), Transaction.id.desc())
        .limit(limit).all()
    )
    return {
        'matching_count': count,
        'matching_total': round(float(total), 2),
        'returned': len(rows),
        'transactions': [{
            'date': t.date.isoformat(),
            'type': t.type,
            'category': t.category,
            'description': t.description or '',
            'amount': round(t.amount * (t.exchange_rate or 1.0), 2),
            'original_amount': t.amount,
            'original_currency': t.currency or 'ILS',
        } for t in rows],
    }


def summarize(user_id, start_date=None, end_date=None, type=None,
              categories=None, keywords=None, group_by='none'):
    if group_by not in GROUP_BY:
        raise ToolError(f'group_by must be one of {GROUP_BY}')
    q = _filtered(user_id, start_date, end_date, type, categories, keywords)

    if group_by == 'none':
        key = None
    elif group_by == 'month':
        key = func.to_char(Transaction.date, 'YYYY-MM')
    elif group_by == 'year':
        key = func.to_char(Transaction.date, 'YYYY')
    elif group_by == 'category':
        key = Transaction.category
    else:
        key = func.coalesce(Transaction.description, '')

    # Income and expense are kept apart; a single signed total hides which
    # side moved.
    columns = [Transaction.type, func.count(Transaction.id),
               func.sum(_converted())]
    if key is None:
        rows = q.with_entities(*columns).group_by(Transaction.type).all()
        return {'totals': [{
            'type': r[0], 'transactions': r[1], 'total': round(float(r[2]), 2),
        } for r in rows]}

    rows = (
        q.with_entities(key, *columns)
        .group_by(key, Transaction.type)
        .order_by(key)
        .limit(MAX_GROUPS + 1)
        .all()
    )
    return {
        'group_by': group_by,
        'truncated': len(rows) > MAX_GROUPS,
        'groups': [{
            'group': r[0], 'type': r[1], 'transactions': r[2],
            'total': round(float(r[3]), 2),
        } for r in rows[:MAX_GROUPS]],
    }


def _months(start, end):
    return (end.year - start.year) * 12 + end.month - start.month + 1


def compare_periods(user_id, before_start, before_end, after_start, after_end,
                    type=None, categories=None, keywords=None):
    """Totals for two date ranges under the same filters, and the change.

    Before/after questions are where the model most wants to do sums of its
    own, so the whole comparison is computed here.
    """
    result = {}
    for label, start, end in (('before', before_start, before_end),
                              ('after', after_start, after_end)):
        s, e = _date(start, f'{label}_start'), _date(end, f'{label}_end')
        if not s or not e:
            raise ToolError(f'{label}_start and {label}_end are required')
        if s > e:
            raise ToolError(f'{label}_start is after {label}_end')
        rows = (
            _filtered(user_id, s.isoformat(), e.isoformat(), type,
                      categories, keywords)
            .with_entities(Transaction.type, func.count(Transaction.id),
                           func.sum(_converted()))
            .group_by(Transaction.type).all()
        )
        months = _months(s, e)
        result[label] = {
            'start': s.isoformat(), 'end': e.isoformat(), 'months': months,
            'by_type': {r[0]: {
                'transactions': r[1], 'total': round(float(r[2]), 2),
                'monthly_average': round(float(r[2]) / months, 2),
            } for r in rows},
        }

    change = {}
    for t in TYPES:
        b = result['before']['by_type'].get(t, {}).get('total', 0.0)
        a = result['after']['by_type'].get(t, {}).get('total', 0.0)
        if b or a:
            change[t] = {
                'after_minus_before': round(a - b, 2),
                'percent': round((a - b) / b * 100, 1) if b else None,
            }
    result['change'] = change
    return result


_FILTERS = {
    'start_date': {'type': 'string',
                   'description': 'Inclusive, YYYY-MM-DD. Omit for no lower bound.'},
    'end_date': {'type': 'string',
                 'description': 'Inclusive, YYYY-MM-DD. Omit for no upper bound.'},
    'type': {'type': 'string', 'enum': list(TYPES),
             'description': 'Omit for both income and expense.'},
    'categories': {'type': 'array', 'items': {'type': 'string'},
                   'description': 'Exact category names (case-insensitive). '
                                  'Use list_categories to see them.'},
    'keywords': {'type': 'array', 'items': {'type': 'string'},
                 'description': 'Match if ANY keyword appears in the '
                                'description or category name '
                                '(case-insensitive substring). Include '
                                'Hebrew and English variants and merchant '
                                'names.'},
}

DECLARATIONS = [
    {
        'name': 'list_categories',
        'description': "List the user's categories with transaction counts "
                       'and the date range each was used in.',
        'parameters': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'search_transactions',
        'description': 'Find individual transactions. Also returns the count '
                       'and total of ALL matches, even beyond the returned '
                       f'rows (max {MAX_ROWS}).',
        'parameters': {
            'type': 'object',
            'properties': {
                **_FILTERS,
                'limit': {'type': 'integer',
                          'description': f'Rows to return, 1-{MAX_ROWS}.'},
            },
        },
    },
    {
        'name': 'summarize',
        'description': 'Totals and counts, split by income/expense, '
                       'optionally grouped. Use this for any "how much" '
                       'question instead of adding rows yourself.',
        'parameters': {
            'type': 'object',
            'properties': {
                **_FILTERS,
                'group_by': {'type': 'string', 'enum': list(GROUP_BY)},
            },
        },
    },
    {
        'name': 'compare_periods',
        'description': 'Compare totals between two date ranges under the same '
                       'filters, with the difference already computed. Use '
                       'this for any before/after or this-year-vs-last-year '
                       'question.',
        'parameters': {
            'type': 'object',
            'properties': {
                'before_start': {'type': 'string', 'description': 'YYYY-MM-DD'},
                'before_end': {'type': 'string', 'description': 'YYYY-MM-DD'},
                'after_start': {'type': 'string', 'description': 'YYYY-MM-DD'},
                'after_end': {'type': 'string', 'description': 'YYYY-MM-DD'},
                **{k: v for k, v in _FILTERS.items()
                   if k not in ('start_date', 'end_date')},
            },
            'required': ['before_start', 'before_end', 'after_start',
                         'after_end'],
        },
    },
]

_TOOLS = {
    'list_categories': list_categories,
    'search_transactions': search_transactions,
    'summarize': summarize,
    'compare_periods': compare_periods,
}


def run(user_id, name, args):
    """Run one tool call. Errors come back as data so the model can recover."""
    fn = _TOOLS.get(name)
    if fn is None:
        return {'error': f'unknown tool {name!r}'}
    try:
        return fn(user_id, **dict(args or {}))
    except (ToolError, TypeError, ValueError) as exc:
        return {'error': str(exc)}
