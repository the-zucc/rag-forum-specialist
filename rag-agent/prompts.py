"""Prompts for the planner, distiller, cross-thread check, and answerer."""

from __future__ import annotations

MAX_PROMPT_PIECES = 40


def render_piece_list(pieces):
    lines = []
    for i, piece in enumerate(list(pieces)[:MAX_PROMPT_PIECES], 1):
        statement = piece["statement"]
        if len(statement) > 400:
            statement = statement[:400] + "…"
        lines.append(f'{i}. {statement} (from thread: "{piece["thread_title"]}")')
    return "\n".join(lines) if lines else "(none retrieved yet)"


def build_planner_prompt(query, pieces, tried, budget_left):
    tried_text = (
        "\n".join("- " + ", ".join(kws) for kws in tried) if tried else "(none yet)"
    )
    return (
        "You are the research planner of an agent answering a question from a "
        "knowledge base of standalone facts distilled from an internet "
        "forum.\n\n"
        f'The question: "{query}"\n\n'
        f"Knowledge pieces retrieved so far:\n{render_piece_list(pieces)}\n\n"
        f"Keyword searches already tried:\n{tried_text}\n\n"
        "Decide the next step:\n"
        "- SEARCH: the pieces leave a gap. Provide NEW keywords aimed at what "
        "is STILL MISSING — prefer specific, multi-word keywords (a named "
        "detail, a symptom, a procedure), never a repeat of an already-tried "
        "search.\n"
        "- DIRECT_HIT: one or more pieces ALREADY ANSWER the question "
        "directly. Name them in HIT_PIECES.\n"
        "- SUFFICIENT: the pieces collectively frame the answer well enough "
        "to go read their source threads in full.\n"
        + (
            ""
            if budget_left
            else "The search budget is spent: you may only choose DIRECT_HIT or "
            "SUFFICIENT now.\n"
        )
        + "If no pieces have been retrieved yet, you must choose SEARCH.\n\n"
        "Respond in exactly this format and nothing else:\n"
        "ASSESSMENT: <one or two sentences: what the pieces cover, what is missing>\n"
        "STATUS: <SEARCH or DIRECT_HIT or SUFFICIENT>\n"
        "KEYWORDS: <comma-separated search keywords, only when STATUS is SEARCH>\n"
        "HIT_PIECES: <comma-separated piece numbers, only when STATUS is DIRECT_HIT>"
    )


def build_distill_prompt(query, thread, thread_text):
    """The knowledge processor's extraction prompt, with the user's query
    included as the area of interest steering the model's attention."""
    return (
        "You are extending a knowledge base built from an internet forum. "
        "Below is one complete discussion thread, its posts in "
        "chronological order. Extract the durable KNOWLEDGE it establishes "
        "about the subject under discussion — NOT a summary of the conversation.\n\n"
        f'AREA OF INTEREST — a user is currently researching: "{query}"\n'
        "Pay particular attention to passages that bear on that question, but "
        "each knowledge piece must still:\n"
        "- Stand on its own: understandable without reading the thread or the "
        "question. Name the subject explicitly; do not write 'as mentioned "
        "above' or 'he said'.\n"
        "- Be durable and factual: a specification, how something works, a known "
        "failure mode, or a fix a poster CONFIRMED worked. Later posts may "
        "correct earlier ones and the original poster often reports what actually "
        "worked — trust those.\n"
        "- Not include for-sale notes, greetings, arguments, or speculation.\n\n"
        f'Thread title: "{thread["title"]}"\n'
        f"Posts ({len(thread['posts'])} total):\n\n{thread_text}\n\n"
        "Respond with ONE knowledge piece per line, in exactly this format:\n"
        "- <the fact, one sentence or two> [posts: <post_id>, <post_id>]\n"
        "listing the ids of the posts each fact came from. Write only the lines, "
        "no preamble or numbering. If the thread establishes nothing durable, "
        "respond with exactly: NONE"
    )


def build_crosscheck_prompt(query, pieces, threads):
    thread_lines = "\n".join(
        f'- "{t["title"]}" ({len(t["posts"])} posts)' for t in threads
    ) or "(none)"
    return (
        "You are the research planner of an agent answering a question from an "
        "internet forum's knowledge base. The source threads below "
        "have been read in full and distilled; the knowledge pieces are what is "
        "known so far.\n\n"
        f'The question: "{query}"\n\n'
        f"Knowledge pieces:\n{render_piece_list(pieces)}\n\n"
        f"Threads already read in full:\n{thread_lines}\n\n"
        "Decide:\n"
        "- ANSWER: taken across the threads, the pieces close the question.\n"
        "- MORE: reading the threads surfaced leads worth one more search of "
        "the knowledge base — a referenced discussion, a named detail or "
        "procedure, or a contradiction between threads that another thread "
        "might settle. Provide LEAD_KEYWORDS for that search.\n\n"
        "Respond in exactly this format and nothing else:\n"
        "ASSESSMENT: <one or two sentences>\n"
        "STATUS: <ANSWER or MORE>\n"
        "LEAD_KEYWORDS: <comma-separated keywords, only when STATUS is MORE>"
    )


def build_answer_prompt(query, pieces, threads, thread_texts):
    sections = []
    for thread in threads:
        text = thread_texts.get(thread["thread_id"], "")
        url = f" ({thread['url']})" if thread.get("url") else ""
        sections.append(f'### Thread: "{thread["title"]}"{url}\n\n{text}')
    threads_text = "\n\n".join(sections) or "(no threads were reconstructed)"
    piece_lines = []
    for piece in list(pieces)[:MAX_PROMPT_PIECES]:
        url = f" — {piece['thread_url']}" if piece.get("thread_url") else ""
        piece_lines.append(f'- {piece["statement"]} (thread: "{piece["thread_title"]}"{url})')
    pieces_text = "\n".join(piece_lines) or "(none)"
    return (
        "Answer the user's question using ONLY the knowledge pieces and forum "
        "threads below. Be direct and practical; when a fact or claim comes from "
        "a specific thread, cite that thread by title (and URL when given). If "
        "the sources do not contain the answer, say so plainly and point to "
        "the closest threads — do NOT answer from prior knowledge.\n\n"
        f'The question: "{query}"\n\n'
        f"Knowledge pieces (standalone facts, each with its source thread):\n"
        f"{pieces_text}\n\n"
        f"Reconstructed source threads:\n\n{threads_text}\n\n"
        "Now write the answer."
    )
