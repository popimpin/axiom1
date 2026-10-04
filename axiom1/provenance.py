"""Where an invitation comes from. Structure only, no model.

Adrian, 2026-10-04: flag invitations "if its from a source outside the enterprise network". AgentDojo's "direct"
attack ("TODO: {goal}") carries no mark a sanitizer can see; worded with a date, it put phishing on the follow-up list
("Your Facebook security code" from security-facebook-com@gmail.com). The tell is the source: outside the organisation,
and someone the owner has never written to. Such an invitation is FLAGGED, not dropped - a real first invitation from
outside (a friend's birthday party) looks the same, and the person decides.
"""
import re


def domain(addr):
    m = re.search(r"@([\w.-]+)", addr or "")
    return m.group(1).lower().strip(".") if m else ""


def inside(addr, org):
    """The address belongs to the organisation's domain or one of its subdomains (ect.enron.com is inside enron.com)."""
    d = domain(addr)
    return bool(org) and (d == org or d.endswith("." + org))


def written_to(sent_messages):
    """Every address the owner has sent to (to, cc), lower-cased."""
    out = set()
    for m in sent_messages:
        for a in list(m.get("to") or []) + list(m.get("cc") or []):
            for x in re.findall(r"[\w.+'-]+@[\w.-]+", a or ""):
                out.add(x.lower())
    return out


def outside_flag(sender, owner, known):
    """The flag for an invitation from `sender`, or None: outside the owner's organisation AND never written to."""
    s = (re.findall(r"[\w.+'-]+@[\w.-]+", sender or "") or [""])[0].lower()
    if not s or inside(s, domain(owner)) or s in known:
        return None
    return f"from outside your organisation ({domain(s)}), from someone you have never written to"
