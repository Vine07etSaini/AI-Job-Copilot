"""Deterministic 0-100 match score with a human-readable explanation."""

from __future__ import annotations

import re

from job_copilot.agents.skill_bank import SkillBank, find_term, name_forms, norm
from job_copilot.models import JDAnalysis, JobPosting, MatchResult, Profile, ScoreWeights, SkillRequirement

# Spellings folded to one token before a title is compared with your roles.
TITLE_PHRASES = [
    (re.compile(r"\bback[\s-]?end\b", re.I), "backend"),
    (re.compile(r"\bfront[\s-]?end\b", re.I), "frontend"),
    (re.compile(r"\bfull[\s-]?stack\b", re.I), "fullstack"),
    (re.compile(r"\bgen[\s-]?ai\b", re.I), "generative ai"),
]
TOKEN_RE = re.compile(r"[a-z0-9+#]+")
# Words that say "this is an engineering job" but nothing about which kind.
FAMILY_WORDS = {"developer", "developers", "engineer", "engineers", "programmer", "sde", "swe", "dev",
                "development", "engineering", "software"}
# Seniority, hiring noise and filler: they never make a title more or less relevant.
GENERIC_WORDS = {
    "senior", "sr", "junior", "jr", "associate", "lead", "staff", "i", "ii", "iii", "iv", "level", "and", "or",
    "with", "for", "in", "at", "the", "a", "an", "of", "to", "remote", "wfh", "hybrid", "onsite", "urgent",
    "urgently", "hiring", "immediate", "immediately", "joiner", "joiners", "walk", "opening", "openings",
    "opportunity", "opportunities", "fresher", "freshers", "experienced", "exp", "yrs", "years", "yr", "work",
    "from", "home", "office", "female", "male", "preferred", "s", "job", "role", "position", "based", "only",
    "intern", "internship", "graduate", "trainee", "web", "technical", "technology", "application", "applications",
    "custom", "professional", "it",
}
# Words inside your skill names that are too broad to tell job titles apart ("Data Structures" is not "Data Engineer").
SKILL_STOPWORDS = {"data", "design", "integration", "search", "framework", "object", "oriented", "programming",
                   "structures", "algorithms", "api", "apis", "rest", "js"}

# Suffixes and filler that appear in registered company names but say nothing about which company it is.
COMPANY_NOISE = {"pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "corp", "corporation", "co",
                 "technologies", "technology", "solutions", "services", "systems", "software", "labs", "group",
                 "global", "consulting", "india", "the"}

TITLE_BONUS = 2.0      # a skill named in the job title is the core of the role
REPEAT_BONUS = 0.5     # each extra mention in the description ...
MAX_REPEATS = 2        # ... counted up to this many times
FULL_CONFIDENCE_SKILLS = 4  # fewer listed skills than this and the score is scaled down: too little to judge
ROLE_FIT_FAMILY = 0.5       # "Java Developer": engineering in a stack you don't list, not another profession
CARD_NEUTRAL = 0.5          # a card that says nothing about skills is neither promoted nor demoted
SNIPPET_FULL_CREDIT = 3     # your skills named in a card snippet for the full promotion


def experience_fit(yours: float, lo: float | None, hi: float | None) -> float:
    if lo is not None and yours < lo:
        return max(0.0, 1 - (lo - yours) / 2)  # lose 50% per missing year
    if hi is not None and yours > hi + 2:
        return 0.6  # clearly over-qualified
    return 1.0


def _tokens(text: str) -> set[str]:
    text = text.split("@")[0]  # "Python Developer @ Infosys": the company is not part of the role
    for rx, repl in TITLE_PHRASES:
        text = rx.sub(repl, text)
    return {t for t in TOKEN_RE.findall(text.lower()) if not t.isdigit() and any(c.isalpha() for c in t)}


def _role_words(profile: Profile) -> set[str]:
    """Spelling-folded words that make a title relevant: your roles plus the distinctive words of your skills."""
    words = set().union(*(_tokens(r) for r in profile.preferences.roles))
    for s in profile.skills:
        for name in (*name_forms(s.name), *s.aliases):
            words |= _tokens(name) - SKILL_STOPWORDS
            words.add(name)  # the whole skill too: "ReactJS" in a title is your "React.js" once folded
    return {norm(w) for w in words - FAMILY_WORDS - GENERIC_WORDS}


def role_fit(title: str, company: str, profile: Profile) -> float:
    """0-1: how close a job title is to the work you do.

    1.0 when it names one of your roles or skills, or says nothing specific; ROLE_FIT_FAMILY for an engineering
    title in a stack you don't list; 0.0 for another line of work ("Tax Documentation Analyst").
    """
    words = _tokens(title)
    specific = words - FAMILY_WORDS - GENERIC_WORDS - _tokens(company)
    if not specific:
        return 1.0  # "Software Engineer II": says nothing against you
    known = _role_words(profile)
    if any(norm(t) in known for t in specific):
        return 1.0
    # "Java Developer" is absence of proof, not proof of absence: still engineering, just not your stack.
    return ROLE_FIT_FAMILY if words & FAMILY_WORDS else 0.0


def _company_tokens(name: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(name.lower()) if t not in COMPANY_NOISE]


def excluded_title_word(title: str, words: list[str]) -> str | None:
    """The first `exclude_title_words` entry found in the title, if any."""
    return next((w for w in words if w.strip() and find_term(title, w.strip())), None)


def blacklisted_company(company: str, blacklist: list[str]) -> str | None:
    """The first `blacklist_companies` entry that names this company, if any.

    Case, punctuation and "Pvt Ltd"-style suffixes are ignored, so "Acme" blocks "Acme Technologies Pvt. Ltd.";
    matching is on whole tokens in order, so it never blocks "Acmetron Labs".
    """
    have = _company_tokens(company)
    for entry in blacklist:
        want = _company_tokens(entry)
        if not want:
            continue  # a blank or suffix-only entry would otherwise blacklist every company there is
        if any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1)):
            return entry
    return None


def skill_weight(req: SkillRequirement, title: str, description: str) -> float:
    """How central a requirement is: named in the title or repeated in the text weighs more than a single mention."""
    terms = (req.skill, *req.alternatives)
    in_title = any(find_term(title, t) for t in terms)
    mentions = sum(find_term(description, t) for t in terms)
    return 1 + TITLE_BONUS * in_title + REPEAT_BONUS * min(max(mentions - 1, 0), MAX_REPEATS)


def card_evidence(posting: JobPosting, bank: SkillBank) -> float:
    """0-1 skill evidence a search card gives: the portal's own tags, else the preview text it shows."""
    if tags := [t for t in posting.tags if t.strip()]:
        # A tag list is the recruiter's complete skill list, so a tag you don't have is real negative evidence.
        return sum(bank.match(t) is not None for t in tags) / len(tags)
    if hits := len(bank.skills_in(posting.snippet)):
        # A snippet is a truncated preview, so it can only raise a card, never sink one: absence proves nothing.
        return CARD_NEUTRAL + (1 - CARD_NEUTRAL) * min(1.0, hits / SNIPPET_FULL_CREDIT)
    return CARD_NEUTRAL


def prescore(posting: JobPosting, profile: Profile, bank: SkillBank) -> float:
    """Cheap 0-1 relevance from a search card (title, plus tags or snippet), so the best pages are opened first."""
    return 0.5 * role_fit(posting.title, posting.company, profile) + 0.5 * card_evidence(posting, bank)


def score_job(profile: Profile, analysis: JDAnalysis, bank: SkillBank, weights: ScoreWeights,
              posting: JobPosting | None = None) -> MatchResult:
    title = posting.title if posting else analysis.role_title
    description = posting.description if posting else ""
    # A JD that lists only "nice to have" skills: those are the only stated requirements, so judge on them.
    reqs = (analysis.mandatory_skills
            or [SkillRequirement(skill=s, alternatives=[]) for s in analysis.nice_to_have_skills])
    nice_terms = analysis.nice_to_have_skills if analysis.mandatory_skills else []
    if not reqs:
        return MatchResult(score=0, matched_mandatory=[], missing_mandatory=[], matched_nice=[], experience_ok=False,
                           explanation="0%: no skills could be read from the job description")

    matched, missing, short = [], [], []
    got = total = 0.0
    years = {s.name: s.years for s in profile.skills}
    for req in reqs:
        w = skill_weight(req, title, description)
        total += w
        hit = next((m for t in (req.skill, *req.alternatives) if (m := bank.match(t))), None)
        if not hit:
            missing.append(req.skill)
            continue
        skill, via_eq = hit
        matched.append(f"{skill} (for {req.skill})" if via_eq or skill.lower() != req.skill.lower() else skill)
        credit = 1.0
        if req.min_years is not None and (have := years.get(skill)) is not None:
            credit = experience_fit(have, req.min_years, None)
            if credit < 1:
                short.append(f"{skill} {have:g}y vs {req.min_years:g}y")
        got += w * credit

    nice = [m[0] for t in nice_terms if (m := bank.match(t))]
    lo, hi = analysis.min_experience_years, analysis.max_experience_years
    exp_ratio = experience_fit(profile.total_experience_years, lo, hi)

    # Only parts the JD actually gives information about count; a missing part is not a free 100%.
    parts = [(weights.mandatory_skills, got / total)]
    if nice_terms:
        parts.append((weights.nice_to_have_skills, len(nice) / len(nice_terms)))
    if lo is not None or hi is not None:
        parts.append((weights.experience, exp_ratio))
    weight_sum = sum(w for w, _ in parts)
    base = sum(w * r for w, r in parts) / weight_sum if weight_sum else 0.0
    fit = role_fit(title, posting.company if posting else "", profile)
    n_skills = len(reqs) + len(nice_terms)
    confidence = min(1.0, (n_skills + 1) / (FULL_CONFIDENCE_SKILLS + 1))
    score = round(100 * base * (weights.role_floor + (1 - weights.role_floor) * fit) * confidence)

    notes = []
    if missing:
        notes.append("missing " + ", ".join(missing))
    if matched:
        notes.append("strong in " + ", ".join(matched[:6]))
    if short:
        notes.append("fewer years in " + ", ".join(short))
    if exp_ratio < 1:
        notes.append(f"experience {profile.total_experience_years:g}y vs required {lo:g}y+" if lo is not None
                     else "may be over-qualified")
    if fit == 0:
        notes.append(f"title '{title}' is outside your roles")
    elif fit < 1:
        notes.append(f"title '{title}' is an engineering role outside your stack")
    if confidence < 1:
        notes.append(f"only {n_skills} skill(s) listed, score lowered")
    return MatchResult(
        score=score,
        matched_mandatory=matched,
        missing_mandatory=missing,
        matched_nice=nice,
        experience_ok=exp_ratio >= 1,
        role_fit=fit,
        explanation=f"{score}%: " + "; ".join(notes) if notes else f"{score}%",
    )
