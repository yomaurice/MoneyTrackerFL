"""Statement parsing.

The fixtures here are deliberately messy in the ways real Israeli exports are
messy: banner rows above the header, Hebrew column names that differ per
institution, day-first dates, thousands separators, credits in their own
column, and total rows at the bottom.
"""
import datetime
import io

import pytest

from services import statements
from services.statements import StatementError, parse_statement


def xlsx(rows):
    """Build an .xlsx in memory from a list of rows."""
    from openpyxl import Workbook

    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- cell parsing --------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('03/04/2026', datetime.date(2026, 4, 3)),      # day-first
    ('3/4/26', datetime.date(2026, 4, 3)),
    ('2026-04-03', datetime.date(2026, 4, 3)),
    ('03.04.2026', datetime.date(2026, 4, 3)),
    ('03/04/2026 14:22', datetime.date(2026, 4, 3)),
    (datetime.datetime(2026, 4, 3, 9, 0), datetime.date(2026, 4, 3)),
    ('', None),
    (None, None),
    ('not a date', None),
])
def test_dates_are_parsed_day_first(raw, expected):
    assert statements.parse_date(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('52.90', 52.90),
    ('1,234.56', 1234.56),
    ('₪ 3,214.00', 3214.00),
    ('-45.00', -45.00),
    ('45.00-', -45.00),          # trailing minus, as some exports emit
    ('(45.00)', -45.00),
    ('1.234,56', 1234.56),       # European decimal comma
    (52.9, 52.9),
    ('', None),
    ('abc', None),
])
def test_amounts_are_parsed(raw, expected):
    assert statements.parse_amount(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('₪', 'ILS'), ('ש"ח', 'ILS'), ('$', 'USD'), ('USD', 'USD'),
    ('€', 'EUR'), ('gbp', 'GBP'), ('', None), (None, None),
])
def test_currency_is_detected(raw, expected):
    assert statements.detect_currency(raw) == expected


# --- header detection ----------------------------------------------------

def test_the_header_is_found_below_banner_rows():
    """Statements open with a logo, an account number and blank rows."""
    data = xlsx([
        ['בנק לאומי', None, None],
        ['חשבון 12-345-6789', None, None],
        [None, None, None],
        ['תאריך', 'תאור', 'סכום'],
        ['03/04/2026', 'שופרסל דיל', '52.90'],
    ])
    parsed = parse_statement('leumi.xlsx', data)

    assert parsed['header_row'] == 3
    assert len(parsed['rows']) == 1


def test_a_file_with_no_header_is_refused_with_a_useful_message():
    data = xlsx([['summary'], ['total spent'], ['3214']])
    with pytest.raises(StatementError) as exc:
        parse_statement('summary.xlsx', data)
    assert 'header row' in str(exc.value)


def test_posting_date_is_not_swallowed_by_the_generic_date_rule():
    """"תאריך חיוב" must map to posted_date, not to txn_date."""
    headers = ['תאריך עסקה', 'תאריך חיוב', 'שם בית עסק', 'סכום חיוב']
    mapping = statements.guess_mapping(headers)

    assert mapping['txn_date'] == 0
    assert mapping['posted_date'] == 1
    assert mapping['merchant_raw'] == 2
    assert mapping['amount'] == 3


# --- whole files ---------------------------------------------------------

def test_a_card_statement_parses():
    data = xlsx([
        ['פירוט עסקאות', None, None, None],
        ['תאריך עסקה', 'שם בית עסק', 'סכום חיוב', 'מטבע'],
        ['03/04/2026', 'שופרסל דיל רמת אביב 442', '52.90', '₪'],
        ['05/04/2026', 'IKEA 3/12', '1,200.00', '₪'],
        ['סה"כ', None, '1,252.90', None],   # total row
    ])
    parsed = parse_statement('max.xlsx', data)

    assert len(parsed['rows']) == 2
    assert parsed['skipped'] == 1           # the total row

    first = parsed['rows'][0]
    assert first['txn_date'] == datetime.date(2026, 4, 3)
    assert first['amount'] == 52.90
    assert first['type'] == 'expense'
    assert first['currency'] == 'ILS'
    # Branch number stripped, so the same shop is one merchant.
    assert first['merchant_clean'] == 'שופרסל דיל רמת אביב'

    second = parsed['rows'][1]
    assert second['amount'] == 1200.00
    assert (second['installment_no'], second['installment_total']) == (3, 12)


def test_credits_in_their_own_column_become_income():
    data = xlsx([
        ['תאריך', 'תאור', 'חובה', 'זכות'],
        ['03/04/2026', 'שופרסל', '52.90', None],
        ['04/04/2026', 'משכורת', None, '9,500.00'],
    ])
    parsed = parse_statement('leumi.xlsx', data)

    assert [r['type'] for r in parsed['rows']] == ['expense', 'income']
    # Amounts are stored positive; direction lives in `type`.
    assert [r['amount'] for r in parsed['rows']] == [52.90, 9500.00]


def test_a_negative_debit_is_also_income():
    data = xlsx([
        ['תאריך', 'תאור', 'סכום'],
        ['03/04/2026', 'החזר', '-120.00'],
    ])
    parsed = parse_statement('x.xlsx', data)

    assert parsed['rows'][0]['type'] == 'income'
    assert parsed['rows'][0]['amount'] == 120.00


def test_csv_with_hebrew_windows_encoding_parses():
    text = 'תאריך,תאור,סכום\r\n03/04/2026,שופרסל,52.90\r\n'
    parsed = parse_statement('leumi.csv', text.encode('cp1255'))

    assert len(parsed['rows']) == 1
    assert parsed['rows'][0]['merchant_raw'] == 'שופרסל'


def test_semicolon_delimited_csv_parses():
    text = 'תאריך;תאור;סכום\n03/04/2026;שופרסל;52,90\n'
    parsed = parse_statement('x.csv', text.encode('utf-8'))
    assert parsed['rows'][0]['amount'] == 52.90


def test_an_explicit_mapping_overrides_detection():
    """The escape hatch: a format change is fixed by remapping, not by code."""
    data = xlsx([
        ['col a', 'col b', 'col c'],
        ['03/04/2026', 'שופרסל', '52.90'],
    ])
    with pytest.raises(StatementError):
        parse_statement('unknown.xlsx', data)   # headers mean nothing

    parsed = parse_statement(
        'unknown.xlsx', data,
        mapping={'txn_date': 0, 'merchant_raw': 1, 'amount': 2},
    )
    assert parsed['rows'][0]['amount'] == 52.90


def test_legacy_xls_is_refused_with_instructions():
    with pytest.raises(StatementError) as exc:
        parse_statement('old.xls', b'\xd0\xcf\x11\xe0')
    assert '.xlsx' in str(exc.value)


def test_an_unknown_extension_is_refused():
    with pytest.raises(StatementError):
        parse_statement('statement.pdf', b'%PDF-1.4')


def test_a_corrupt_xlsx_is_refused_rather_than_crashing():
    with pytest.raises(StatementError):
        parse_statement('broken.xlsx', b'not really a zip file')


def test_an_empty_file_is_refused():
    with pytest.raises(StatementError):
        parse_statement('empty.xlsx', xlsx([]))


def test_a_file_with_only_unreadable_rows_says_so():
    data = xlsx([
        ['תאריך', 'תאור', 'סכום'],
        ['סה"כ', None, None],
        [None, None, None],
    ])
    with pytest.raises(StatementError) as exc:
        parse_statement('x.xlsx', data)
    assert 'No transaction rows' in str(exc.value)
