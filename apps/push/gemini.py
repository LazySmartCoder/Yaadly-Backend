"""Gemini 2.5 flash-lite push-notification copy for the three daily nudges.

Every generated notification is strictly two lines: a short title plus a
two-line body (the push constraint). Generation is best-effort — any failure
(missing key, network error, malformed response) falls back to a warm generic
message so a notification is always worth sending.
"""

import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT = 20

# All daily notification copy uses the cheap flash-lite model.
CHAT_MODEL = "gemini-2.5-flash-lite"

# Line-length caps so a two-line body renders nicely on a phone lock screen.
LINE_1_MAX = 75
LINE_2_MAX = 95
TITLE_MAX = 45

MORNING_PROMPT = """You are Yaadly, a warm companion who has read the user's past journal entries. It is 9 AM. Write ONE push notification that tells the user "everyone believes in you", grounded in a real moment from their own journal.

- Line 1: the core idea, warm and direct — that everyone believes in them.
- Line 2: one small, true echo from their past: a time when someone they know (friend, family, partner, colleague, mentor) believed in them, supported them, or cheered them on. Draw it ONLY from the entries below; never invent names, people, or events.

Rules:
- Exactly two lines. No bullet points, no surrounding quotes, no emojis, no em dashes, no "line 1"/"line 2" labels.
- Line 1 under 75 characters; line 2 under 95 characters.
- Warm, everyday, human words. Never sound like a bot.
- If no entry shows someone believing in them, keep line 2 a gentle, generic line about people rooting for them.

Return ONLY a JSON object with keys "title" (a short upbeat title under 45 characters) and "body" (the two lines joined by a newline).

User's journal entries:
{content}"""

AFTERNOON_PROMPT = """You are Yaadly. Summarize the user's journal entry below into a simple, warm memory in exactly two lines.

- Line 1: what happened, very briefly.
- Line 2: how it felt or one warm, true detail.

Stay strictly true to the entry; never invent facts. No surrounding quotes, no emojis, no em dashes, no labels. Line 1 under 75 characters; line 2 under 95 characters.

Return ONLY a JSON object with keys "title" (under 45 characters, e.g. "A memory from {date}") and "body" (the two lines joined by a newline).

Journal entry (date: {date}, mood: {mood}):
{content}"""

EVENING_PROMPT = """You are Yaadly. It is 10 PM and the user has not journaled today. Write ONE push notification in exactly two lines:

- Line 1: a warm invitation to jot down how their day went before they sleep.
- Line 2: a gentle reminder of a good thing or two from their past journal entries — a strength, a win, or something they were proud of. Draw it ONLY from the entries below; never invent facts.

Rules:
- Exactly two lines. No surrounding quotes, no emojis, no em dashes, no labels.
- Line 1 under 75 characters; line 2 under 95 characters.
- Warm, encouraging, everyday words. Never sound like a bot.
- If there are no entries, make line 2 a gentle generic push to end the day on a kind note.

Return ONLY a JSON object with keys "title" (under 45 characters, warm and short) and "body" (the two lines joined by a newline).

User's past journal entries:
{content}"""


def _api_key():
    from django.conf import settings

    return getattr(settings, "GEMINI_API_KEY", None)


def _call_gemini(prompt, max_output_tokens=256, temperature=0.8):
    """Best-effort single-turn call to gemini-2.5-flash-lite.

    Returns the raw model text, or None on any failure.
    """
    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; using fallback push copy.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{CHAT_MODEL}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": prompt}]}
                ],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_output_tokens,
                    "responseMimeType": "application/json",
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini push copy request failed: %s", exc)
        return None

    try:
        payload = response.json()
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini push copy response: %s", exc)
        return None


def _parse_json_object(text):
    """Best-effort parse of a JSON object from the model's output."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _fit_line(text, limit):
    """Collapse whitespace and hard-truncate a single line at ``limit``."""
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _clean_body(raw_body):
    """Normalize an AI body into exactly two lines, each under its cap."""
    lines = [
        _fit_line(line, LINE_1_MAX if i == 0 else LINE_2_MAX)
        for i, line in enumerate((raw_body or "").splitlines())
    ]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return ["", ""]
    if len(lines) == 1:
        return [lines[0], ""]
    return [lines[0], lines[1]]


def _result_from_ai(raw_text, fallback_title, fallback_body):
    """Turn the model's JSON text into a safe {title, body} pair."""
    data = _parse_json_object(raw_text)
    if not isinstance(data, dict):
        return {"title": fallback_title, "body": fallback_body}
    title = (data.get("title") or "").strip()
    body = _clean_body(data.get("body") or "")
    if not title or not body[0]:
        return {"title": fallback_title, "body": fallback_body}
    return {"title": _fit_line(title, TITLE_MAX), "body": "\n".join(body)}


# --- Slot-specific generators -------------------------------------------------

MORNING_FALLBACK_TITLE = "Everyone believes in you"
MORNING_FALLBACK_BODY = "Everyone believes in you.\nYou are stronger and braver than you know."


def morning_message(raw_contents):
    """9 AM nudge: "everyone believes in you", echoed from the user's past."""
    content = (raw_contents or "").strip()
    text = None
    if content:
        text = _call_gemini(
            MORNING_PROMPT.format(content=content),
            max_output_tokens=220,
            temperature=0.8,
        )
    if not text:
        return {
            "title": MORNING_FALLBACK_TITLE,
            "body": MORNING_FALLBACK_BODY,
        }
    return _result_from_ai(text, MORNING_FALLBACK_TITLE, MORNING_FALLBACK_BODY)


AFTERNOON_FALLBACK_TITLE = "A little memory break"
AFTERNOON_FALLBACK_BODY = "A small memory from not long ago.\nTake a moment to smile at it."


def afternoon_memory(entry):
    """1 PM nudge: a simple, two-line memory drawn from one recent entry."""
    if entry is None:
        return {
            "title": AFTERNOON_FALLBACK_TITLE,
            "body": AFTERNOON_FALLBACK_BODY,
        }
    content = (entry.content or "").strip()
    date = entry.date.isoformat() if entry.date else ""
    mood = (entry.mood or "").strip() or "(none)"
    text = None
    if content:
        text = _call_gemini(
            AFTERNOON_PROMPT.format(date=date, mood=mood, content=content),
            max_output_tokens=220,
            temperature=0.8,
        )
    if not text:
        return {
            "title": _fit_line(
                f"A memory from {date}" if date else AFTERNOON_FALLBACK_TITLE,
                TITLE_MAX,
            ),
            "body": AFTERNOON_FALLBACK_BODY,
        }
    return _result_from_ai(text, AFTERNOON_FALLBACK_TITLE, AFTERNOON_FALLBACK_BODY)


EVENING_FALLBACK_TITLE = "How was your day?"
EVENING_FALLBACK_BODY = "Jot down how your day went.\nA quiet moment for you tonight."


def evening_message(raw_contents):
    """10 PM nudge: journal today + a good thing from the user's past."""
    content = (raw_contents or "").strip()
    text = None
    if content:
        text = _call_gemini(
            EVENING_PROMPT.format(content=content),
            max_output_tokens=220,
            temperature=0.8,
        )
    if not text:
        return {
            "title": EVENING_FALLBACK_TITLE,
            "body": EVENING_FALLBACK_BODY,
        }
    return _result_from_ai(text, EVENING_FALLBACK_TITLE, EVENING_FALLBACK_BODY)
