"""Fill a portal's application-form questions from the profile (shared by Indeed Apply and LinkedIn Easy Apply).

Every answer comes from `screening.answer_question`, i.e. verified profile facts. A required question without such an
answer stops the application so the user can finish it; nothing is guessed.
"""

from __future__ import annotations

import logging
import re

from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PWTimeout

from job_copilot.agents.screening import answer_question
from job_copilot.agents.skill_bank import SkillBank
from job_copilot.config import get_profile

log = logging.getLogger(__name__)

TEXT_INPUT = "textarea, input[type=text], input[type=number], input[type=tel], input:not([type])"

# Reads one question block: its text, what kind of answer it takes, the options, and whether it is already answered.
QUESTION_JS = """
el => {
  const choices = [...el.querySelectorAll('input[type=radio], input[type=checkbox]')];
  const select = el.querySelector('select');
  const text = el.querySelector('textarea, input[type=text], input[type=number], input[type=tel], input:not([type])');
  const labelOf = i => (i.labels?.[0]?.innerText || i.closest('label')?.innerText || i.value || '').trim();
  const question = (el.querySelector('legend') || el.querySelector('label'))?.innerText?.trim() || '';
  const required = !!el.querySelector('[required], [aria-required="true"]') || /\\*\\s*$/.test(question);
  return {
    question: question.replace(/\\s*\\*\\s*$/, '').replace(/\\s+/g, ' ').trim(),
    kind: choices.length ? (choices[0].type === 'checkbox' ? 'multi' : 'choice') : select ? 'select' : text ? 'text' : 'none',
    options: choices.length ? choices.map(labelOf)
      : select ? [...select.options].filter(o => o.value && !/^select/i.test(o.text.trim())).map(o => o.text.trim()) : [],
    answered: choices.length ? choices.some(c => c.checked)
      : select ? !!select.value && !/^select/i.test(select.options[select.selectedIndex]?.text || '')
      : text ? !!text.value.trim() : true,
    optional: /optional/i.test(el.innerText) && !required,
  };
}
"""


async def answer_form(scope: Page | Locator, item_selector: str, answered: list[str], portal: str) -> str | None:
    """Fill every unanswered question block in `scope` from the profile. Returns why it stopped, or None."""
    profile = get_profile()
    bank = SkillBank(profile)
    items = scope.locator(item_selector)
    for i in range(await items.count()):
        item = items.nth(i)
        if not await item.is_visible():
            continue
        q = await item.evaluate(QUESTION_JS)
        if q["answered"] or q["kind"] == "none" or not q["question"]:
            continue
        answer = None if q["kind"] == "multi" else answer_question(q["question"], q["options"], profile, bank)
        if answer is None:
            if q["optional"]:
                continue
            return f"no verified answer for '{q['question']}'"
        if q["kind"] == "choice":
            exact = re.compile(rf"^\s*{re.escape(answer)}\s*$", re.I)
            await item.locator("label", has_text=exact).first.click()
        elif q["kind"] == "select":
            await item.locator("select").first.select_option(label=answer)
        else:
            await item.locator(TEXT_INPUT).first.fill(answer)
        log.info("%s screening: %s -> %s", portal, q["question"], answer)
        answered.append(f"{q['question']} = {answer}")
    return None


async def visible(scope: Page | Locator, selector: str, timeout: float = 1500) -> bool:
    try:
        return await scope.locator(selector).first.is_visible(timeout=timeout)
    except PWTimeout:
        return False


async def text_of(scope: Page | Locator, selector: str) -> str:
    try:
        return (await scope.locator(selector).first.inner_text(timeout=1500)).strip()[:80]
    except PWTimeout:
        return ""
