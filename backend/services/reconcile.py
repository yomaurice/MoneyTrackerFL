"""Match ingested rows against tracked transactions and decide a verdict.

The point of the whole pipeline is one bucket: `proposed` -- a real charge with
no counterpart in the tracked data, i.e. an expense that was missed. Everything
else here exists to keep that bucket honest, because a false positive costs the
user a duplicate row and a false negative costs them the thing they asked for.

Three properties worth stating, since they drive the code:

*Idempotent.* A `dedup_hash` is computed per row and unique per user, so
re-running an import, or a wallet notification arriving by two paths, converges
on one staged row.

*Never guesses between two plausible matches.* Two ₪50 coffees on the same day
become `ambiguous` and get asked about. Picking one would silently corrupt the
data in a way nobody would ever notice.

*Suppresses the aggregate card charge.* One Leumi line "מקס 3,214" is the same
money as ~40 Max card rows. Counting both would double the month.
"""
import datetime
import difflib
import hashlib
import re

from models import (
    STAGED_AMBIGUOUS,
    STAGED_IGNORED,
    STAGED_MATCHED,
    STAGED_PROPOSED,
    Transaction,
    db,
)
from services import categorize

# A card purchase date and its posting date differ by days, and the tracked
# transaction may carry either.
DATE_SLACK_DAYS = 5

# Exact for ILS: the issuer and the user are reading the same shekel figure.
# Loose for foreign currency, where the app's exchange_rate will not equal the
# issuer's, so the converted amounts legitimately disagree.
AMOUNT_TOL_SAME_CURRENCY = 0.0
AMOUNT_TOL_FOREIGN = 0.02
AMOUNT_TOL_ABSOLUTE = 0.02

MATCH_THRESHOLD = 0.85
# How far clear the best candidate must be before it is treated as *the* match
# rather than one of several.
RUNNER_UP_MARGIN = 0.10

WEIGHT_AMOUNT = 0.55
WEIGHT_DATE = 0.25
WEIGHT_DESCRIPTION = 0.20

# Bank lines that are really the monthly card settlement. Suppressing these is
# the double-count guard.
DEFAULT_CARD_AGGREGATE_PATTERNS = (
    r'מקס',
    r'\bmax\b',
    r'ישראכרט',
    r'isracard',
    r'כאל',
    r'cal\b',
    r'visa\s*cal',
    r'לאומי\s*קארד',
    r'אמריקן\s*אקספרס',
    r'american\s*express',
    r'\bamex\b',
    r'כרטיסי\s*אשראי',
)

IGNORED_CARD_AGGREGATE = 'card_aggregate'
IGNORED_PENDING = 'pending'
IGNORED_DUPLICATE = 'duplicate'


def compute_dedup_hash(source, source_profile_id, external_id,
                       txn_date, amount, currency, merchant):
    """Stable identity for an ingested row.

    Prefers the issuer's own identifier, which is authoritative. Falls back to
    the shape of the transaction, which is what a wallet notification gives.
    """
    if external_id:
        basis = f'{source_profile_id}|{external_id}'
    else:
        date_part = txn_date.isoformat() if txn_date else ''
        amount_part = f'{float(amount):.2f}' if amount is not None else ''
        basis = '|'.join([
            source or '',
            date_part,
            amount_part,
            (currency or '').upper(),
            categorize.normalize(merchant),
        ])
    return hashlib.sha256(basis.encode('utf-8')).hexdigest()


BASE_CURRENCY = 'ILS'


def _amount_tolerance(currency, amount):
    """How far apart two amounts may be and still be the same charge.

    Keyed on whether the charge is in the base currency, not on whether the two
    sides agree with each other. In shekels both sides are reading the same
    figure, so any gap is a different charge. In any other currency the app
    stored a converted amount using its own `exchange_rate`, which will not
    equal the issuer's, so the two legitimately disagree by a fraction.
    """
    is_base = (currency or BASE_CURRENCY).upper() == BASE_CURRENCY
    rate = AMOUNT_TOL_SAME_CURRENCY if is_base else AMOUNT_TOL_FOREIGN
    return max(AMOUNT_TOL_ABSOLUTE, abs(float(amount or 0)) * rate)


def is_card_aggregate(text, patterns=None):
    """True when a bank line is the monthly card settlement, not a purchase."""
    haystack = categorize.normalize(text)
    if not haystack:
        return False
    for pattern in (patterns or DEFAULT_CARD_AGGREGATE_PATTERNS):
        try:
            if re.search(pattern, haystack, re.IGNORECASE):
                return True
        except re.error:
            # A user-supplied pattern that does not compile must not take the
            # whole reconcile run down with it.
            continue
    return False


def _date_score(staged_date, txn_date):
    if not staged_date or not txn_date:
        return 0.0
    days = abs((staged_date - txn_date).days)
    if days > DATE_SLACK_DAYS:
        return 0.0
    return 1.0 - (days / (DATE_SLACK_DAYS + 1))


def _amount_score(staged_amount, txn_amount, tolerance):
    if staged_amount is None or txn_amount is None:
        return 0.0
    delta = abs(float(staged_amount) - float(txn_amount))
    if delta > tolerance:
        return 0.0
    if tolerance == 0:
        return 1.0
    return 1.0 - (delta / tolerance) * 0.15  # near-exact stays near-1


def _description_score(staged_text, txn_text):
    a = categorize.normalize(staged_text)
    b = categorize.normalize(txn_text)
    if not a or not b:
        # No description on either side is not evidence against a match, so
        # this stays neutral rather than scoring zero and sinking a good pair.
        return 0.5
    return difflib.SequenceMatcher(None, a, b).ratio()


def score_candidate(staged, txn):
    tolerance = _amount_tolerance(staged.currency, staged.amount)
    amount = _amount_score(staged.amount, txn.amount, tolerance)
    if amount == 0.0:
        return 0.0
    date = _date_score(staged.txn_date, txn.date)
    if date == 0.0:
        return 0.0
    description = _description_score(
        staged.merchant_clean or staged.merchant_raw, txn.description
    )
    return round(
        WEIGHT_AMOUNT * amount
        + WEIGHT_DATE * date
        + WEIGHT_DESCRIPTION * description,
        4,
    )


def find_candidates(staged, claimed_ids=()):
    """Tracked transactions that could plausibly be this staged row.

    Filtered in SQL first so a long history does not get pulled into memory.
    """
    if staged.amount is None or staged.txn_date is None:
        return []

    tolerance = _amount_tolerance(staged.currency, staged.amount)

    low = staged.txn_date - datetime.timedelta(days=DATE_SLACK_DAYS)
    high = staged.txn_date + datetime.timedelta(days=DATE_SLACK_DAYS)

    query = Transaction.query.filter(
        Transaction.user_id == staged.user_id,
        Transaction.date >= low,
        Transaction.date <= high,
        Transaction.amount >= float(staged.amount) - tolerance,
        Transaction.amount <= float(staged.amount) + tolerance,
    )
    if staged.type:
        query = query.filter(Transaction.type == staged.type)
    if staged.currency:
        # A $50 charge and a ₪50 charge are not the same money.
        query = query.filter(Transaction.currency == staged.currency)

    return [t for t in query.all() if t.id not in claimed_ids]


def reconcile_row(staged, claimed_ids=None, exclude_patterns=None,
                  profile_kind=None):
    """Decide one staged row's verdict in place. Caller commits.

    `claimed_ids` is carried across a run so two staged rows cannot both claim
    the same tracked transaction -- otherwise a genuine second charge of the
    same amount would be matched away and never proposed.
    """
    claimed = claimed_ids if claimed_ids is not None else set()

    # 1. Pending authorisations change amount or vanish, then reappear as
    #    completed. Staging them would propose a charge that never happened.
    if (staged.issuer_status or '').lower() in ('pending', 'ממתין', 'טרם נקלט'):
        staged.state = STAGED_IGNORED
        staged.ignored_reason = IGNORED_PENDING
        return staged

    # 2. The aggregate card charge on a bank statement.
    if profile_kind == 'bank' and is_card_aggregate(
        staged.merchant_raw or staged.merchant_clean, exclude_patterns
    ):
        staged.state = STAGED_IGNORED
        staged.ignored_reason = IGNORED_CARD_AGGREGATE
        return staged

    candidates = find_candidates(staged, claimed)
    scored = sorted(
        ((score_candidate(staged, t), t) for t in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    scored = [pair for pair in scored if pair[0] > 0]

    if not scored:
        staged.state = STAGED_PROPOSED
        staged.match_score = None
        staged.candidate_ids = None
        return staged

    best_score, best = scored[0]
    strong = [pair for pair in scored if pair[0] >= MATCH_THRESHOLD]

    if len(strong) > 1 and (strong[0][0] - strong[1][0]) <= RUNNER_UP_MARGIN:
        # Two equally good explanations. Asking costs a click; guessing costs
        # silent data corruption.
        staged.state = STAGED_AMBIGUOUS
        staged.match_score = best_score
        staged.candidate_ids = [t.id for _, t in strong[:5]]
        return staged

    if best_score >= MATCH_THRESHOLD:
        staged.state = STAGED_MATCHED
        staged.matched_txn_id = best.id
        staged.match_score = best_score
        claimed.add(best.id)
        return staged

    # Something similar exists but not similar enough. Propose it, and keep the
    # near-misses so the wizard can show why it is not being called a match.
    staged.state = STAGED_PROPOSED
    staged.match_score = best_score
    staged.candidate_ids = [t.id for _, t in scored[:3]]
    return staged


def reconcile_batch(staged_rows, profile_kind=None, exclude_patterns=None,
                    apply_suggestions=True):
    """Reconcile a whole batch and return counters for the ImportBatch row."""
    claimed = set()
    counts = {
        'received': len(staged_rows),
        'matched': 0,
        'proposed': 0,
        'ambiguous': 0,
        'suppressed': 0,
    }

    for staged in staged_rows:
        reconcile_row(
            staged,
            claimed_ids=claimed,
            exclude_patterns=exclude_patterns,
            profile_kind=profile_kind,
        )

        if apply_suggestions and staged.state in (
            STAGED_PROPOSED, STAGED_AMBIGUOUS
        ):
            guess = categorize.suggest(
                staged.user_id,
                staged.merchant_raw,
                staged.memo,
                staged.type,
            )
            staged.merchant_clean = guess['merchant_clean']
            staged.suggested_category = guess['category']
            staged.suggested_description = guess['description']
            staged.category_confidence = guess['confidence']

        if staged.state == STAGED_MATCHED:
            counts['matched'] += 1
        elif staged.state == STAGED_PROPOSED:
            counts['proposed'] += 1
        elif staged.state == STAGED_AMBIGUOUS:
            counts['ambiguous'] += 1
        elif staged.state == STAGED_IGNORED:
            counts['suppressed'] += 1

    db.session.flush()
    return counts
