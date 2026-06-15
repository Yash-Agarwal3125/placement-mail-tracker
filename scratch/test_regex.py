import re

subject = "Bazaarvoice PPT & online test is scheduled"

_STATUS_PATTERNS = [
    ("REJECTED", re.compile(
        r"(not\s*shortlisted|not\s*selected|regret\s*to|"
        r"unfortunately|rejected|could\s*not\s*make)",
        re.IGNORECASE,
    )),
    ("OFFER_RECEIVED", re.compile(
        r"(offer\s*(letter|released)|final\s*selection\s*result|"
        r"congratulations.*selected|selected\s*candidates?\s*list)",
        re.IGNORECASE,
    )),
    ("SELECTED", re.compile(
        r"(finally?\s*selected|selection\s*list|selected\s*for\s*joining)",
        re.IGNORECASE,
    )),
    ("HR", re.compile(
        r"(hr\s*(round|interview|discussion)|"
        r"human\s*resource\s*(round|interview))",
        re.IGNORECASE,
    )),
    ("SHORTLISTED", re.compile(
        r"(shortlist|short[\-\s]list|shortlisted\s*students?|"
        r"selected\s*for\s*(next|further|interview)|qualified)",
        re.IGNORECASE,
    )),
    ("INTERVIEW", re.compile(
        r"(interview\s*(scheduled|process|round|date)|"
        r"next\s*round.*selection|technical\s*interview|"
        r"gd.*round|group\s*discussion)",
        re.IGNORECASE,
    )),
    ("OA", re.compile(
        r"(online\s*(assessment|test)|oa\s*(scheduled|date|link)|"
        r"hackerrank|coding\s*test|assessment\s*scheduled|"
        r"aptitude\s*test)",
        re.IGNORECASE,
    )),
    ("REGISTERED", re.compile(
        r"(registration\s*(successful|confirmed|complete)|"
        r"successfully\s*registered|applied\s*successfully)",
        re.IGNORECASE,
    )),
]

for status, pattern in _STATUS_PATTERNS:
    if pattern.search(subject):
        print(f"Matched {status}!")
        break
else:
    print("No match")
