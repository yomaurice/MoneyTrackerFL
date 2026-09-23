"""Merchant cleanup and category guessing."""
import pytest

from models import Category, MerchantRule, db
from services import categorize

USER = '__pytest_categorize_user__'


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def add_category(uid, name, type_='expense'):
    row = Category(user_id=uid, name=name, type=type_)
    db.session.add(row)
    db.session.flush()
    return row


# --- cleanup -------------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('שופרסל דיל רמת אביב 442', 'שופרסל דיל רמת אביב'),
    ('שופרסל   דיל', 'שופרסל דיל'),
    ('PAYPAL *STEAM GAMES', 'steam games'),
    ('BIT העברה', 'העברה'),
    ('רמי לוי סניף 12', 'רמי לוי'),
    ('AMAZON.COM 1234', 'amazon com'),
])
def test_branch_numbers_and_rail_prefixes_are_stripped(raw, expected):
    assert categorize.clean_merchant(raw) == expected


def test_cleanup_never_returns_empty_for_a_numeric_name():
    """If the digits were the whole name, keep them rather than losing the row."""
    assert categorize.clean_merchant('12345').strip() != ''


def test_niqqud_does_not_change_identity():
    assert categorize.normalize('שׁוֹפֶּרסל') == categorize.normalize('שופרסל')


@pytest.mark.parametrize('raw, expected', [
    ('שופרסל דיל 3/12', (3, 12)),
    ('IKEA 1/6', (1, 6)),
    ('no instalments here', (None, None)),
    ('13/12', (None, None)),   # no > total
    ('5/1', (None, None)),     # total < 2
])
def test_instalment_counters_are_extracted(raw, expected):
    assert categorize.extract_installment(raw) == expected


def test_the_instalment_counter_is_not_part_of_the_merchant():
    """Otherwise one shop looks like twelve different merchants."""
    a = categorize.clean_merchant('שופרסל דיל 3/12')
    b = categorize.clean_merchant('שופרסל דיל 4/12')
    assert a == b


# --- income detection (lifted from Money_Tracker_Importer) ---------------

@pytest.mark.parametrize('text', ['משכורת אוגוסט', 'החזר מס', 'מלגה', 'salary'])
def test_income_words_are_recognised(text):
    assert categorize.is_income(text)


@pytest.mark.parametrize('text', ['שופרסל דיל', 'פז דלק', ''])
def test_expenses_are_not_income(text):
    assert not categorize.is_income(text)


# --- guessing ------------------------------------------------------------

def test_a_keyword_resolves_to_the_users_own_category(app, uid):
    with app.app_context():
        add_category(uid, 'zzGroceries')
        out = categorize.suggest(uid, 'שופרסל דיל רמת אביב 442')

        assert out['category'] == 'zzGroceries'
        assert out['confidence'] == categorize.CONFIDENCE_KEYWORD
        assert out['merchant_clean'] == 'שופרסל דיל רמת אביב'
        db.session.rollback()


def test_no_matching_category_means_no_suggestion(app, uid):
    """Inventing a category the user does not use is worse than asking."""
    with app.app_context():
        add_category(uid, 'zzRent')
        out = categorize.suggest(uid, 'שופרסל דיל')

        assert out['category'] is None
        assert out['confidence'] == categorize.CONFIDENCE_NONE
        # The description still gets a sensible default.
        assert out['description'] == 'שופרסל דיל'
        db.session.rollback()


def test_a_users_categories_are_never_borrowed_from_another(app, uid, make_user):
    other = make_user('__pytest_categorize_other__')
    with app.app_context():
        add_category(other, 'zzGroceries')
        out = categorize.suggest(uid, 'שופרסל דיל')
        assert out['category'] is None
        db.session.rollback()


def test_category_names_match_loosely(app, uid):
    with app.app_context():
        add_category(uid, 'zzGroceries & household')
        out = categorize.suggest(uid, 'רמי לוי')
        assert out['category'] == 'zzGroceries & household'
        db.session.rollback()


def test_a_learned_rule_beats_a_keyword(app, uid):
    with app.app_context():
        add_category(uid, 'zzGroceries')
        categorize.learn(uid, 'שופרסל דיל', 'zzDate night', 'Dinner', 'expense')
        db.session.commit()

        out = categorize.suggest(uid, 'שופרסל דיל רמת אביב 442')

        assert out['category'] == 'zzDate night'
        assert out['description'] == 'Dinner'
        assert out['confidence'] == categorize.CONFIDENCE_RULE
        assert out['source'] == 'rule'


def test_learning_twice_updates_rather_than_duplicates(app, uid):
    with app.app_context():
        categorize.learn(uid, 'פז', 'Fuel', 'Petrol', 'expense')
        db.session.commit()
        categorize.learn(uid, 'פז', 'Car', 'Diesel', 'expense')
        db.session.commit()

        rules = MerchantRule.query.filter_by(user_id=uid).all()
        assert len(rules) == 1
        # The latest decision wins: a changed category is a recategorisation.
        assert rules[0].category == 'Car'
        assert rules[0].description == 'Diesel'
        assert rules[0].hits == 2


def test_learning_needs_both_a_merchant_and_a_category(app, uid):
    with app.app_context():
        assert categorize.learn(uid, '', 'Fuel', None, 'expense') is None
        assert categorize.learn(uid, 'פז', None, None, 'expense') is None
        db.session.rollback()


def test_a_broken_user_regex_does_not_break_guessing(app, uid):
    with app.app_context():
        add_category(uid, 'zzGroceries')
        db.session.add(MerchantRule(
            user_id=uid, pattern='[unclosed', is_regex=True,
            category='Nope', hits=99,
        ))
        db.session.commit()

        out = categorize.suggest(uid, 'שופרסל דיל')
        assert out['category'] == 'zzGroceries'


def test_the_most_used_rule_wins(app, uid):
    with app.app_context():
        db.session.add_all([
            MerchantRule(user_id=uid, pattern='שופרסל', category='Rare',
                         hits=1),
            MerchantRule(user_id=uid, pattern='שופרסל דיל', category='Common',
                         hits=50),
        ])
        db.session.commit()

        assert categorize.suggest(uid, 'שופרסל דיל')['category'] == 'Common'
