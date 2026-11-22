import re
import unicodedata

from accounts.nicknames import matching_text, nickname_rule_matches
from .models import ContentKeywordRule


# These are review hints, not final rejection reasons. Do not save phone numbers or links.
CONTACT_HINT = re.compile(r"(?<!\d)1[3-9](?:[\s.-]*\d){9}(?!\d)|(?:https?://|www\.)", re.IGNORECASE)


def screen(kind, snapshot):
    if kind not in ("nickname", "review"):
        return []
    text = " ".join(snapshot["text"].values())
    if not text.strip():
        return []
    findings = []
    if kind == "nickname" and nickname_rule_matches(text):
        findings.append({"label": "命中内置昵称词库"})
    if CONTACT_HINT.search(unicodedata.normalize("NFKC", text)):
        findings.append({"label": "疑似包含联系方式或链接"})
    normalized = matching_text(text)
    hits = set()
    for rule in ContentKeywordRule.objects.filter(enabled=True, scope__in=(kind, ContentKeywordRule.Scope.BOTH)):
        if rule.normalized_keyword and rule.normalized_keyword in normalized and rule.normalized_keyword not in hits:
            hits.add(rule.normalized_keyword)
            findings.append({"rule_id": rule.pk, "keyword": rule.keyword, "label": rule.label or "命中自定义关键词"})
            if len(hits) == 20:
                break
    return findings
