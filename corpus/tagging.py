"""Local, auditable subject labels. Labels never modify source text."""
import re

VERSION = "subject-rules-v1"


def normalize_tags(values):
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("tags must be a list of at most 32 subject names")
    result = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("each tag must be a string")
        tag = re.sub(r"[\s_]+", "-", value.strip().lower())
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", tag):
            raise ValueError("tags must contain 1-64 letters, numbers, spaces or hyphens")
        result.add(tag)
    return sorted(result)


# A title match or repeated body evidence is required. Incidental mentions alone
# do not establish a subject. This taxonomy can grow without an LLM or re-extraction.
RULES = {
    "patent-law": r"\b(?:patent(?:s|ing|ability)?|mpep|ptab|35\s+u\.?s\.?c\.?)\b",
    "double-patenting": r"\bdouble[\s-]+patenting\b",
    "terminal-disclaimers": r"\bterminal[\s-]+disclaimer(?:s)?\b",
    "patent-prosecution": r"\b(?:mpep|patent examin(?:ing|ation)|patent prosecution|ex parte appeal(?:s)?)\b",
    "patent-appeals": r"\b(?:ptab|patent trial and appeal board|appeals review panel|ex parte baurin|ex parte xencor)\b",
    "space-law": r"\b(?:outer space|space law|space treaty)\b",
    "international-law": r"\b(?:treat(?:y|ies)|international law)\b",
    # CCPA alone can also mean the Court of Customs and Patent Appeals.
    "privacy-law": r"\b(?:consumer privacy act|privacy law|data protection)\b",
    "election-law": r"\b(?:redistricting|reapportionment|voting rights|election law)\b",
    "environmental-law": r"\b(?:clean air act|environmental protection agency|coal- and oil-fired|environmental law)\b",
    "criminal-law": r"\b(?:supervised release|criminal law|criminal sentence|imprisonment)\b",
    "administrative-law": r"\b(?:federal register|administrative procedure act|rulemaking)\b",
    "securities-law": r"\b(?:securities and exchange commission|form 8-k|material definitive agreement)\b",
    "semiconductor-research": r"\b(?:semiconductor(?:s)?|integrated circuits?|wafer fabrication|chip fabrication|photolithography)\b",
    "biotechnology-research": r"\b(?:binding proteins?|antibod(?:y|ies)|biotechnology|trispecific|trivalent)\b",
    "pharmaceutical-research": r"\b(?:pharmaceutical(?:s)?|drug development|drug formulation)\b",
    "artificial-intelligence-research": r"\b(?:artificial intelligence|machine learning|large language models?)\b",
    "publishing-resources": r"\b(?:media kit|advertising rates|editorial calendar)\b",
}


def infer_tags(title, text):
    text = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=\w)", "", text)
    result = {"research": {"reason": "source corpus membership", "version": VERSION}}
    for tag, pattern in RULES.items():
        title_match = re.search(pattern, title, re.I)
        first = None
        count = 0
        for match in re.finditer(pattern,text,re.I):
            count += 1
            if first is None:first=match.group(0)
        # Longer documents need stronger evidence to avoid classifying a topic
        # from a few references in a large compendium.
        threshold = max(3, min(12, len(text) // 30000 + 3))
        if title_match or count >= threshold:
            result[tag] = {"version": VERSION, "title_match": title_match.group(0) if title_match else None,
                           "body_matches": count, "sample": first}
    legal = [tag for tag in result if tag.endswith("-law") or tag.startswith("patent-") or tag in ("double-patenting", "terminal-disclaimers")]
    if legal or re.search(r"\b(?:court|amicus|amici|fed\. cir\.|u\.s\.c\.)", title, re.I):
        result["legal-research"] = {"version": VERSION, "reason": "legal subject or legal document title", "subjects": legal}
    return result
