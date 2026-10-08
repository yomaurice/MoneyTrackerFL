"""The chat assistant: its data tools, and the tool loop with Gemini faked."""
import datetime
from types import SimpleNamespace

import pytest

from models import Transaction, db
from routes import chat as chat_route
from services import chat_tools

BASE = 'https://localhost'
USER = '__pytest_chat_user__'
OTHER = '__pytest_chat_other__'


@pytest.fixture
def uid(make_user):
    return make_user(USER)


def add(uid, amount, category, description='', day=datetime.date(2026, 3, 1),
        type='expense', rate=1.0, currency='ILS'):
    db.session.add(Transaction(
        user_id=uid, amount=amount, category=category, description=description,
        date=day, type=type, exchange_rate=rate, currency=currency,
    ))
    db.session.commit()


# --- tools ---------------------------------------------------------------

def test_summarize_totals_by_type_and_converts_currency(app, uid):
    with app.app_context():
        add(uid, 100, 'zzFuel')
        add(uid, 10, 'zzFuel', rate=3.5, currency='USD')
        add(uid, 500, 'zzSalary', type='income')

        body = chat_tools.summarize(uid)

    totals = {t['type']: t['total'] for t in body['totals']}
    assert totals == {'expense': 135.0, 'income': 500.0}


def test_keywords_match_description_or_category(app, uid):
    with app.app_context():
        add(uid, 100, 'zzCar insurance')
        add(uid, 50, 'zzMisc', description='Paz fuel')
        add(uid, 70, 'zzGroceries', description='Shufersal')

        body = chat_tools.summarize(uid, keywords=['car', 'fuel'])

    assert body['totals'][0]['total'] == 150.0


def test_date_range_is_inclusive_and_groups_by_month(app, uid):
    with app.app_context():
        add(uid, 10, 'zzPower', day=datetime.date(2025, 12, 31))
        add(uid, 20, 'zzPower', day=datetime.date(2026, 1, 1))
        add(uid, 30, 'zzPower', day=datetime.date(2026, 2, 28))
        add(uid, 40, 'zzPower', day=datetime.date(2026, 3, 1))

        body = chat_tools.summarize(
            uid, start_date='2026-01-01', end_date='2026-02-28',
            group_by='month')

    assert [(g['group'], g['total']) for g in body['groups']] == [
        ('2026-01', 20.0), ('2026-02', 30.0)]


def test_compare_periods_computes_the_change(app, uid):
    with app.app_context():
        add(uid, 600, 'zzPower', day=datetime.date(2025, 1, 10))
        add(uid, 400, 'zzPower', day=datetime.date(2025, 2, 10))
        add(uid, 200, 'zzPower', day=datetime.date(2026, 1, 10))
        add(uid, 100, 'zzPower', day=datetime.date(2026, 2, 10))
        add(uid, 999, 'zzFuel', day=datetime.date(2026, 2, 10))

        body = chat_tools.compare_periods(
            uid, '2025-01-01', '2025-02-28', '2026-01-01', '2026-02-28',
            categories=['zzpower'])

    assert body['before']['by_type']['expense']['total'] == 1000.0
    assert body['before']['by_type']['expense']['monthly_average'] == 500.0
    assert body['after']['by_type']['expense']['total'] == 300.0
    assert body['change']['expense'] == {
        'after_minus_before': -700.0, 'percent': -70.0}


def test_compare_periods_needs_both_ranges(app, uid):
    with app.app_context():
        reply = chat_tools.run(uid, 'compare_periods', {
            'before_start': '2025-01-01', 'before_end': '2025-02-28',
            'after_start': '2026-01-01'})
    assert 'error' in reply


def test_search_reports_total_of_all_matches_beyond_the_limit(app, uid):
    with app.app_context():
        for i in range(5):
            add(uid, 10, 'zzFuel', day=datetime.date(2026, 3, i + 1))

        body = chat_tools.search_transactions(uid, limit=2)

    assert body['returned'] == 2
    assert body['matching_count'] == 5
    assert body['matching_total'] == 50.0
    assert body['transactions'][0]['date'] == '2026-03-05'


def test_tools_never_see_another_users_rows(app, uid, make_user):
    other = make_user(OTHER)
    with app.app_context():
        add(other, 999, 'zzFuel')

        assert chat_tools.summarize(uid)['totals'] == []
        assert chat_tools.search_transactions(uid)['matching_count'] == 0
        assert chat_tools.list_categories(uid)['categories'] == []


def test_bad_arguments_come_back_as_errors_not_exceptions(app, uid):
    with app.app_context():
        assert 'error' in chat_tools.run(uid, 'summarize', {'start_date': 'May'})
        assert 'error' in chat_tools.run(uid, 'summarize', {'group_by': 'week'})
        assert 'error' in chat_tools.run(uid, 'summarize', {'bogus': 1})
        assert 'error' in chat_tools.run(uid, 'drop_table', {})


# --- the loop, with Gemini faked -------------------------------------------

def _call(name, **args):
    return SimpleNamespace(name=name, args=args)


def _turn(calls=None, text=None):
    from google.genai import types
    return SimpleNamespace(
        function_calls=calls, text=text,
        candidates=[SimpleNamespace(content=types.Content(
            role='model', parts=[types.Part(text='...')]))],
    )


class FakeGemini:
    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.requests.append(list(contents))
        return self.turns.pop(0)


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')

    def install(*turns):
        fake = FakeGemini(turns)
        monkeypatch.setattr(chat_route, '_client', fake)
        return fake
    return install


def test_chat_runs_tools_as_the_logged_in_user(app, uid, login, gemini,
                                               make_user):
    other = make_user(OTHER)
    with app.app_context():
        add(uid, 120, 'zzFuel')
        add(other, 999, 'zzFuel')
    fake = gemini(
        _turn([_call('summarize', keywords=['fuel'])]),
        _turn(text='You spent ₪120 on fuel.'),
    )

    res = login(USER).post('/api/chat', json={'question': 'Fuel?'},
                           base_url=BASE)

    assert res.status_code == 200, res.data
    assert res.get_json()['answer'] == 'You spent ₪120 on fuel.'
    tool_reply = fake.requests[1][-1].parts[0].function_response.response
    assert tool_reply['result']['totals'][0]['total'] == 120.0


def test_chat_passes_history_through(app, uid, login, gemini):
    fake = gemini(_turn(text='ok'))

    login(USER).post('/api/chat', json={
        'question': 'And last year?',
        'history': [
            {'role': 'user', 'text': 'Fuel this year?'},
            {'role': 'assistant', 'text': '₪120'},
            {'role': 'system', 'text': 'ignored'},
        ],
    }, base_url=BASE)

    roles = [c.role for c in fake.requests[0]]
    assert roles == ['user', 'model', 'user']


def test_chat_stops_after_max_steps(app, uid, login, gemini):
    gemini(*[_turn([_call('list_categories')])] * chat_route.MAX_STEPS)

    res = login(USER).post('/api/chat', json={'question': 'loop'},
                           base_url=BASE)

    assert res.status_code == 200
    assert 'too many lookups' in res.get_json()['answer']


def _busy():
    from google.genai import errors
    return errors.ServerError(503, {'error': {
        'code': 503, 'message': 'high demand', 'status': 'UNAVAILABLE'}})


class BusyThenFake(FakeGemini):
    """Fails every call on the main model, answers on any other."""

    def generate_content(self, model, contents, config):
        self.requests.append((model, list(contents)))
        if model == chat_route.DEFAULT_MODEL:
            raise _busy()
        return self.turns.pop(0)


def test_chat_falls_back_when_the_main_model_is_busy(app, uid, login,
                                                     monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.delenv('GEMINI_FALLBACK_MODEL', raising=False)
    fake = BusyThenFake([_turn(text='from the fallback')])
    monkeypatch.setattr(chat_route, '_client', fake)

    res = login(USER).post('/api/chat', json={'question': 'hi'},
                           base_url=BASE)

    assert res.get_json()['answer'] == 'from the fallback'
    assert [m for m, _ in fake.requests] == [
        chat_route.DEFAULT_MODEL, chat_route.DEFAULT_FALLBACK_MODEL]


def test_chat_reports_busy_when_every_model_is(app, uid, login, monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')

    class AlwaysBusy(FakeGemini):
        def generate_content(self, model, contents, config):
            raise _busy()

    monkeypatch.setattr(chat_route, '_client', AlwaysBusy([]))

    res = login(USER).post('/api/chat', json={'question': 'hi'},
                           base_url=BASE)
    assert res.status_code == 503


def test_chat_validates_input(app, uid, login, gemini):
    client = login(USER)
    assert client.post('/api/chat', json={}, base_url=BASE).status_code == 400
    assert client.post('/api/chat', json={'question': 'x' * 5000},
                       base_url=BASE).status_code == 400


def test_chat_needs_a_key(app, uid, login, monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    res = login(USER).post('/api/chat', json={'question': 'hi'}, base_url=BASE)
    assert res.status_code == 503


def test_chat_needs_a_session(app):
    assert app.test_client().post(
        '/api/chat', json={'question': 'hi'}, base_url=BASE
    ).status_code == 401


def test_chat_gives_up_cleanly_when_out_of_time(app, uid, login, gemini,
                                                monkeypatch):
    # Out of budget before the first call: a 503 the page can show, not a
    # worker killed by gunicorn's timeout.
    monkeypatch.setattr(chat_route, 'TOTAL_TIMEOUT_S', 0)
    fake = gemini(_turn(text='never sent'))

    res = login(USER).post('/api/chat', json={'question': 'hi'},
                           base_url=BASE)

    assert res.status_code == 503
    assert 'busy' in res.get_json()['message']
    assert fake.requests == []


def test_each_call_waits_only_for_the_time_left(app, uid, login, gemini,
                                                monkeypatch):
    monkeypatch.setattr(chat_route, 'TOTAL_TIMEOUT_S', 12)
    timeouts = []

    class Recording(FakeGemini):
        def generate_content(self, model, contents, config):
            timeouts.append(config.http_options.timeout)
            return super().generate_content(model, contents, config)

    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setattr(chat_route, '_client', Recording([_turn(text='ok')]))

    login(USER).post('/api/chat', json={'question': 'hi'}, base_url=BASE)

    assert len(timeouts) == 1 and 0 < timeouts[0] <= 12_000
