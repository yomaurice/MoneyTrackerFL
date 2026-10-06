"""Ask questions about your own money in plain language.

Backed by the Gemini API. The model answers by calling the read-only tools in
services/chat_tools.py, which are pinned to the logged-in user, and this module
runs that loop. Conversation history lives in the browser and is sent with
each question; nothing about a chat is stored here.

GEMINI_API_KEY must be set. GEMINI_MODEL overrides the model;
GEMINI_FALLBACK_MODEL adds one to try when it is busy.
"""
import datetime
import logging
import os
import time

from flask import Blueprint, g, jsonify, request

from auth import login_required
from services import chat_tools

chat_bp = Blueprint('chat', __name__, url_prefix='/api/chat')

# Measured on the free tier (2026-10): gemini-flash-latest spends ~14s
# thinking per step and often answers 503/504 after 20-60s, so a three-step
# question cannot finish inside a web request. The lite model answers most
# steps in under a second and gets these questions right once the tools do
# the arithmetic. A fallback model is only used if GEMINI_FALLBACK_MODEL is set.
DEFAULT_MODEL = 'gemini-flash-lite-latest'
# The whole question, every model call included. Must stay under gunicorn's
# --timeout in the Procfile, or the worker is killed mid-answer and the
# browser gets a bare 502.
ANSWER_BUDGET_S = 50
# Time held back for the last call, which must answer instead of looking up
# more. Most lite steps take under a second, but one in a few takes 7-18s.
FINAL_STEP_RESERVE_S = 15
# Sent with the last call, which has no tools. Gemini ignores the
# function-calling mode NONE and keeps asking for lookups, so the tools are
# taken away instead and it is told why.
FINAL_NUDGE = ('No more lookups are possible. Answer now from the results '
               'above, and say plainly if something could not be found.')
# Each step is one model call; a question normally needs two to four. The cap
# stops a confused model from burning through the free tier's daily quota.
MAX_STEPS = 8
MAX_QUESTION_CHARS = 2000
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 4000

SYSTEM_PROMPT = """\
You answer questions about the user's personal finances in the MoneyTracker app.
Today is {today}.

The user's categories (name, type, number of transactions):
{categories}

Use the tools to look things up; never guess amounts. Each lookup is slow,
so plan the fewest calls that answer the question, and answer as soon as you
have the numbers. A search that finds nothing means there is nothing to
find; do not retry it with other words more than once. Never add, subtract
or average numbers yourself: every figure in your answer must come straight
from a tool result. Use summarize for totals and compare_periods for any
before/after comparison. Amounts are already converted to the user's main
currency (normally ILS, shown as ₪).

Topics like "my car" span several categories and merchants (fuel, insurance,
licence, garage, parking, leasing). Search broadly with keywords in both
Hebrew and English, then say which categories and merchants you counted so
the user can spot anything missed or wrongly included. Name only merchants
and categories that appear in the tool results; the keywords you searched
for are not results, so do not list them as merchants you found.

For "how much is X saving me" questions, call compare_periods on the related
spending (such as electricity bills) for the same calendar months before and
after the change, because bills are seasonal: months since the change this
year against the same months a year earlier. Use whole calendar months and
end both periods at the last complete month, because this month's bills may
not have arrived yet. Count any related credits or
income too. If the data shows what the change cost, state the cost alongside
the saving rather than netting them. If you need a fact the data cannot show,
like when something was installed, ask the user rather than assume.

Be concise. Lead with the answer and the number, then a short breakdown.
Reply in the language the user wrote in.
"""

_client = None


def _gemini():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        # The SDK's own retries back off for minutes on a busy model, far
        # longer than anyone waits for a chat reply. Timeouts are set per
        # call, from what is left of the answer's budget.
        _client = genai.Client(
            api_key=os.environ['GEMINI_API_KEY'],
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def _history(raw):
    from google.genai import types

    contents = []
    if not isinstance(raw, list):
        return contents
    for msg in raw[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(msg, dict):
            continue
        text = msg.get('text')
        role = {'user': 'user', 'assistant': 'model'}.get(msg.get('role'))
        if not role or not isinstance(text, str) or not text.strip():
            continue
        contents.append(types.Content(
            role=role, parts=[types.Part(text=text[:MAX_HISTORY_CHARS])]))
    return contents


def _category_lines(user_id):
    # Given up front, so the model does not spend a slow step looking them up.
    rows = chat_tools.list_categories(user_id)['categories']
    if not rows:
        return '(none yet)'
    return '\n'.join(
        f"- {r['category']} ({r['type']}, {r['transactions']})" for r in rows)


def answer(user_id, question, history=None):
    """Answer on the main model, or on the fallback if one is set and the
    main one is busy, all within ANSWER_BUDGET_S.

    The fallback starts the question over rather than resuming: the tools only
    read, so repeating them is harmless, and a half-finished turn from one
    model is not valid input to another.
    """
    import httpx
    from google.genai import errors

    deadline = time.monotonic() + ANSWER_BUDGET_S
    models = [os.environ.get('GEMINI_MODEL', DEFAULT_MODEL)]
    if os.environ.get('GEMINI_FALLBACK_MODEL'):
        models.append(os.environ['GEMINI_FALLBACK_MODEL'])
    for i, model in enumerate(models):
        try:
            return _answer_with(model, user_id, question, history, deadline)
        except (errors.ServerError, errors.ClientError,
                httpx.TimeoutException) as exc:
            busy = not isinstance(exc, errors.ClientError) or exc.code == 429
            if not busy or i == len(models) - 1:
                raise
            logging.warning('Gemini model %s unavailable (%r); falling back '
                            'to %s', model, exc, models[i + 1])


def _answer_with(model, user_id, question, history, deadline):
    """Run the tool loop to an answer on one model, before `deadline`."""
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT.format(
            today=datetime.date.today().isoformat(),
            categories=_category_lines(user_id)),
        tools=[types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=d['name'], description=d['description'],
                parameters_json_schema=d['parameters'])
            for d in chat_tools.DECLARATIONS
        ])],
        # This module runs the tools itself, so they always get the real
        # user id.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True),
    )
    contents = _history(history)
    contents.append(types.Content(role='user', parts=[types.Part(text=question)]))

    # The last call may not look anything else up, so it must answer from what
    # it has. Without this a model that keeps searching runs out of steps or
    # time and the user gets nothing.
    final_config = config.model_copy(update={'tools': None})

    for step in range(MAX_STEPS):
        remaining = deadline - time.monotonic()
        if remaining < 2:
            break
        final = (step == MAX_STEPS - 1
                 or remaining < FINAL_STEP_RESERVE_S + 2)
        if final:
            # Rides on the last user turn (the tool results), the shape the
            # API was checked to accept.
            contents[-1] = types.Content(
                role='user',
                parts=list(contents[-1].parts) + [types.Part(text=FINAL_NUDGE)])
        step_config = (final_config if final else config).model_copy(update={
            'http_options': types.HttpOptions(
                timeout=int(remaining * 1000),
                retry_options=types.HttpRetryOptions(attempts=1)),
        })
        response = _gemini().models.generate_content(
            model=model, contents=contents, config=step_config)
        calls = response.function_calls or []
        if not calls:
            return response.text or "Sorry, I couldn't come up with an answer."
        if final:
            break

        # The model turn goes back verbatim: newer Gemini models attach
        # signatures to it that the next call checks.
        contents.append(response.candidates[0].content)
        contents.append(types.Content(role='user', parts=[
            types.Part.from_function_response(
                name=call.name,
                response={'result': chat_tools.run(user_id, call.name, call.args)})
            for call in calls
        ]))

    return ('That question took too long to look up. Try asking something '
            'narrower, such as one year or one category.')


@chat_bp.route('', methods=['POST'])
@login_required
def chat():
    if not os.environ.get('GEMINI_API_KEY'):
        return jsonify({'message': 'The assistant is not configured.'}), 503

    data = request.get_json(silent=True) or {}
    question = data.get('question')
    if not isinstance(question, str) or not question.strip():
        return jsonify({'message': 'question is required'}), 400
    if len(question) > MAX_QUESTION_CHARS:
        return jsonify({
            'message': f'question exceeds {MAX_QUESTION_CHARS} characters'}), 400

    import httpx
    from google.genai import errors

    try:
        text = answer(g.user_id, question.strip(), data.get('history'))
    except errors.ClientError as exc:
        if exc.code == 429:
            return jsonify({
                'message': "The assistant's free quota is used up for now. "
                           'Try again in a minute, or tomorrow if the daily '
                           'limit was reached.'}), 429
        logging.error('Gemini rejected a chat request: %s', exc)
        return jsonify({'message': 'The assistant could not answer.'}), 502
    except (errors.ServerError, httpx.TimeoutException) as exc:
        logging.warning('Gemini unavailable: %r', exc)
        return jsonify({
            'message': 'The assistant is busy right now. Try again in a '
                       'minute.'}), 503
    except errors.APIError as exc:
        logging.error('Gemini error: %s', exc)
        return jsonify({'message': 'The assistant is unavailable right now.'}), 502

    return jsonify({'answer': text})
