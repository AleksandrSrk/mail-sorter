import textwrap

import pytest

from app.db import Store

RULES = """
    _all:
      rules:
        - name: Спам
          from: [Bad@Spam.ru, news@legit.ru]
          from_domain: [junk.example]
          spam_header: true
          learn: true
          move_to: Junk
          mark_read: true
      learn_spam:
        folder: Junk
        never: [friend@gmail.com, "@partner.ru"]
    box@our.ru:
      rules:
        - name: Партнёр
          from: [p@mail.ru]
          move_to: ПАРТНЁР
"""


@pytest.fixture
def store(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(textwrap.dedent(RULES), encoding="utf-8")
    learned = tmp_path / "learned_spam.txt"
    learned.write_text("old@spam.ru\n# комментарий\nbad@spam.ru\n", encoding="utf-8")
    s = Store(str(tmp_path / "mail.db"))
    assert s.seed(str(rules), str(learned))
    return s
