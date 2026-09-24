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

# How far apart the statement date and the tracked date may be. Wide on
# purpose: people log a purchase when they remember it, not when it happened.
DATE_SLACK_DAYS = 10

# --- the confidence equation ---------------------------------------------
#
#   score = amount + date + name            (capped at 1.0)
#
# Amount carries most of the weight, because it is the one field both sides
# record identically. Name is a medium factor that mostly helps: the user's own
# descriptions rarely look like the issuer's merchant string, so a mismatch
# costs very little, while a match -- or a pairing learned from an earlier
# month -- adds a lot.
#
#   amount  exact (within 0.01)            0.60
#           within 2 units                 0.50 -> 0.40, sliding with the gap
#           further                        not a candidate
#   date    0.20 * (1 - days / 12)         0.20 same day, 0.03 at 10 days
#   name    learned from a past pairing   +0.25
#           similar (>= 0.75)             +0.20
#           partly similar (>= 0.50)      +0.10
#           missing on either side         0
#           clearly different (< 0.30)    -0.05
#
# Worked through: exact amount, 3 days apart, names unrelated = 0.60 + 0.15 -
# 0.05 = 0.70, a match. 1.50 off, same day, unrelated names = 0.425 + 0.20 -
# 0.05 = 0.575, a possible match the wizard offers but does not decide.

AMOUNT_EXACT = 0.01
AMOUNT_NEAR_UNITS = 2.0
# In a foreign currency the app's own exchange rate will not equal the
# issuer's, so "near" scales with the amount there rather than staying at 2.
AMOUNT_NEAR_FOREIGN_RATE = 0.02

SCORE_AMOUNT_EXACT = 0.60
SCORE_AMOUNT_NEAR_MAX = 0.50
SCORE_AMOUNT_NEAR_MIN = 0.40

SCORE_DATE_MAX = 0.20

SCORE_NAME_LEARNED = 0.25
SCORE_NAME_SIMILAR = 0.20
SCORE_NAME_PARTIAL = 0.10
SCORE_NAME_MISMATCH = -0.05

NAME_SIMILAR = 0.75
NAME_PARTIAL = 0.50
NAME_MISMATCH = 0.30
# Two words are "the same" at this similarity (שופרסל / שופרסל-דיל, a typo).
WORD_ALIKE = 0.80
# Whole strings only count as similar when they are nearly identical.
STRING_ALIKE = 0.85

MATCH_THRESHOLD = 0.70
# Below a match but worth showing: the wizard offers these as "is it this
# one?" instead of leaving the user to hunt for it.
POSSIBLE_THRESHOLD = 0.45
# How far clear the best candidate must be before it is treated as *the* match
# rather than one of several.
RUNNER_UP_MARGIN = 0.10

# A past pairing only teaches a name alias if a person made it or the scorer
# was sure. Learning from shaky auto-matches would let one wrong guess
# reinforce itself every month.
LEARN_FROM_SCORE = 0.80

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
    """The widest gap at which two amounts can still be the same charge.

    Two units in shekels, where a gap means rounding or a tip. In any other
    currency the app stored a converted amount using its own `exchange_rate`,
    which will not equal the issuer's, so the allowance grows with the amount.
    """
    is_base = (currency or BASE_CURRENCY).upper() == BASE_CURRENCY
    if is_base:
        return AMOUNT_NEAR_UNITS
    return max(AMOUNT_NEAR_UNITS,
               abs(float(amount or 0)) * AMOUNT_NEAR_FOREIGN_RATE)


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
        return None
    days = abs((staged_date - txn_date).days)
    if days > DATE_SLACK_DAYS:
        return None
    return SCORE_DATE_MAX * (1 - days / (DATE_SLACK_DAYS + 2))


def _amount_score(staged_amount, txn_amount, tolerance):
    if staged_amount is None or txn_amount is None:
        return None
    delta = abs(float(staged_amount) - float(txn_amount))
    if delta <= AMOUNT_EXACT:
        return SCORE_AMOUNT_EXACT
    if delta > tolerance:
        return None
    slide = (delta - AMOUNT_EXACT) / (tolerance - AMOUNT_EXACT)
    return SCORE_AMOUNT_NEAR_MAX - slide * (
        SCORE_AMOUNT_NEAR_MAX - SCORE_AMOUNT_NEAR_MIN
    )


def name_similarity(a, b):
    """0..1, or None when either side has no name.

    Word-based, because whole-string character similarity is noise on short
    names: "hot mobile" and "phone bill" share enough letters to score 0.6.
    Instead: one name contained in the other, or the share of the shorter
    name's words that have a near-identical word on the other side. Whole-string
    similarity only counts when it is near-identical (a typo, a spacing change).
    """
    a, b = categorize.normalize(a), categorize.normalize(b)
    if not a or not b:
        return None
    # Containment, but not for a two-letter fragment that is inside everything.
    if min(len(a), len(b)) >= 3 and (a in b or b in a):
        return 1.0

    words_a = [w for w in a.split() if len(w) > 1]
    words_b = [w for w in b.split() if len(w) > 1]
    overlap = 0.0
    if words_a and words_b:
        shorter, longer = sorted((words_a, words_b), key=len)
        hits = sum(
            1 for w in shorter
            if any(
                w in v or v in w
                or difflib.SequenceMatcher(None, w, v).ratio() >= WORD_ALIKE
                for v in longer
            )
        )
        overlap = hits / len(shorter)

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(overlap, ratio if ratio >= STRING_ALIKE else 0.0)


def _name_score(staged, txn, aliases):
    description = categorize.normalize(txn.description)
    if description and description in aliases:
        return SCORE_NAME_LEARNED
    similarity = name_similarity(
        staged.merchant_clean or staged.merchant_raw, txn.description
    )
    if similarity is None:
        # No name on either side is not evidence against a match.
        return 0.0
    if similarity >= NAME_SIMILAR:
        return SCORE_NAME_SIMILAR
    if similarity >= NAME_PARTIAL:
        return SCORE_NAME_PARTIAL
    if similarity < NAME_MISMATCH:
        return SCORE_NAME_MISMATCH
    return 0.0


def learned_aliases(user_id, merchant):
    """Descriptions this merchant has been paired with before.

    The memory for repeating charges: once a monthly "HOT MOBILE" line has been
    paired with the user's "phone bill", every later month recognises it by
    name even though the two strings share nothing.
    """
    from models import STAGED_CONFIRMED, StagedTransaction

    key = categorize.clean_merchant(merchant)
    if not key:
        return frozenset()

    rows = (
        db.session.query(Transaction.description)
        .join(
            StagedTransaction,
            db.or_(
                StagedTransaction.matched_txn_id == Transaction.id,
                StagedTransaction.created_txn_id == Transaction.id,
            ),
        )
        .filter(
            StagedTransaction.user_id == user_id,
            StagedTransaction.merchant_clean == key,
            db.or_(
                StagedTransaction.state == STAGED_CONFIRMED,
                db.and_(
                    StagedTransaction.state == STAGED_MATCHED,
                    StagedTransaction.match_score >= LEARN_FROM_SCORE,
                ),
            ),
        )
        .all()
    )
    return frozenset(categorize.normalize(d) for (d,) in rows if d)


def score_candidate(staged, txn, aliases=frozenset()):
    tolerance = _amount_tolerance(staged.currency, staged.amount)
    amount = _amount_score(staged.amount, txn.amount, tolerance)
    if amount is None:
        return 0.0
    date = _date_score(staged.txn_date, txn.date)
    if date is None:
        return 0.0
    name = _name_score(staged, txn, aliases)
    return round(max(0.0, min(1.0, amount + date + name)), 4)


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
                  profile_kind=None, alias_cache=None):
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

    # A row can be reconciled again (a re-upload, or "re-check" in the
    # wizard), so clear the previous verdict's link before deciding afresh.
    staged.matched_txn_id = None

    merchant = staged.merchant_clean or staged.merchant_raw
    cache = alias_cache if alias_cache is not None else {}
    key = categorize.clean_merchant(merchant)
    if key not in cache:
        cache[key] = learned_aliases(staged.user_id, merchant)

    candidates = find_candidates(staged, claimed)
    scored = sorted(
        ((score_candidate(staged, t, cache[key]), t) for t in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    scored = [pair for pair in scored if pair[0] >= POSSIBLE_THRESHOLD]

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
    # near-misses so the wizard can offer them as "is it this one?".
    staged.state = STAGED_PROPOSED
    staged.match_score = best_score
    staged.candidate_ids = [t.id for _, t in scored[:3]]
    return staged


def paired_txn_ids(user_id):
    """Transactions some staged row is already paired with.

    Passed as `claimed_ids` when re-matching the queue, so a transaction the
    user just linked to one charge cannot be handed to a second, identical one
    (two ₪45 Wolt orders the same evening).
    """
    from models import STAGED_CONFIRMED, StagedTransaction

    rows = db.session.query(
        StagedTransaction.matched_txn_id, StagedTransaction.created_txn_id
    ).filter(
        StagedTransaction.user_id == user_id,
        StagedTransaction.state.in_((STAGED_MATCHED, STAGED_CONFIRMED)),
    ).all()
    return {i for pair in rows for i in pair if i is not None}


def reconcile_batch(staged_rows, profile_kind=None, exclude_patterns=None,
                    apply_suggestions=True, claimed_ids=None):
    """Reconcile a whole batch and return counters for the ImportBatch row."""
    claimed = set(claimed_ids or ())
    aliases = {}
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
            alias_cache=aliases,
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
