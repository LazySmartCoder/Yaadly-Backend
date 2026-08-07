"""Gemini-powered journalizing for new journal entries.

Turns a user's raw thoughts into a polished journal entry, judges the mood of
the day, and writes a crisp title. Strictly best-effort: any failure (missing
API key, network error, malformed response) is logged and swallowed so a
journal entry can always be saved.
"""

import json
import logging
import re

import requests

logger = logging.getLogger(__name__)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT = 20

# Chat and routing always use the same cheap model, regardless of GEMINI_MODEL.
CHAT_MODEL = "gemini-2.5-flash-lite"

JOURNALIZE_PROMPT = """You are a thoughtful journaling assistant. Given a user's raw journal thoughts, produce exactly three things:

1. "content": the user's thoughts rewritten as a polished, warm, well-organized journal entry in the first person. Stay strictly true to what the user shared — keep the same facts and tone, never invent events or details — but gently improve the flow and wording. Preserve every word the user wrote exactly as-is, including slang and profanity such as "fuck" or "bastard". Never censor, replace, soften, paraphrase away, or remove any of the user's words — the entry must read as if the user wrote it themselves.
2. "mood": a single short label (for example "Joyful", "Anxious", "Grateful", "Reflective", "Tired") that best captures how the user felt that day.
3. "title": a crisp, short title (under 60 characters, no trailing period) that captures the theme of the day.

Return ONLY a JSON object with the keys "content", "mood", and "title".

User's raw thoughts:
{content}"""


def _api_key():
    from django.conf import settings

    return getattr(settings, "GEMINI_API_KEY", None)


def _model():
    from django.conf import settings

    return getattr(settings, "GEMINI_MODEL", "gemini-2.5-flash-lite")


def journalize(raw_content):
    """Journalize the user's raw thoughts via Gemini.

    Returns a dict with ``title``, ``content``, and ``mood`` keys, or None when
    journalizing is unavailable (no key, network error, or bad response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; saving the raw entry.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{_model()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": JOURNALIZE_PROMPT.format(content=content)}]}
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 1024,
                    "responseMimeType": "application/json",
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini journalize request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini journalize response: %s", exc)
        return None

    return _parse_result(text)


def _parse_result(text):
    """Extract ``{title, content, mood}`` from the model's JSON output."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("Gemini returned non-JSON journalize output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Gemini returned non-JSON journalize output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Gemini journalize output was not a JSON object.")
        return None

    content = (data.get("content") or "").strip()
    if not content:
        logger.warning("Gemini journalize output had no content.")
        return None

    title = (data.get("title") or "").strip()
    mood = (data.get("mood") or "").strip()
    return {
        "title": title[:255],
        "content": content,
        "mood": mood[:50],
    }


AID_PROMPT = """You are a journaling assistant. Write ONE long, detailed single sentence — a big one-liner — that vividly summarizes the user's journal entry below. The sentence must:

- Flow as a single line with no line breaks.
- Capture what happened, the key details, and how the user felt.
- Be detailed and meaty, not a dry list.
- Stay strictly true to the user's words; never invent events, people, or facts.

Return ONLY the sentence as plain text, with no quotes, labels, or bullets.

User's journal entry (mood: {mood}):
{content}"""


def describe_entry(raw_content, mood=""):
    """Write a long, detailed one-line description ("AID") of a journal entry.

    Returns the single-line string, or None when prompting is unavailable (no
    key, network error, or malformed response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping entry description.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{_model()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": AID_PROMPT.format(mood=mood or "(none)", content=content)}]}
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 256,
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini describe_entry request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini describe_entry response: %s", exc)
        return None

    text = (text or "").strip().strip('"')
    if not text:
        return None
    return " ".join(text.split())


FOLLOW_UP_PROMPT = """You are a supportive journaling coach. Based on what the user has written so far, ask ONE short, specific, open-ended question that gently encourages them to go deeper into their day.

Rules:
- Reply with only the question text, no quotes, labels, or explanations.
- Under 12 words.
- Do not repeat questions already asked.

So far, the user has written:
{content}"""


def ask_followup(raw_content):
    """Ask a single low-cost follow-up question about the user's journal so far.

    Returns the question text, or None when prompting is unavailable (no key,
    network error, or malformed response). Never returns empty strings that
    look like a successful prompt.
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping follow-up question.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{_model()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": FOLLOW_UP_PROMPT.format(content=content)}]}
                ],
                "generationConfig": {
                    "temperature": 0.8,
                    "maxOutputTokens": 64,
                    "topP": 0.95,
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini follow-up request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini follow-up response: %s", exc)
        return None

    question = (text or "").strip().strip('"')
    return question or None


SUMMARY_PROMPT = """You are a close friend who just read the user's latest journal entry. Write exactly two short lines:

1. "summary": ONE brief, warm sentence in the past tense that reflects back what they shared. Stay strictly true to their words; never invent details.
2. "question": ONE crisp, upbeat, energetic question that puts TODAY front and center — the fresh, brand-new day ahead — while weaving in yesterday only as a quiet, subtle echo. The question should make the user feel happy and fired up about today; yesterday is a light footnote, never the main subject. Tune it to the mood and content of the entry:
   - If yesterday was hard or heavy, cheer for today as a fresh start, with a soft, caring nod back ("…fresh off yesterday's tough one?") that feels warm, not heavy.
   - If yesterday was good or joyful, invite them to carry that spark into today, punchy and celebratory ("…especially after yesterday's win?").
   - Otherwise, keep it bright, curious, and natural.

Structure of the question:
- LEAD with today: an energetic opener like "What's new today?" or "What's today bringing you?" or "What's the best part of today so far?" — happy, curious, alive.
- Then tag yesterday on as a short bridge (under 8 words), e.g. "…especially after yesterday's win?" or "…fresh off yesterday's long day?"
- Whole thing must be one breath long — crisp, punchy, everyday words.

Examples of the feel:
- "What's the best part of today so far? Especially after yesterday's hike win?"
- "What's new today? Even if it's lighter after yesterday's long one."
- "What's today bringing you? Something new after yesterday's interview nerves?"

Rules for the question:
- TODAY is the star; yesterday is only a quiet echo, never the focus.
- One breath long, crisp, energetic, everyday words, no labels, no quotes, no em dashes.
- Never sound like a bot: no "reflect on", "explore your feelings", "how did that make you feel", "take a moment to", or similar robotic phrasing.

Return ONLY a JSON object with the keys "summary" and "question".

User's most recent journal entry (mood: {mood}):
{content}"""


def summarize(raw_content, mood=""):
    """Summarize the user's latest journal entry into a two-line nudge
    (one reflective sentence plus one follow-up question, tone-matched to the
    entry's mood).

    Returns a dict with ``summary`` and ``question`` keys, or None when
    prompting is unavailable (no key, network error, or malformed response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping home summary.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{_model()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": SUMMARY_PROMPT.format(mood=mood, content=content)}]}
                ],
                "generationConfig": {
                    "temperature": 0.8,
                    "maxOutputTokens": 256,
                    "responseMimeType": "application/json",
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini summary request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini summary response: %s", exc)
        return None

    return _parse_summary(text)


def _parse_summary(text):
    """Extract ``{summary, question}`` from the model's JSON output."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("Gemini returned non-JSON summary output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Gemini returned non-JSON summary output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Gemini summary output was not a JSON object.")
        return None

    summary = (data.get("summary") or "").strip()
    if not summary:
        logger.warning("Gemini summary output had no summary line.")
        return None

    question = (data.get("question") or "").strip()
    return {
        "summary": summary,
        "question": question,
    }


CHAT_PROMPT = """You are Yaadly, a warm, thoughtful companion who helps the user look back on their past and cherish their memories. You know the user through their journal entries:

{memories}

(If nothing appears above, it simply means you have not seen their journal yet.)

Talk to the user kindly. Reply conversationally, warmly, and concisely (usually 2-4 sentences). Draw on their journal memories when relevant; otherwise invite them to share a memory with a gentle, open-ended question. Never invent events, facts, or details about their life. Never judge, diagnose, or lecture."""


ROUTE_PROMPT = """You are Yaadly, a warm companion who helps the user look back on their journal. Here is a compact index of the user's journal days, each with its date and a one-line description:

{index}

Here is the conversation so far (each line is "role: text"):
{messages}

Decide how to handle the user's LATEST message. Whenever the user refers to a specific past day or memory, your FIRST move must be to confirm WHICH date they mean before any details are given:

- If the user asks about a past day/memory WITHOUT an explicit date → return {{"intent": "ask_date", "date": "<most likely date from the index>", "question": "A short, natural confirming question mentioning that date, e.g. 'Are you talking about May 12, 2026?'"}}.
- If the user's latest message explicitly states a date (or clearly confirms "yes"/"that's it" after you asked about a specific date) → return {{"intent": "detail", "date": "<that date>"}}.
- If the latest message answers "no" to a confirmation → return {{"intent": "general", "date": null}} unless another memory clearly fits.
- For anything else (greetings, small talk, asking about journaling in general, sharing a new memory) → return {{"intent": "general", "date": null}}.

Rules:
- "date" must exactly match one of the dates in the index, or be null.
- Never invent dates, events, or details that are not in the index.

Return ONLY a JSON object with the keys "intent" ("general" | "ask_date" | "detail"), "date" (string or null), and "question" (string or null)."""


def route_chat(messages, index):
    """Decides what the chat should do this turn: answer generally, ask which
    date the user means, or fetch a specific entry's details.

    ``messages`` is the conversation so far; ``index`` is a compact
    "date: one-line description" listing of the user's journal days. Returns
    the parsed intent dict, or None when routing is unavailable (no key,
    network error, or malformed response)."""
    if not messages or not index:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping chat routing.")
        return None

    transcript = "\n".join(
        f"{msg.get('role', 'user')}: {(msg.get('content') or '').strip()}"
        for msg in messages
        if (msg.get('content') or '').strip()
    )

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{CHAT_MODEL}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": ROUTE_PROMPT.format(index=index, messages=transcript)}]}
                ],
                "generationConfig": {
                    "temperature": 0.2,
                    "maxOutputTokens": 160,
                    "responseMimeType": "application/json",
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini route request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini route response: %s", exc)
        return None

    data = _parse_json_object(text)
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in {"general", "ask_date", "detail"}:
        return None
    return {
        "intent": intent,
        "date": data.get("date"),
        "question": (data.get("question") or "").strip() or None,
    }


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


def chat_reply(messages, memories):
    """Carry on a conversation with the user about their past and memories.

    ``messages`` is the conversation so far, a list of
    ``{"role": "user" | "assistant", "content": str}``. Returns the assistant's
    reply text, or None when prompting is unavailable (no key, network error,
    or malformed response).
    """
    if not messages:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping chat reply.")
        return None

    contents = []
    for msg in messages:
        role = "model" if (msg.get("role") or "").lower() == "assistant" else "user"
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        contents.append({"role": role, "parts": [{"text": text}]})
    if not contents:
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{CHAT_MODEL}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "systemInstruction": {
                    "parts": [
                        {"text": CHAT_PROMPT.format(memories=memories or "(none yet)")}
                    ]
                },
                "contents": contents,
                "generationConfig": {
                    "temperature": 0.9,
                    "maxOutputTokens": 512,
                    "topP": 0.95,
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini chat request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini chat response: %s", exc)
        return None

    return (text or "").strip() or None


BIO_PROMPT = """You are a thoughtful journaling assistant. Based on the user's journal entries, write a short, crisp two-line bio about them as a person.

Rules:
- Line 1: who they are, drawn only from what they have written.
- Line 2: what matters to them or the tone of their reflections.
- Stay strictly true to their writing; never invent names, facts, or events.
- Keep each line short (under 15 words).
- Write in the third person.

Return ONLY a JSON object with the key "bio" whose value is the two lines joined by a newline.

User's journal entries:
{content}"""


def build_bio(raw_contents):
    """Build a short two-line bio of the user from their journal entries.

    Returns the two-line bio string, or None when prompting is unavailable (no
    key, network error, or malformed response).
    """
    content = (raw_contents or "").strip()
    if not content:
        return None

    api_key = _api_key()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set; skipping profile bio.")
        return None

    try:
        response = requests.post(
            f"{GEMINI_API_URL}/models/{_model()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={
                "contents": [
                    {"role": "user", "parts": [{"text": BIO_PROMPT.format(content=content)}]}
                ],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 256,
                    "responseMimeType": "application/json",
                },
            },
            timeout=GEMINI_TIMEOUT,
        )
        response.raise_for_status()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Gemini bio request failed: %s", exc)
        return None

    try:
        payload = response.json()
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("Unexpected Gemini bio response: %s", exc)
        return None

    return _parse_bio(text)


def _parse_bio(text):
    """Extract the two-line bio string from the model's JSON output."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            logger.warning("Gemini returned non-JSON bio output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Gemini returned non-JSON bio output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Gemini bio output was not a JSON object.")
        return None

    bio = (data.get("bio") or "").strip()
    return bio or None
