"""Category ownership and uniqueness.

Regression cover for a live bug: the deployed table had UNIQUE (name) with no
user_id in it, so the first account to create a name took it from everyone else
and the second got a 500.
"""
import pytest

from models import Category, db

BASE = 'https://localhost'
USER = '__pytest_cat_user__'
OTHER = '__pytest_cat_other__'

NAME = 'zzSharedCategoryName'


def test_two_users_can_hold_the_same_category_name(app, make_user, login):
    make_user(USER)
    make_user(OTHER)

    first = login(USER).post('/api/categories',
                             json={'name': NAME, 'type': 'expense'},
                             base_url=BASE)
    second = login(OTHER).post('/api/categories',
                               json={'name': NAME, 'type': 'expense'},
                               base_url=BASE)

    assert first.status_code == 200, first.data
    assert second.status_code == 200, second.data
    assert NAME in first.get_json()
    assert NAME in second.get_json()


def test_one_user_cannot_hold_the_same_name_twice(app, make_user, login):
    uid = make_user(USER)
    client = login(USER)

    client.post('/api/categories', json={'name': NAME, 'type': 'expense'},
                base_url=BASE)
    client.post('/api/categories', json={'name': NAME, 'type': 'expense'},
                base_url=BASE)

    with app.app_context():
        rows = Category.query.filter_by(user_id=uid, name=NAME).all()
        assert len(rows) == 1


def test_the_database_enforces_it_not_just_the_route(app, make_user):
    """The application already filtered by user; the schema did not."""
    from sqlalchemy.exc import IntegrityError

    uid = make_user(USER)
    with app.app_context():
        db.session.add(Category(user_id=uid, name=NAME, type='expense'))
        db.session.commit()

        db.session.add(Category(user_id=uid, name=NAME, type='expense'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_income_and_expense_may_share_a_name(app, make_user, login):
    uid = make_user(USER)
    client = login(USER)

    client.post('/api/categories', json={'name': NAME, 'type': 'expense'},
                base_url=BASE)
    client.post('/api/categories', json={'name': NAME, 'type': 'income'},
                base_url=BASE)

    with app.app_context():
        rows = Category.query.filter_by(user_id=uid, name=NAME).all()
        assert {r.type for r in rows} == {'expense', 'income'}


def test_delete_honours_the_type(app, make_user, login):
    """Without the type, the backend picked between them arbitrarily."""
    uid = make_user(USER)
    client = login(USER)
    client.post('/api/categories', json={'name': NAME, 'type': 'expense'},
                base_url=BASE)
    client.post('/api/categories', json={'name': NAME, 'type': 'income'},
                base_url=BASE)

    res = client.delete(f'/api/category/delete/{NAME}?type=income',
                        base_url=BASE)

    assert res.status_code == 200
    with app.app_context():
        rows = Category.query.filter_by(user_id=uid, name=NAME).all()
        assert [r.type for r in rows] == ['expense']


def test_delete_cannot_reach_another_users_category(app, make_user, login):
    make_user(USER)
    other = make_user(OTHER)
    login(OTHER).post('/api/categories',
                      json={'name': NAME, 'type': 'expense'}, base_url=BASE)

    res = login(USER).delete(f'/api/category/delete/{NAME}?type=expense',
                             base_url=BASE)

    assert res.status_code == 404
    with app.app_context():
        assert Category.query.filter_by(user_id=other, name=NAME).count() == 1
