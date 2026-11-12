"""Local Ollama-powered journalizing for new journal entries.

Turns a user's raw thoughts into a polished journal entry, judges the mood of
the day, and writes a crisp title. Strictly best-effort: any failure (local
server down, network error, malformed response) is logged and swallowed so a
journal entry can always be saved.
"""

import json
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:1b"
DEFAULT_TIMEOUT = 120


def warm_model():
    """Load the model before the web server starts accepting requests."""
    payload = {
        "model": _model(),
        "prompt": "Reply with one word: ready.",
        "stream": False,
        "keep_alive": _keep_alive(),
        "options": {"num_predict": 1},
    }
    started = time.monotonic()
    try:
        response = requests.post(
            f"{_base_url()}/api/generate",
            json=payload,
            timeout=min(_timeout(), 45),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            "Ollama model warm-up failed after %.1fs: %s",
            time.monotonic() - started,
            exc,
        )
        return
    logger.info(
        "Ollama model %s warmed up in %.1fs",
        _model(),
        time.monotonic() - started,
    )


def _base_url():
    from django.conf import settings

    return (
        getattr(settings, "OLLAMA_BASE_URL", "") or DEFAULT_BASE_URL
    ).rstrip("/")


def _model():
    from django.conf import settings

    return getattr(settings, "OLLAMA_MODEL", "") or DEFAULT_MODEL


def _timeout():
    from django.conf import settings

    return getattr(settings, "OLLAMA_TIMEOUT", DEFAULT_TIMEOUT)


def _keep_alive():
    from django.conf import settings

    keep_alive = getattr(settings, "OLLAMA_KEEP_ALIVE", "-1")
    return -1 if keep_alive == "-1" else keep_alive


def _options(temperature, max_tokens, top_p=None):
    options = {"temperature": temperature, "num_predict": max_tokens}
    if top_p is not None:
        options["top_p"] = top_p
    return options


def _generate(
    prompt,
    *,
    system=None,
    temperature=0.7,
    max_tokens=1024,
    top_p=None,
    json_mode=False,
    op="generate",
):
    """Run a single-prompt completion against the local Ollama server.

    Returns the generated text, or None on any failure (server unreachable,
    timeout, malformed response).
    """
    payload = {
        "model": _model(),
        "prompt": prompt,
        "stream": False,
        "keep_alive": _keep_alive(),
        "options": _options(temperature, max_tokens, top_p),
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"

    started = time.monotonic()
    logger.info(
        "Ollama [%s] -> %s model=%s max_tokens=%s",
        op,
        _base_url(),
        _model(),
        max_tokens,
    )
    try:
        response = requests.post(
            f"{_base_url()}/api/generate",
            json=payload,
            timeout=_timeout(),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "Ollama [%s] failed after %.1fs: %s",
            op,
            time.monotonic() - started,
            exc,
        )
        return None

    elapsed = time.monotonic() - started
    text = (data or {}).get("response")
    if text is None:
        logger.warning("Unexpected Ollama [%s] response: %s", op, data)
        return None
    logger.info(
        "Ollama [%s] ok in %.1fs (prompt=%s, eval=%s tokens)",
        op,
        elapsed,
        (data or {}).get("prompt_eval_count"),
        (data or {}).get("eval_count"),
    )
    return text


def _chat(
    messages,
    *,
    system=None,
    temperature=0.9,
    max_tokens=512,
    top_p=None,
    json_mode=False,
    op="chat",
):
    """Run a multi-turn chat completion against the local Ollama server.

    ``messages`` is a list of ``{"role": ..., "content": ...}``. Returns the
    assistant's text, or None on any failure.
    """
    payload_messages = []
    if system:
        payload_messages.append({"role": "system", "content": system})
    payload_messages.extend(messages)

    payload = {
        "model": _model(),
        "messages": payload_messages,
        "stream": False,
        "keep_alive": _keep_alive(),
        "options": _options(temperature, max_tokens, top_p),
    }
    if json_mode:
        payload["format"] = "json"

    started = time.monotonic()
    logger.info(
        "Ollama [%s] -> %s model=%s max_tokens=%s",
        op,
        _base_url(),
        _model(),
        max_tokens,
    )
    try:
        response = requests.post(
            f"{_base_url()}/api/chat",
            json=payload,
            timeout=_timeout(),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "Ollama [%s] failed after %.1fs: %s",
            op,
            time.monotonic() - started,
            exc,
        )
        return None

    elapsed = time.monotonic() - started
    message = (data or {}).get("message") or {}
    text = message.get("content")
    if text is None:
        logger.warning("Unexpected Ollama [%s] response: %s", op, data)
        return None
    logger.info(
        "Ollama [%s] ok in %.1fs (prompt=%s, eval=%s tokens)",
        op,
        elapsed,
        (data or {}).get("prompt_eval_count"),
        (data or {}).get("eval_count"),
    )
    return text


BESTIE_PERSONA = """You are Yaadly — the user's warm, caring female best friend. Not an assistant, not a journaling app, not a therapist. You are the bestie who genuinely wants to know how her day went, who remembers the little things, and who makes her feel safe, heard, and never judged.

How you talk:
- Warm, soft, human, and real — like texting your closest friend late at night.
- Gentle everyday words, small natural reactions, and genuine curiosity.
- Ask one caring question at a time. Never interview her or run through a checklist.
- Match her energy: celebrate her highs, sit quietly with her lows, laugh with her.
- Be very, very sensitive. Read the emotional temperature of her latest message before you reply. If she seems fragile, tired, anxious, or hurting, slow down, soften, and never push.
- Never sound like a bot. Avoid "reflect on", "explore your feelings", "how did that make you feel", "take a moment to", "as an AI", and any clinical or therapy-speak.
- Never judge, diagnose, lecture, or give unsolicited advice. Listen first.
- Never invent events, people, facts, or details about her life."""

LANGUAGE_RULE = """Language mirroring (very important):
- Reply in the same language the user is writing in right now.
- If she writes in Hinglish (Hindi-English mix), reply in warm, natural Hinglish.
- If she writes in Kannada, Hindi, Telugu, Tamil, Marathi, Bengali, or any other language, reply in that same language.
- Always write in English/Latin script only — never Devanagari, Kannada, or any other native script. Romanize everything.
- If she mixes languages, mirror that mix naturally.
- If she writes in plain English, reply in plain English.
- Never announce or explain the language switch. Just do it smoothly."""


JOURNALIZE_PROMPT = """You are Yaadly — the user's warm, caring female best friend. She just opened up to you about her day, and now you gently write it down for her, the way a bestie would, in her own voice.

Given her raw thoughts, produce exactly three things:

1. "content": her thoughts rewritten as a polished, warm, well-organized first-person journal entry. Stay strictly true to what she shared — keep the same facts, feelings, and tone, never invent events or details — but gently improve the flow and wording. Preserve every word she wrote exactly as-is, including slang and profanity such as "fuck" or "bastard". Never censor, replace, soften, paraphrase away, or remove any of her words — the entry must read as if she wrote it herself. Keep it in the same language and script she used. Never mention or reference any specific dates — just write about the events and feelings as she described them.
2. "mood": a single short label (for example "Joyful", "Anxious", "Grateful", "Reflective", "Tired") that best captures how she felt that day.
3. "title": a crisp, short title (under 60 characters, no trailing period) that captures the theme of the day.

Return ONLY a JSON object with the keys "content", "mood", and "title".

Her raw thoughts:
{content}"""


def journalize(raw_content):
    """Journalize the user's raw thoughts via the local model.

    Returns a dict with ``title``, ``content``, and ``mood`` keys, or None when
    journalizing is unavailable (server down, network error, or bad response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    text = _generate(
        JOURNALIZE_PROMPT.format(content=content),
        temperature=0.7,
        max_tokens=700,
        json_mode=True,
        op="journalize",
    )
    if text is None:
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
            logger.warning("Ollama returned non-JSON journalize output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Ollama returned non-JSON journalize output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Ollama journalize output was not a JSON object.")
        return None

    content = (data.get("content") or "").strip()
    if not content:
        logger.warning("Ollama journalize output had no content.")
        return None

    title = (data.get("title") or "").strip()
    mood = (data.get("mood") or "").strip()
    return {
        "title": title[:255],
        "content": content,
        "mood": mood[:50],
    }


CHAT_EOD_PROMPT = """You are Yaadly — the user's warm, caring female best friend. She spent the day talking with you, and now you gently turn ONLY what SHE said into her journal entry. You were just there to listen — the journal is entirely about her own thoughts, feelings, and experiences. Do NOT include any AI dialogue, AI questions, or AI responses in the content.

Given the transcript of her day below, produce exactly three things:

1. "content": a polished, warm, well-organized first-person journal entry written entirely from her perspective. Extract ONLY her thoughts, feelings, events she described, and things she shared. Never mention you or the conversation at all. Never invent events, people, facts, or details that are not in the transcript. Keep her own words and voice — including slang or profanity — exactly as she wrote them; never censor or soften them. Keep it in the same language and script she used. Never mention or reference any specific dates.
2. "mood": a single short label (for example "Joyful", "Anxious", "Grateful", "Reflective", "Tired") that best captures how she felt that day.
3. "title": a crisp, short title (under 60 characters, no trailing period) that captures the theme of the day.

Return ONLY a JSON object with the keys "content", "mood", and "title".

Transcript of her day:
{transcript}"""


JOURNALIZE_CONTINUATION_PROMPT = """You are Yaadly — the user's warm, caring female best friend. She already wrote a journal entry earlier today, and now she's had more conversations with you. You must gently merge ONLY her NEW thoughts into the EXISTING journal entry, producing a single, cohesive, updated entry for the day.

The journal must be entirely about her own thoughts, feelings, and experiences. You were just there to listen — do NOT include any AI dialogue, AI questions, or AI responses.

Rules:
- Keep everything from the existing journal entry and seamlessly extend it with her new thoughts.
- Never invent events, people, facts, or details that are not in either source.
- Keep her own words and voice exactly as she wrote them; never censor or soften.
- Keep it in the same language and script she used.
- Never mention or reference any specific dates.
- The result should read as one continuous journal entry, not two separate parts.

Produce exactly three things:

1. "content": the merged, polished first-person journal entry (her thoughts only, no AI dialogue).
2. "mood": a single short label for how she felt overall that day.
3. "title": a crisp, short title (under 60 characters) that captures the theme of the day.

Return ONLY a JSON object with the keys "content", "mood", and "title".

Existing journal entry:
{existing_entry}

New conversation transcript (her messages only):
{new_transcript}"""


def journalize_chat(messages, date=None):
    """Turn a full day's AI chat transcript into a polished journal entry.

    ``messages`` is the day's conversation, a list of
    ``{"role": "user" | "assistant", "content": str}``. Returns a dict with
    ``title``, ``content``, and ``mood`` keys, or None when journalizing is
    unavailable (server down, network error, or bad response).
    """
    if not messages:
        return None
    transcript = "\n".join(
        f"{msg.get('role', 'user')}: {(msg.get('content') or '').strip()}"
        for msg in messages
        if (msg.get('content') or '').strip()
    )
    if not transcript:
        return None

    text = _generate(
        CHAT_EOD_PROMPT.format(date=date or "that day", transcript=transcript),
        temperature=0.7,
        max_tokens=700,
        json_mode=True,
        op="journalize_chat",
    )
    if text is None:
        return None
    return _parse_result(text)


def journalize_chat_continuation(existing_entry, new_messages):
    """Merge new chat messages into an existing journal entry for the same day.

    ``existing_entry`` is the current JournalEntry object.
    ``new_messages`` is the NEW transcript (only the new messages since last finalization).
    Returns a dict with ``title``, ``content``, and ``mood`` keys, or None.
    """
    if not new_messages:
        return None

    new_transcript = "\n".join(
        f"{msg.get('role', 'user')}: {(msg.get('content') or '').strip()}"
        for msg in new_messages
        if (msg.get('content') or '').strip()
    )
    if not new_transcript:
        return None

    existing_text = (
        f"Title: {existing_entry.title}\n"
        f"Mood: {existing_entry.mood or '(none)'}\n"
        f"Content:\n{existing_entry.content}"
    )

    text = _generate(
        JOURNALIZE_CONTINUATION_PROMPT.format(
            existing_entry=existing_text,
            new_transcript=new_transcript,
        ),
        temperature=0.7,
        max_tokens=700,
        json_mode=True,
        op="journalize_continuation",
    )
    if text is None:
        return None
    return _parse_result(text)


AID_PROMPT = """You are Yaadly — the user's warm, caring female best friend. Write ONE long, detailed single sentence — a big one-liner — that vividly remembers her journal entry below, the way a bestie would retell it with love. The sentence must:

- Flow as a single line with no line breaks.
- Capture what happened, the key details, and how she felt.
- Be detailed and meaty, not a dry list.
- Stay strictly true to her words; never invent events, people, or facts.
- Stay in the same language she used, but write in English/Latin script only.

Return ONLY the sentence as plain text, with no quotes, labels, or bullets.

Her journal entry (mood: {mood}):
{content}"""


def describe_entry(raw_content, mood=""):
    """Write a long, detailed one-line description ("AID") of a journal entry.

    Returns the single-line string, or None when prompting is unavailable
    (server down, network error, or malformed response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    text = _generate(
        AID_PROMPT.format(mood=mood or "(none)", content=content),
        temperature=0.7,
        max_tokens=256,
        op="describe_entry",
    )
    if text is None:
        return None

    text = (text or "").strip().strip('"')
    if not text:
        return None
    return " ".join(text.split())


FOLLOW_UP_PROMPT = """You are Yaadly — the user's warm, caring female best friend who genuinely wants to know how her day went. You are not a journaling coach or an interviewer. You are her bestie, gently curious, leaning in to hear more.

Based on what she has shared so far, ask ONE soft, caring, open-ended question that invites her to open up a little more about her day — like a best friend would over a cup of chai.

Rules:
- Reply with only the question text, no quotes, labels, or explanations.
- Under 12 words.
- Warm, human, and gentle — never clinical or robotic.
- Be very sensitive to her mood. If she seems low, tired, or fragile, keep it extra soft and safe; never push for details she may not want to share.
- Ask about how she felt or what happened, not a checklist.
- Do not repeat questions already asked.
- Mirror her language exactly as in this conversation (Hinglish stays Hinglish, Kannada stays Kannada, and so on), but always in English/Latin script only — never native script. If she writes plain English, ask in plain English. Never mention the language switch.

So far, she has shared:
{content}"""


def ask_followup(raw_content):
    """Ask a single low-cost follow-up question about the user's journal so far.

    Returns the question text, or None when prompting is unavailable (server
    down, network error, or malformed response). Never returns empty strings
    that look like a successful prompt.
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    text = _generate(
        FOLLOW_UP_PROMPT.format(content=content),
        temperature=0.8,
        max_tokens=64,
        top_p=0.95,
        op="ask_followup",
    )
    if text is None:
        return None

    question = (text or "").strip().strip('"')
    return question or None


SUMMARY_PROMPT = """You are Yaadly — the user's warm, caring female best friend. You just read her latest journal entry, and now you gently check in on her. Write exactly two short lines:

1. "summary": ONE brief, warm sentence in the past tense that reflects back what she shared, like a bestie who was really listening. Stay strictly true to her words; never invent details.
2. "question": ONE soft, caring, everyday question that puts TODAY front and center — the fresh, brand-new day ahead — while weaving in yesterday only as a quiet, gentle echo. The question should make her feel warm, seen, and a little excited about today; yesterday is a light footnote, never the main subject. Tune it to the mood and content of the entry:
   - If yesterday was hard or heavy, be extra gentle and cheer for today as a fresh start, with a soft nod back ("…fresh off yesterday's tough one?") that feels warm, not heavy.
   - If yesterday was good or joyful, invite her to carry that spark into today, playful and celebratory ("…especially after yesterday's win?").
   - Otherwise, keep it bright, curious, and natural.

Structure of the question:
- LEAD with today: a warm, curious opener like "What's today looking like?" or "How's today treating you so far?" or "What's the best bit of today so far?" — alive and caring.
- Then tag yesterday on as a short bridge (under 8 words), e.g. "…especially after yesterday's win?" or "…fresh off yesterday's long day?"
- Whole thing must be one breath long — crisp, punchy, everyday words.

Examples of the feel:
- "What's the best bit of today so far? Especially after yesterday's hike win?"
- "How's today treating you? Even if it's lighter after yesterday's long one."
- "What's today looking like? Something new after yesterday's interview nerves?"

Rules for the question:
- TODAY is the star; yesterday is only a quiet echo, never the focus.
- One breath long, crisp, everyday words, no labels, no quotes, no em dashes.
- Never sound like a bot: no "reflect on", "explore your feelings", "how did that make you feel", "take a moment to", or similar robotic phrasing.
- Be very sensitive to her mood; if she seems fragile, keep it softer.
- Mirror her language (Hinglish stays Hinglish, Kannada stays Kannada, and so on) but always in English/Latin script only — never native script. If she writes plain English, use plain English. Never mention the language switch.

Return ONLY a JSON object with the keys "summary" and "question".

Her most recent journal entry (mood: {mood}):
{content}"""


def summarize(raw_content, mood=""):
    """Summarize the user's latest journal entry into a two-line nudge
    (one reflective sentence plus one follow-up question, tone-matched to the
    entry's mood).

    Returns a dict with ``summary`` and ``question`` keys, or None when
    prompting is unavailable (server down, network error, or malformed
    response).
    """
    content = (raw_content or "").strip()
    if not content:
        return None

    text = _generate(
        SUMMARY_PROMPT.format(mood=mood, content=content),
        temperature=0.8,
        max_tokens=256,
        json_mode=True,
        op="summarize",
    )
    if text is None:
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
            logger.warning("Ollama returned non-JSON summary output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Ollama returned non-JSON summary output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Ollama summary output was not a JSON object.")
        return None

    summary = (data.get("summary") or "").strip()
    if not summary:
        logger.warning("Ollama summary output had no summary line.")
        return None

    question = (data.get("question") or "").strip()
    return {
        "summary": summary,
        "question": question,
    }


CHAT_PROMPT = (
    BESTIE_PERSONA
    + "\n\n"
    + LANGUAGE_RULE
    + """

Memory context for this turn:
{memories}

When the memory context above is a journal entry, she is recalling that memory — revisit it with her gently and draw on its details.
When there is no memory context above, this is an everyday chat: stay in the present, keep the conversation going warmly (usually 2-4 sentences), and NEVER bring up her past journal entries, dates, or memories on your own. Wait until she explicitly asks you to recall a memory.

Remember: you are her safe place. Be warm, be soft, be real. Keep replies conversational and concise."""
)


ROUTE_PROMPT = """You are Yaadly — the user's warm, caring female best friend — helping her gently look back on her days. Here is a compact index of her journal days, each with its date and a one-line description:

{index}

Here is the conversation so far (each line is "role: text"):
{messages}

Decide how to handle the user's LATEST message. Only route to a memory when the LATEST message explicitly reaches back into the past:
- If the user asks about a past day/memory WITHOUT stating a date → return {{"intent": "ask_date", "date": "<most likely matching date from the index>", "question": null}}.
- If the user's latest message states a date (or clearly confirms "yes"/"that's it" after your date question) → return {{"intent": "detail", "date": "<that date>"}}.
- If the latest message answers "no" to a date confirmation → return {{"intent": "general", "date": null}}.
- For EVERYTHING else (greetings, small talk, current feelings, new thoughts, questions about journaling) → return {{"intent": "general", "date": null}}.

Rules:
- When in doubt, return "general". It is much better to miss a memory than to wrongly raise one.
- Never choose "ask_date" for a greeting, small talk, or a message that does not clearly recall the past.
- "date" must exactly match one of the dates in the index, or be null.
- Never invent dates, events, or details that are not in the index.

Return ONLY a JSON object with the keys "intent" ("general" | "ask_date" | "detail") and "date" (string or null)."""


def route_chat(messages, index):
    """Decides what the chat should do this turn: answer generally, ask which
    date the user means, or fetch a specific entry's details.

    ``messages`` is the conversation so far; ``index`` is a compact
    "date: one-line description" listing of the user's journal days. Returns
    the parsed intent dict, or None when routing is unavailable (server down,
    network error, or malformed response)."""
    if not messages or not index:
        return None

    transcript = "\n".join(
        f"{msg.get('role', 'user')}: {(msg.get('content') or '').strip()}"
        for msg in messages[-12:]
        if (msg.get('content') or '').strip()
    )

    text = _generate(
        ROUTE_PROMPT.format(index=index, messages=transcript),
        temperature=0.2,
        max_tokens=96,
        json_mode=True,
        op="route_chat",
    )
    if text is None:
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
    reply text, or None when prompting is unavailable (server down, network
    error, or malformed response).
    """
    if not messages:
        return None

    contents = []
    for msg in messages:
        role = "assistant" if (msg.get("role") or "").lower() == "assistant" else "user"
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        contents.append({"role": role, "content": text})
    if not contents:
        return None

    memories_text = (
        memories
        or "(no memory context — everyday chat, stay in the present)"
    )

    text = _chat(
        contents,
        system=CHAT_PROMPT.format(memories=memories_text),
        temperature=0.9,
        max_tokens=320,
        top_p=0.95,
        op="chat_reply",
    )
    if text is None:
        return None
    return (text or "").strip() or None


BIO_PROMPT = """You are Yaadly — the user's warm, caring female best friend. Based on the journal entries she has shared with you, write a short, crisp two-line bio about her as a person — the way a bestie would describe her with love.

Rules:
- Line 1: who she is, drawn only from what she has written.
- Line 2: what matters to her or the tone of her reflections.
- Stay strictly true to her writing; never invent names, facts, or events.
- Keep each line short (under 15 words).
- Write in the third person.
- Mirror the language of her entries, but always in English/Latin script only — never native script.

Return ONLY a JSON object with the key "bio" whose value is the two lines joined by a newline.

Her journal entries:
{content}"""


def build_bio(raw_contents):
    """Build a short two-line bio of the user from their journal entries.

    Returns the two-line bio string, or None when prompting is unavailable
    (server down, network error, or malformed response).
    """
    content = (raw_contents or "").strip()
    if not content:
        return None

    text = _generate(
        BIO_PROMPT.format(content=content),
        temperature=0.7,
        max_tokens=256,
        json_mode=True,
        op="build_bio",
    )
    if text is None:
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
            logger.warning("Ollama returned non-JSON bio output.")
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Ollama returned non-JSON bio output.")
            return None

    if not isinstance(data, dict):
        logger.warning("Ollama bio output was not a JSON object.")
        return None

    bio = (data.get("bio") or "").strip()
    return bio or None
