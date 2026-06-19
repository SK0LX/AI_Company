"""Unit tests for the group-chat presence guardrails. No LLM, no network.

    python tests/test_group_chat.py
"""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import group_chat as gc
from src.config import settings

CHAT = -100777  # unique test chat id
ROSTER = [
    ("developer", "Backend-разработчик", "developer"),
    ("frontend", "Frontend-разработчик", "frontend"),
    ("tester", "QA-тестировщик", "tester"),
]


def main() -> None:
    settings.enable_group_chat = True
    settings.group_cooldown = 6
    settings.group_ambient_prob = 0.15
    gc._states.pop(CHAT, None)

    # transcript
    gc.observe(CHAT, name="Max", text="привет всем")
    gc.observe(CHAT, name="Yarik", text="как дела с API?")
    assert gc.transcript_text(CHAT) == "Max: привет всем\nYarik: как дела с API?"

    # addressed detection: by slug, by role — on WORD BOUNDARIES
    assert gc.detect_addressed("tester что думаешь?", ROSTER) == "tester"
    assert gc.detect_addressed("developer, глянь плиз", ROSTER) == "developer"
    assert gc.detect_addressed("просто болтаем ни о чём", ROSTER) is None
    assert gc.detect_addressed("protesters gathered today", ROSTER) is None  # not 'tester'

    # work intent: whole-word EN + RU stems (first-person counts; no substring noise)
    assert gc.work_intent("создай файл main.py")
    assert gc.work_intent("please implement the endpoint")
    assert gc.work_intent("сделаю это завтра")          # RU stem 'сдела'
    assert gc.work_intent("добавлю валидацию")          # RU stem 'добав'
    assert not gc.work_intent("норм, спасибо")
    assert not gc.work_intent("это просто префикс")     # 'префикс' must NOT read as 'fix'

    # questions are detected WITHOUT a literal "?" (the bug a user hit)
    assert gc.is_question("что вы сейчас делаете каждый")
    assert gc.is_question("расскажи о себе")
    assert gc.is_question("how does this work")
    assert not gc.is_question("да бл")
    assert not gc.is_question("ок, понял")
    # "to everyone" questions invite several to answer; a plain greeting does not
    assert gc.is_to_everyone("что вы все думаете")
    assert not gc.is_to_everyone("developer, глянь")
    # gate passes a question even with no "?"
    gc._states.pop(CHAT, None)
    assert gc.should_consider(CHAT, "что вы делаете", addressed=False, is_followup=False)

    # cooldown + mark_post + last_speaker + dedup
    assert gc.cooldown_ok(CHAT)  # nobody posted yet
    gc.mark_post(CHAT, "developer", "Backend-разработчик", "готово, залил в backend/")
    assert gc.last_speaker(CHAT) == "developer"
    assert not gc.cooldown_ok(CHAT)  # just posted -> within cooldown
    assert gc.is_duplicate(CHAT, "готово,   залил в backend/")  # whitespace-insensitive
    assert not gc.is_duplicate(CHAT, "что-то другое")
    # cooldown clears after the window
    assert gc.cooldown_ok(CHAT, now=gc.state(CHAT).last_post + settings.group_cooldown + 1)

    # should_consider gate
    gc._states.pop(CHAT, None)  # fresh, no cooldown
    # disabled flag wins
    settings.enable_group_chat = False
    assert not gc.should_consider(CHAT, "создай?", addressed=True, is_followup=False)
    settings.enable_group_chat = True
    # addressed / question / work-intent -> always consider (non-followup)
    assert gc.should_consider(CHAT, "что делаем?", addressed=False, is_followup=False)
    assert gc.should_consider(CHAT, "ничего", addressed=True, is_followup=False)
    assert gc.should_consider(CHAT, "сделай форму", addressed=False, is_followup=False)
    # ambient chatter depends on probability
    settings.group_ambient_prob = 1.0
    assert gc.should_consider(CHAT, "ну такое", addressed=False, is_followup=False)
    settings.group_ambient_prob = 0.0
    assert not gc.should_consider(CHAT, "ну такое", addressed=False, is_followup=False)
    # cooldown blocks a NEW (non-followup) message...
    gc.mark_post(CHAT, "tester", "QA", "ok")
    assert not gc.should_consider(CHAT, "tester?", addressed=True, is_followup=False)
    # ...but follow-up turns within a burst IGNORE cooldown (paced by the sleep+cap)
    assert gc.should_consider(CHAT, "", addressed=False, is_followup=True, rng=random.Random(1))      # r≈0.13 < 0.5
    assert not gc.should_consider(CHAT, "", addressed=False, is_followup=True, rng=random.Random(0))  # r≈0.84 ≥ 0.5

    gc._states.pop(CHAT, None)
    print("group chat tests: OK")


if __name__ == "__main__":
    main()
