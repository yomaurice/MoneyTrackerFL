"""Parse a bank or card statement export into staged-transaction dicts.

Israeli institutions all export XLSX or CSV, and all of them export something
slightly wrong: junk banner rows above the real header, merged title cells,
Hebrew column names that vary by institution and change without notice, credits
expressed as a separate column on one statement and a negative number on the
next.

So there is deliberately no per-institution parser. Instead:

1. Find the header row by scanning the first rows for one that looks like
   headers -- a date-like column and an amount-like column together.
2. Map those headers onto our fields using the institution's saved mapping,
   falling back to keyword matching.
3. Hand back rows plus the mapping that was used, so the UI can confirm or
   correct it and save it to `SourceProfile.column_mapping`.

A format change is then fixed by remapping in the UI, not by shipping code.
Legacy `.xls` is not supported -- save as `.xlsx` first.
"""
import csv
import datetime
import io
import re

from services import categorize

MAX_HEADER_SCAN_ROWS = 25
MAX_ROWS = 5000

# Our field names, and the header keywords that suggest each one. Order
# matters: the first field whose keyword matches a header claims it.
FIELD_KEYWORDS = (
    ('txn_date', (
        'תאריך עסקה', 'תאריך ביצוע', 'תאריך רכישה', 'תאריך',
        'transaction date', 'purchase date', 'date',
    )),
    ('posted_date', (
        'תאריך חיוב', 'תאריך ערך', 'מועד חיוב',
        'posting date', 'value date', 'charge date',
    )),
    ('merchant_raw', (
        'שם בית עסק', 'בית עסק', 'תאור', 'תיאור', 'פרטים', 'שם העסק',
        'merchant', 'description', 'details', 'business',
    )),
    ('amount', (
        'סכום חיוב', 'סכום העסקה', 'סכום', 'חובה', 'debit',
        'amount', 'charge', 'sum',
    )),
    ('credit_amount', (
        'זכות', 'credit',
    )),
    ('currency', (
        'מטבע', 'סוג מטבע', 'currency',
    )),
    ('memo', (
        'הערות', 'notes', 'memo', 'comment',
    )),
    ('issuer_status', (
        'סטטוס', 'status',
    )),
    ('external_id', (
        'אסמכתא', 'מספר אסמכתא', 'reference', 'ref', 'id',
    )),
)

REQUIRED_FIELDS = ('txn_date', 'amount')

_DATE_FORMATS = (
    '%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d.%m.%Y', '%d.%m.%y',
    '%d-%m-%Y', '%m/%d/%Y',
)

_CURRENCY_SIGNS = {
    '₪': 'ILS', 'nis': 'ILS', 'ש"ח': 'ILS', 'שח': 'ILS',
    '$': 'USD', 'usd': 'USD',
    '€': 'EUR', 'eur': 'EUR',
    '£': 'GBP', 'gbp': 'GBP',
}


class StatementError(ValueError):
    """A file we cannot make sense of, with a message fit for the user."""


def _norm_header(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip().lower()


def parse_date(value):
    """Parse a statement date cell, or return None.

    Israeli exports are day-first, so `03/04/2026` is 3 April. `%m/%d/%Y` is
    tried last purely as a fallback for an English-locale export, and only
    matters for days above 12 where day-first would have failed anyway.
    """
    if value in (None, ''):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value

    text = str(value).strip()
    if not text:
        return None
    # Some exports carry a time component.
    text = text.split(' ')[0]

    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value):
    """Parse an amount cell, or return None.

    Handles thousands separators, currency signs, trailing/leading minus, and
    parenthesised negatives. Returns a signed float; the caller decides the
    transaction's direction.
    """
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    negative = text.startswith('-') or text.endswith('-')
    if text.startswith('(') and text.endswith(')'):
        negative = True

    cleaned = re.sub(r'[^\d.,]', '', text)
    if not cleaned:
        return None

    # With both separators present, whichever comes last is the decimal point:
    # "1,234.56" is comma-grouped, "1.234,56" is the European form. Assuming
    # the comma is always the group separator turned 1.234,56 into 1.23456.
    if ',' in cleaned and '.' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        if re.search(r',\d{3}(\D|$)', cleaned + ' '):
            cleaned = cleaned.replace(',', '')
        else:
            cleaned = cleaned.replace(',', '.')

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    return -amount if negative and amount > 0 else amount


def detect_currency(*values):
    for value in values:
        if not value:
            continue
        text = str(value).strip().lower()
        for sign, code in _CURRENCY_SIGNS.items():
            if sign in text:
                return code
        if len(text) == 3 and text.isalpha():
            return text.upper()
    return None


def _looks_like_header(cells):
    """A header row has a date-ish and an amount-ish column name."""
    headers = [_norm_header(c) for c in cells]
    if sum(1 for h in headers if h) < 2:
        return False

    def hits(field):
        keywords = dict(FIELD_KEYWORDS)[field]
        return any(k in h for h in headers for k in
                   (kw.lower() for kw in keywords))

    return hits('txn_date') and (hits('amount') or hits('credit_amount'))


def guess_mapping(headers):
    """Map our field names onto column indexes by keyword.

    Longest keyword first, so "תאריך חיוב" is read as the posting date rather
    than being swallowed by the plain "תאריך" rule.
    """
    normalised = [_norm_header(h) for h in headers]
    mapping = {}
    claimed = set()

    candidates = []
    for field, keywords in FIELD_KEYWORDS:
        for keyword in keywords:
            candidates.append((len(keyword), field, keyword.lower()))
    candidates.sort(reverse=True)

    for _, field, keyword in candidates:
        if field in mapping:
            continue
        for index, header in enumerate(normalised):
            if index in claimed or not header:
                continue
            if keyword in header:
                mapping[field] = index
                claimed.add(index)
                break

    return mapping


def _read_xlsx(data):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise StatementError(
            'That file could not be opened as an .xlsx workbook. Legacy .xls '
            'is not supported -- open it and save as .xlsx first.'
        ) from exc

    sheet = workbook[workbook.sheetnames[0]]
    rows = []
    for row in sheet.iter_rows(values_only=True):
        rows.append(list(row))
        if len(rows) > MAX_ROWS + MAX_HEADER_SCAN_ROWS:
            break
    workbook.close()
    return rows


def _read_csv(data):
    for encoding in ('utf-8-sig', 'utf-8', 'cp1255', 'iso-8859-8'):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise StatementError('That CSV is not in a text encoding we recognise.')

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel

    return [row for row in csv.reader(io.StringIO(text), dialect)]


def read_table(filename, data):
    """Read a statement file into a list of raw rows."""
    lowered = (filename or '').lower()
    if lowered.endswith('.xlsx') or lowered.endswith('.xlsm'):
        return _read_xlsx(data)
    if lowered.endswith('.csv') or lowered.endswith('.txt'):
        return _read_csv(data)
    if lowered.endswith('.xls'):
        raise StatementError(
            'Legacy .xls files are not supported. Open it and save as .xlsx.'
        )
    raise StatementError('Upload an .xlsx or .csv statement export.')


def find_header(rows):
    """Index of the header row, or None.

    Statements routinely open with a bank logo, an account number and a couple
    of blank rows, so the header is rarely row 0.
    """
    for index, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        if row and _looks_like_header(row):
            return index
    return None


def parse_statement(filename, data, mapping=None, header_row=None):
    """Parse a statement into row dicts.

    Returns {headers, mapping, rows, skipped, header_row}. `mapping` is echoed
    back so the caller can save a corrected one against the SourceProfile.
    """
    table = read_table(filename, data)
    if not table:
        raise StatementError('That file is empty.')

    if header_row is not None:
        header_index = header_row
    else:
        header_index = find_header(table)

    if header_index is None:
        if mapping:
            # Detection only has to succeed when we are guessing. Requiring it
            # here would break the manual-mapping escape hatch in exactly the
            # case it exists for: a file whose headers we cannot recognise.
            header_index = 0
        else:
            raise StatementError(
                'Could not find a header row with a date and an amount '
                'column. Check this is the transactions export rather than a '
                'summary, or map the columns by hand.'
            )

    if header_index >= len(table):
        raise StatementError('That header row is past the end of the file.')

    headers = [h if h is not None else '' for h in table[header_index]]
    resolved = dict(mapping) if mapping else guess_mapping(headers)

    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing and 'amount' in missing and 'credit_amount' in resolved:
        missing.remove('amount')
    if missing:
        raise StatementError(
            'Could not identify these columns: ' + ', '.join(missing) +
            '. Map them by hand and the choice will be remembered.'
        )

    def cell(row, field):
        index = resolved.get(field)
        if index is None or index >= len(row):
            return None
        return row[index]

    rows, skipped = [], 0
    for raw_row in table[header_index + 1:]:
        if not raw_row or all(c in (None, '') for c in raw_row):
            continue
        if len(rows) >= MAX_ROWS:
            skipped += 1
            continue

        txn_date = parse_date(cell(raw_row, 'txn_date'))
        debit = parse_amount(cell(raw_row, 'amount'))
        credit = parse_amount(cell(raw_row, 'credit_amount'))

        # A statement may carry credits in their own column, or as a negative
        # in the debit column. Both mean money coming in.
        if debit in (None, 0) and credit not in (None, 0):
            signed = -abs(credit)
        elif debit is not None:
            signed = debit
        else:
            signed = None

        if txn_date is None or signed is None or signed == 0:
            # A total row, a section break, or a line we cannot read. Counted
            # rather than silently dropped, so the UI can say so.
            skipped += 1
            continue

        merchant = cell(raw_row, 'merchant_raw')
        merchant = '' if merchant is None else str(merchant).strip()
        installment_no, installment_total = categorize.extract_installment(merchant)

        rows.append({
            'txn_date': txn_date,
            'posted_date': parse_date(cell(raw_row, 'posted_date')),
            # Amount is stored positive; direction lives in `type`.
            'amount': abs(signed),
            'type': 'income' if signed < 0 else 'expense',
            'currency': detect_currency(
                cell(raw_row, 'currency'), cell(raw_row, 'amount')
            ) or 'ILS',
            'merchant_raw': merchant or None,
            'merchant_clean': categorize.clean_merchant(merchant) or None,
            'memo': _as_text(cell(raw_row, 'memo')),
            'issuer_status': _as_text(cell(raw_row, 'issuer_status')),
            'external_id': _as_text(cell(raw_row, 'external_id')),
            'installment_no': installment_no,
            'installment_total': installment_total,
            'raw': [None if c is None else str(c) for c in raw_row],
        })

    if not rows:
        raise StatementError(
            'No transaction rows could be read from that file. '
            f'{skipped} rows were skipped as headers, totals or blanks.'
        )

    return {
        'headers': [str(h) for h in headers],
        'mapping': resolved,
        'rows': rows,
        'skipped': skipped,
        'header_row': header_index,
    }


def _as_text(value):
    if value in (None, ''):
        return None
    text = str(value).strip()
    return text[:300] or None
