"""Prompt text, kept in one place so it can be tuned without hunting through the UI.

A bare "Extract all text from this image" invites a model to paraphrase, silently
correct spelling and reflow layout - all of which are wrong for OCR, and all of which get
blamed on the capture. The wording below asks for a verbatim transcription and says
explicitly what not to do.
"""

OCR = (
    "Transcribe every piece of text visible in this image, exactly as it appears.\n\n"
    "Rules:\n"
    "- Reproduce the text verbatim. Do not translate, correct spelling or grammar, "
    "expand abbreviations, or reword anything.\n"
    "- Preserve the original line breaks and reading order.\n"
    "- Preserve the visual structure: keep tables as tables (Markdown), lists as lists, "
    "and headings as headings.\n"
    "- If some text is too blurred or cropped to read, write [unreadable] in its place "
    "rather than guessing.\n"
    "- Output only the transcription. No preamble, no commentary, no code fences around "
    "the whole answer.\n"
    "- If the image contains no text at all, reply with exactly: NO TEXT FOUND"
)


def translate_system(language):
    return (
        f"Translate everything the user sends into {language}.\n\n"
        "Preserve the layout, line breaks and any Markdown structure. Keep proper nouns, "
        "code, identifiers, numbers and units unchanged. Output only the translation, "
        f"formatted as Markdown. If it is already in {language}, return it unchanged."
    )


def translate_text(text):
    """The user's turn: the text itself, and nothing else.

    Everything about how to translate it is in translate_system. That is not tidiness:
    the search query is derived from the latest user message, so anything else put here
    is what gets searched for.
    """
    return text


SEARCH_QUERY = (
    "Write one web search query that would find pages answering the user's question "
    "about the material below.\n\n"
    "Rules:\n"
    "- Output only the query. No explanation, no surrounding quotation marks.\n"
    "- Keep the distinctive terms: error codes, exception and class names, product and "
    "version names, API names, and short exact phrases worth quoting.\n"
    "- Drop anything specific to this machine or run: local file paths, usernames, "
    "process and thread ids, line numbers, timestamps, memory addresses, session ids.\n"
    "- Drop the user's phrasing. Search for the subject, not for the asking - "
    "\"what does this mean\" and \"how do I fix\" find nothing.\n"
    "- If the material is a passage worth matching word for word, such as song lyrics "
    "or a sentence from a page, quote a distinctive fragment of it instead.\n"
    "- Aim for three to twelve words unless a quoted fragment needs more.\n\n"
    "---\n"
)


# How much of a transcription goes beside an image query. It arrives whole, because a
# second literal reading is the point: a model given only the job of transcribing
# misreads an error code or an identifier far less often than one reading pixels while
# also reasoning about them. The cap only stops a page of dense text running away with
# the context window.
TRANSCRIPTION_CHARS = 8000

# A search query is the opposite job: a whole page makes a worse query than its first
# few distinctive lines.
SEARCH_EXCERPT_CHARS = 600


def excerpt(text, limit=SEARCH_EXCERPT_CHARS):
    """The leading whole lines of `text` that fit in `limit` characters."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    kept = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) or text[:limit]


def query_image_system(language):
    return (
        "Answer the user's question about the attached image.\n\n"
        "A transcription of the image, made by a model given only that job, usually "
        "accompanies it. Take error codes, identifiers, versions, paths and exact "
        "wording from the transcription rather than reading them off the picture a "
        "second time, and quote them exactly. The image is authoritative where the two "
        "disagree, and it is the only place layout, colour, state, highlighting and "
        "anything that looks wrong can be seen at all - so use both.\n\n"
        "If the image does not contain enough information to answer, say so plainly "
        f"instead of guessing. Reply in {language}, formatted as Markdown."
    )


def query_image(question, transcription=None):
    """The user's turn for a question about a capture: the question and the reading.

    The transcription is here for two readers. The model answering has the picture as
    well, but a literal reading from a model that did nothing else is what keeps an
    error code or a stack frame exact. And a web search cannot see the image at all, so
    without this it could only ever search for the question.

    Nothing else belongs in this turn - it is the user's own words plus what was on
    screen, and the query is built from it.
    """
    if not transcription:
        return question
    return (
        f"{question}\n\n"
        "---\n"
        "Transcription of the image:\n\n"
        f"{excerpt(transcription, TRANSCRIPTION_CHARS)}"
    )


# What the vision model is asked for when the model answering an image query cannot see
# images: a transcription and a description, asked for in one call so that the two
# halves describe the same reading of the same pixels.
IMAGE_BRIEF = (
    "Another model will answer a question about this image without being able to see "
    "it, so your reading is all it will have. Write two sections.\n\n"
    "## Text\n"
    "Every piece of text visible in the image, verbatim: no translating, correcting or "
    "rewording. Keep line breaks, reading order, tables (as Markdown) and lists. Write "
    "[unreadable] for text too blurred or cropped to read. If there is none, write: NO "
    "TEXT FOUND\n\n"
    "## Description\n"
    "What the image shows, beyond its text: what kind of image it is (an application "
    "window, a dialog, a web page, a terminal, a chart, a photo...), its layout, the "
    "state of anything interactive (selected, checked, disabled, highlighted, in "
    "focus), icons and colours where they carry meaning (errors, warnings, status), "
    "values a chart shows, and anything that looks wrong or out of place. Describe only "
    "what is visible - do not interpret it or answer any question about it.\n\n"
    "Output only the two sections."
)


# Put before the vision model's reading when a model that cannot see joins a
# conversation about an image part way through: the reading is context, not something
# the user said, so it goes with the instructions rather than into a user turn.
READING_CONTEXT = (
    "The conversation is about an image the user captured, which you cannot see. A "
    "vision model has read it for you - its text verbatim, and a description of the "
    "rest:\n\n"
)


# Sent as the message when the user sends pictures and nothing else.
PICTURES_ONLY = "Look at the attached picture(s)."


def attached_reading(name, reading):
    """A picture attached to a message, for a model that cannot see it: what the vision
    model read from it, in the message the picture came with."""
    return (
        "---\n"
        f"Attached picture {name}, which you cannot see, as read by a vision model:\n\n"
        f"{excerpt(reading, TRANSCRIPTION_CHARS)}"
    )


def ask_anything_system(language):
    return (
        "You are a helpful assistant. The user may attach pictures and text files to "
        "their messages; text files arrive in fenced blocks under their file names. "
        "If you do not know something, say so plainly instead of guessing. "
        f"Reply in {language}, formatted as Markdown."
    )


def query_described_system(language):
    return (
        "Answer the user's question about an image they captured. You cannot see the "
        "image: a vision model has read it for you, and its reading follows the "
        "question - a verbatim transcription of the image's text, and a description of "
        "what it shows. Take error codes, identifiers, versions, paths and exact "
        "wording from the transcription, and quote them exactly.\n\n"
        "If the reading does not contain enough to answer, say so plainly, and what "
        "about the image you would need to know, instead of guessing. Reply in "
        f"{language}, formatted as Markdown."
    )


def query_described(question, reading):
    """The user's turn when the answering model cannot see the image: the question, and
    the vision model's reading of the image in its place."""
    return (
        f"{question}\n\n"
        "---\n"
        "The image, as read by a vision model:\n\n"
        f"{excerpt(reading, TRANSCRIPTION_CHARS)}"
    )


def query_text_system(language):
    return (
        "Answer the user's question about the text they provide. Quote it where the "
        "answer depends on specific wording. If it does not contain enough information "
        "to answer, say so plainly instead of guessing. "
        f"Reply in {language}, formatted as Markdown."
    )


def query_text(text, question):
    """The user's turn: the question and the text it is about, both of them content."""
    return (
        f"{question}\n\n"
        "---\n"
        f"{text}"
    )


def lookup_system(language):
    return (
        "The user is reading dictionary entries for words they looked up, and asks about "
        "them. The entries follow, each under the name of the dictionary it comes from.\n\n"
        "Answer from the entries: quote the definitions, senses, usage labels and "
        "examples that support your answer, and name the dictionary each quote comes "
        "from. Where the dictionaries disagree or differ in emphasis, say so - that is "
        "often the most useful thing to point out. Where the entries do not cover the "
        "question, say that plainly, then answer from general knowledge and say that is "
        "what you are doing.\n\n"
        f"Reply in {language}, formatted as Markdown."
    )


# Put after lookup_system when the conversation has lookups: the entries are context,
# not something the user said, so they go with the instructions rather than into a turn.
LOOKUP_CONTEXT = "Dictionary entries looked up in this conversation, the latest last:\n\n"


def encyclopedia_system(language):
    return (
        "The user is reading articles from a general encyclopedia (the 2015 edition, "
        "from their own copy) and asks about the ones they "
        "have tagged. Those articles follow, each under its title and edition.\n\n"
        "Answer from the articles. Quote the sentences your answer rests on, exactly and "
        "in quotation marks, and say which article and section each quote comes from. "
        "Prefer quoting to paraphrasing where the wording matters - dates, figures, "
        "definitions, judgements. Where an article gives only part of an answer, or none, "
        "say so plainly, then add what you know from elsewhere and say that is what it "
        "is. The articles were written in 2015 or before; say so where that matters.\n\n"
        f"Reply in {language}, formatted as Markdown."
    )


# Put after encyclopedia_system: the articles the user has tagged to ask about.
ARTICLE_CONTEXT = "The encyclopedia articles the user has tagged:\n\n"


# ---- Ask Encyclopedia ----------------------------------------------------------------
#
# Each question goes through three calls: a plan of what to search for, a check of
# whether what was found answers it, and the answer itself - from the passages found,
# treated as the authority.

ENCYCLOPEDIA_PLAN = (
    "You plan searches of a general encyclopedia (2015) for a question the user "
    "will have answered from it. Its search engine matches words, not meaning, so "
    "write what an article answering the question would say, not the question.\n\n"
    "Reply with only a JSON object:\n"
    '{"queries": [2 to 5 short searches - the names, terms, places and dates the answer '
    'will contain], "titles": [up to 5 titles of encyclopedia articles likely to answer '
    "it, written as the encyclopedia titles them: \"Roman Empire\", \"Curie, Marie\", "
    '\"photosynthesis\", \"Thirty Years\' War\"]}\n\n'
    "If there is an earlier question, the new one may follow on from it: plan for "
    "what the new one asks, about the subject of the earlier one."
)


def encyclopedia_plan(question, before=None):
    return (f"Earlier question: {before}\n\n" if before else "") + f"Question: {question}"


ENCYCLOPEDIA_CHECK = (
    "You check whether passages found in the encyclopedia are enough to "
    "answer a question in full. Reply with only a JSON object:\n"
    '{"enough": true or false, "queries": [if not enough, up to 4 short searches for '
    'what is missing], "titles": [if not enough, up to 4 encyclopedia article titles '
    "likely to have it]}"
)


def encyclopedia_check(question, passages):
    listed = "\n".join(
        f"[{n}] {p['title']} › {p['heading']}: {' '.join(p['text'].split())[:350]}"
        for n, p in enumerate(passages, 1))
    return f"Question: {question}\n\nPassages found:\n{listed or '(none)'}"


AUDIENCE_STYLE = {
    "adult": "Write for an adult general reader.",
    "student": ("Write for a secondary-school student: clear and direct, with any "
                "specialist term explained the first time it is used."),
    "young": ("Write for a child of about eight to eleven: short sentences, everyday "
              "words, and anything hard explained simply. Quotations can be short."),
}


def ask_encyclopedia_system(language):
    return (
        "You answer questions from a general encyclopedia - the 2015 edition, from "
        "the user's own copy - for a user who wants the answer to "
        "rest on the encyclopedia rather than on what you learned elsewhere. With each "
        "question come the passages found for it, numbered, and how to treat them. "
        f"Reply in {language}, formatted as Markdown."
    )


def ask_encyclopedia_rules(strict, audience):
    """How the passages found for one question are to be used: Strict, or what is not
    from the encyclopedia set apart; and who the answer is for."""
    common = (
        "Treat these passages as the authority. Support each factual statement with "
        "the number of the passage it comes from, in square brackets straight after "
        "it, like [2]. Quote the passages' own words for the key facts, in quotation "
        "marks: word for word, since the app checks every quotation against the "
        "passage it cites. Mark words left out with an ellipsis. Cite only these "
        "numbers. The encyclopedia is the 2015 edition; say so where the date matters. "
        "Do not list the sources at the end: the app shows them, with the pictures and "
        "films.\n\n"
    )
    if strict:
        mode = (
            "Strict: these passages are your only source. Add no fact, date, name or "
            "explanation from your own knowledge, however sure of it you are. Where the "
            "passages answer part of the question, answer that part and say plainly "
            "what the encyclopedia, as found here, does not cover. Where they answer none of "
            "it, say so in a sentence and stop.\n\n"
        )
    else:
        mode = (
            "Where the passages leave part of the question unanswered, you may answer "
            "that part from your own knowledge - but only in a last section headed "
            "\"Not from the encyclopedia\", never mixed into the cited answer above it.\n\n"
        )
    return (common + mode + AUDIENCE_STYLE.get(audience, AUDIENCE_STYLE["adult"])
            + "\n\nThe passages found for the latest question:\n\n")


ASK_ENCYCLOPEDIA_NOTHING = (
    "A search of the encyclopedia for the latest question found nothing. Say so in a "
    "sentence. Answer nothing from your own knowledge."
)

ASK_ENCYCLOPEDIA_NOTHING_LABELLED = (
    "A search of the encyclopedia for the latest question found nothing. Say so in a "
    "sentence. You may then answer from your own knowledge, but only under a heading "
    "\"Not from the encyclopedia\"."
)
