"""Merchant cleanup, and guessing a category and description from it.

A note on what was reusable from `Money_Tracker_Importer`. The plan expected
`BASE_CATEGORY_MAP` there to be a merchant -> category mapper worth lifting
wholesale. It isn't: its keys are Google Sheet budget-line labels
("סופר", "אינטרנט + ביטוחים"), which never appear in a bank or card statement
row. "שופרסל דיל רמת אביב 442" does not match the key "סופר" by any means. So
the merchant keyword layer below is new work, because nothing equivalent
existed.

What did carry over is `is_income_row`'s vocabulary, and the *idea* behind
`refine_mixed_category` -- that a note disambiguates an otherwise ambiguous
charge -- generalised here into keyword rules rather than sheet-specific
special cases.

Deliberately no hardcoded category vocabulary. Categories are per-user rows in
the `category` table, and inventing names the user does not use would produce
suggestions they have to correct every time. Keywords map to a *concept*, and
the concept is then resolved against that user's own categories. No match means
no suggestion, which is honest: the wizard asks instead of guessing wrong.
"""
import difflib
import re
import unicodedata

from models import Category, MerchantRule

# Confidence bands. The wizard shows these, so they need to mean something:
# a rule the user taught us is near-certain, a keyword guess is a hint.
CONFIDENCE_RULE = 0.95
CONFIDENCE_KEYWORD = 0.6
CONFIDENCE_NONE = 0.0

# Lifted from Money_Tracker_Importer.is_income_row.
INCOME_WORDS = (
    'משכורת', 'הכנסה', 'הכנסות', 'מלגה', 'החזר מס', 'בונוס', 'שכירות',
    'salary', 'refund', 'reimbursement',
)

# Noise that carries no identity: branch names, city names, card references
# and the payment-rail prefixes that wrap the real merchant.
_PREFIX_NOISE = (
    'bit', 'paypal', 'pay', 'google', 'apple pay', 'gpay', 'ebay',
    'ביט', 'פייפאל', 'פייבוקס', 'paybox',
)

_SUFFIX_NOISE_PATTERNS = (
    r'\bכרטיס\s*\d+\b',          # "כרטיס 1234"
    r'\bסניף\s*\d+\b',            # "סניף 42"
    r'\bבע"?מ\b',                 # Ltd
    r'\bltd\b', r'\binc\b',
    r'\btel\s*aviv\b',
    r'\d{1,2}/\d{1,2}',           # installment "3/12" -- captured separately
)

# Merchant keyword -> concept. Concepts are matched against the user's own
# category names, so these are intentionally generic.
MERCHANT_KEYWORDS = (
    # groceries
    (('שופרסל', 'רמי לוי', 'רמי לוי', 'ויקטורי', 'יינות ביתן', 'טיב טעם',
      'אושר עד', 'מגה', 'am pm', 'am:pm', 'סופר', 'מכולת', 'shufersal',
      'rami levy', 'tiv taam', 'yochananof', 'יוחננוף'), 'groceries'),
    # fuel
    (('פז', 'דלק', 'סונול', 'דור אלון', 'טן', 'paz', 'sonol', 'delek',
      'ten ', 'yellow'), 'fuel'),
    # parking
    (('חניון', 'חניה', 'פנגו', 'pango', 'cellopark', 'סלופארק', 'parking'),
     'parking'),
    # pharmacy / health
    (('סופר פארם', 'super pharm', 'be פארם', 'בית מרקחת', 'מכבי', 'כללית',
      'קופת חולים', 'pharmacy', 'clalit', 'maccabi'), 'pharmacy'),
    # dining
    (('מסעדה', 'קפה', 'ארומה', 'קופיקס', 'מקדונלד', 'burger', 'pizza',
      'פיצה', 'wolt', 'וולט', 'tenbis', 'תן ביס', 'restaurant', 'cafe'),
     'dining'),
    # transport
    (('רב קו', 'רב-קו', 'אגד', 'דן ', 'רכבת', 'gett', 'גט', 'uber',
      'מטרו', 'moovit'), 'transport'),
    # utilities
    (('חשמל', 'electric'), 'electricity'),
    (('מים', 'מקורות', 'water'), 'water'),
    (('ארנונה', 'עירייה', 'עיריית', 'municipal'), 'municipal taxes'),
    # comms
    (('בזק', 'הוט', 'סלקום', 'פרטנר', 'פלאפון', 'יס', 'partner', 'cellcom',
      'hot ', 'bezeq', 'internet', 'אינטרנט'), 'phones and internet'),
    # insurance -- the refine_mixed_category idea, as keywords
    (('ביטוח', 'פוליסה', 'הרel', 'הראל', 'מגדל', 'כלל ביטוח', 'insurance',
      'policy'), 'insurances'),
    # subscriptions
    (('netflix', 'spotify', 'youtube', 'disney', 'icloud', 'google one',
      'openai', 'anthropic', 'microsoft', 'adobe'), 'subscriptions'),
    # online
    (('aliexpress', 'amazon', 'ebay', 'asos', 'shein', 'temu', 'ali express'),
     'online orders'),
    # kids
    (('מעון', 'גן ילדים', 'צהרון', 'בית ספר', 'school', 'kindergarten'),
     'kids education'),
    # housing
    (('משכנתא', 'mortgage'), 'mortgage'),
)


def strip_niqqud(text):
    """Remove Hebrew vowel marks so 'שׁוֹפֶּרסל' and 'שופרסל' compare equal."""
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', text)
        if not unicodedata.combining(ch)
    )


def normalize(text):
    """Casefold, strip niqqud and punctuation, collapse whitespace."""
    if not text:
        return ''
    out = strip_niqqud(str(text)).lower()
    out = out.replace('״', '"').replace('׳', "'")
    out = re.sub(r'[^\w\s"\'/-]', ' ', out, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', out).strip()


def extract_installment(text):
    """Pull "3/12" out of a merchant string. Returns (no, total) or (None, None).

    Card statements carry the instalment counter inside the description, and it
    must not be treated as part of the merchant name -- otherwise the same shop
    looks like twelve different merchants.
    """
    if not text:
        return None, None
    match = re.search(r'\b(\d{1,2})\s*/\s*(\d{1,2})\b', str(text))
    if not match:
        return None, None
    no, total = int(match.group(1)), int(match.group(2))
    if total < 2 or no < 1 or no > total:
        return None, None
    return no, total


def clean_merchant(raw):
    """Reduce a statement description to a stable merchant name.

    'שופרסל דיל רמת אביב 442' -> 'שופרסל דיל רמת אביב'. Branch numbers, card
    references and rail prefixes go; the words that identify the shop stay.
    """
    if not raw:
        return ''

    text = normalize(raw)

    for pattern in _SUFFIX_NOISE_PATTERNS:
        text = re.sub(pattern, ' ', text)

    # Rail prefixes: 'paypal *acme' / 'bit acme' -> 'acme'
    for prefix in _PREFIX_NOISE:
        text = re.sub(rf'^{re.escape(prefix)}\s*\*?\s*', '', text)

    # Trailing standalone numbers are branch or terminal ids, never identity.
    text = re.sub(r'(\s+\d{1,6})+$', '', text)
    text = re.sub(r'\s+', ' ', text).strip(' -*/')

    # If stripping removed everything, the digits *were* the name.
    return text or normalize(raw)


def is_income(text):
    """Lifted from Money_Tracker_Importer.is_income_row."""
    haystack = normalize(text)
    return any(normalize(word) in haystack for word in INCOME_WORDS)


def match_concept(merchant_clean, memo=None):
    """First keyword concept the merchant or memo hits, else None."""
    haystack = f'{normalize(merchant_clean)} {normalize(memo)}'.strip()
    if not haystack:
        return None

    for keywords, concept in MERCHANT_KEYWORDS:
        for keyword in keywords:
            if normalize(keyword) and normalize(keyword) in haystack:
                return concept
    return None


def _user_categories(user_id, txn_type):
    rows = Category.query.filter_by(user_id=user_id).all()
    # `type` is nullable on legacy rows, so treat a missing type as usable
    # rather than hiding the user's own categories from them.
    return [r for r in rows if not r.type or not txn_type or r.type == txn_type]


def resolve_concept_to_category(user_id, concept, txn_type):
    """Map a concept onto one of *this user's* categories, or None.

    Never invents a category. A guess the user does not use is worse than no
    guess: they have to correct it every time, and it pollutes their list.
    """
    if not concept:
        return None

    names = [c.name for c in _user_categories(user_id, txn_type) if c.name]
    if not names:
        return None

    target = normalize(concept)
    for name in names:
        if normalize(name) == target:
            return name

    # Substring either way: 'groceries' vs 'Groceries & household'.
    for name in names:
        norm = normalize(name)
        if target and (target in norm or norm in target):
            return name

    close = difflib.get_close_matches(target, [normalize(n) for n in names],
                                      n=1, cutoff=0.85)
    if close:
        for name in names:
            if normalize(name) == close[0]:
                return name
    return None


def _rule_for(user_id, merchant_clean, memo):
    """The learned rule that best fits, preferring the most-used one."""
    haystack = f'{normalize(merchant_clean)} {normalize(memo)}'.strip()
    if not haystack:
        return None

    rules = (
        MerchantRule.query
        .filter_by(user_id=user_id)
        .order_by(MerchantRule.hits.desc())
        .all()
    )

    for rule in rules:
        if not rule.pattern:
            continue
        if rule.is_regex:
            try:
                if re.search(rule.pattern, haystack, re.IGNORECASE):
                    return rule
            except re.error:
                # A pattern the user typed badly must not break every guess.
                continue
        elif normalize(rule.pattern) in haystack:
            return rule
    return None


def suggest(user_id, merchant_raw, memo=None, txn_type=None):
    """Suggest (category, description, confidence) for a staged row.

    Both fields stay editable in the wizard; this only decides the default.
    """
    merchant_clean = clean_merchant(merchant_raw)

    rule = _rule_for(user_id, merchant_clean, memo)
    if rule is not None:
        return {
            'merchant_clean': merchant_clean,
            'category': rule.category,
            'description': rule.description or merchant_clean or None,
            'confidence': CONFIDENCE_RULE,
            'source': 'rule',
        }

    concept = match_concept(merchant_clean, memo)
    category = resolve_concept_to_category(user_id, concept, txn_type)

    return {
        'merchant_clean': merchant_clean,
        'category': category,
        'description': merchant_clean or None,
        'confidence': CONFIDENCE_KEYWORD if category else CONFIDENCE_NONE,
        'source': 'keyword' if category else 'none',
    }


def learn(user_id, merchant_clean, category, description, txn_type):
    """Remember a confirmed merchant -> (category, description) pairing.

    Called when a proposal is accepted, which is what makes the next import
    better than this one. Caller commits.
    """
    pattern = (merchant_clean or '').strip()
    if not pattern or not category:
        return None

    rule = MerchantRule.query.filter_by(
        user_id=user_id, pattern=pattern
    ).first()

    if rule is None:
        rule = MerchantRule(
            user_id=user_id,
            pattern=pattern,
            is_regex=False,
            auto_learned=True,
            hits=1,
        )
        db_add = True
    else:
        rule.hits = (rule.hits or 0) + 1
        db_add = False

    # The latest decision wins: a merchant whose category the user changes has
    # been recategorised, not mis-clicked.
    rule.category = category
    rule.description = description or None
    rule.txn_type = txn_type

    if db_add:
        from models import db
        db.session.add(rule)
    return rule
