"""Read a payment notification into amount, currency, merchant and direction.

Shared by the phone's background send and the /add page's save, so both
read a notification identically. The page has a TypeScript twin
(MoneyTracker_frontend/src/utils/walletParse.ts) for the instant prefill;
both are held to tests/fixtures/wallet_notifications.json.

Deliberately forgiving. Google Wallet's wording varies by language, version
and card, and has not been pinned down for Israel. So: the amount is found
wherever a currency sits next to a number, the merchant is the title unless
the title is just the app's name, and a notification that yields no amount is
still staged -- with its text visible in the review card -- rather than lost.
"""
import re

# Bidirectional marks Android sprinkles around numbers in RTL text.
_BIDI = re.compile('[‎‏‪-‮⁦-⁩]')

_CURRENCIES = (
    ('ש"ח', 'ILS'), ('ש״ח', 'ILS'), ('₪', 'ILS'), ('ILS', 'ILS'), ('NIS', 'ILS'),
    ('$', 'USD'), ('USD', 'USD'),
    ('€', 'EUR'), ('EUR', 'EUR'),
    ('£', 'GBP'), ('GBP', 'GBP'),
)
_CUR = '|'.join(re.escape(sign) for sign, _ in _CURRENCIES)
_NUM = r'\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?'

_AMOUNT_PATTERNS = (
    re.compile(rf'(?P<cur>{_CUR})\s*(?P<num>{_NUM})'),
    re.compile(rf'(?P<num>{_NUM})\s*(?P<cur>{_CUR})'),
)

# Titles that name the app rather than the shop.
_GENERIC_TITLES = (
    'google wallet', 'google pay', 'gpay', 'wallet', 'ארנק google',
    'google ארנק', 'ארנק',
)

# Where a merchant sits inside the text when the title is generic.
_MERCHANT_IN_TEXT = re.compile(
    r'(?:\bat\s+|\bב-|\bאצל\s+)(?P<m>.+?)'
    r'(?=\s+(?:with|using|עם|באמצעות|בכרטיס)\b|$)'
)

_REFUND_WORDS = ('החזר', 'זיכוי', 'refund', 'refunded', 'credited')


def _currency(sign):
    for known, code in _CURRENCIES:
        if sign == known:
            return code
    return None


def _number(text):
    """'1,234.50' -> 1234.5; '4,50' -> 4.5. The last separator is decimal
    only when two or fewer digits follow it."""
    match = re.search(r'[.,](\d{1,2})$', text)
    if match:
        whole = re.sub(r'[.,]', '', text[:match.start()])
        return float(f'{whole}.{match.group(1)}')
    return float(re.sub(r'[.,]', '', text))


def _clean(text):
    return re.sub(r'\s+', ' ', _BIDI.sub('', text or '')).strip()


def parse(title, text):
    """{'amount', 'currency', 'merchant', 'type'}; amount None if not found."""
    title, text = _clean(title), _clean(text)
    both = f'{title} {text}'

    amount = currency = None
    for pattern in _AMOUNT_PATTERNS:
        match = pattern.search(text) or pattern.search(title)
        if match:
            amount = _number(match.group('num'))
            currency = _currency(match.group('cur'))
            break

    merchant = None
    title_is_generic = title.lower() in _GENERIC_TITLES
    if title and not title_is_generic and not any(
        p.search(title) for p in _AMOUNT_PATTERNS
    ):
        merchant = title
    elif amount is not None:
        found = _MERCHANT_IN_TEXT.search(text)
        if found:
            merchant = found.group('m').strip(' .,')

    lowered = both.lower()
    is_refund = any(word in lowered for word in _REFUND_WORDS)

    return {
        'amount': amount,
        'currency': currency,
        'merchant': merchant or None,
        'type': 'income' if is_refund else 'expense',
    }
