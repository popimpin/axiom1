"""Threads that need a person to look before anything is done with them. Structure only, no model.

Adrian, 2026-10-04: "we can also have a folder that explicitly needs human oversight, such as code being sent in text
via email as some teams may do this when away from the office." Instruction is one way only: nothing in a mailbox is
ever run or acted on - and some content (code, commands, database statements, encoded blobs, or text addressed to an
AI assistant) is exactly what a person must see with their own eyes. Such a thread keeps whatever else it is (filed,
a follow-up...) and ALSO goes in the "needs human oversight" folder, with the reason.

Tightened after a first pass over five Enron mailboxes: a bare ">" (email reply quoting) had matched ~300 threads
as a shell prompt, and a loose import pattern matched prose ("a note from creditors.", "from Enya.").
"""
import re

CHECKS = [
    ("code", re.compile(
        r"```[^`]{10,}```"                                     # a fenced block: a pair of ```
        r"|^\s*(?:def|class)\s+\w+\s*[(:]"                      # a definition
        r"|^\s*function\s+\w+\s*\("
        r"|^\s*import\s+[\w.]+\s*;?\s*$"                        # an import that is an import
        r"|^\s*from\s+[\w.]+\s+import\s+\w"
        r"|^\s*#include\s*<"
        r"|^\s*(?:public|private)\s+(?:static\s+)?\w+\s+\w+\s*\("
        r"|<script\b", re.M)),
    ("commands", re.compile(
        r"^\s*(?:\$|PS>|C:\\>)\s*(?:sudo|cd|ls|python3?|pip|git|curl|wget|npm|ssh|scp|chmod|export|echo|cat|grep|"
        r"docker|kubectl|dir|copy|del)\b"                        # a prompt followed by a real command
        r"|\bsudo\s+\S|\bchmod\s+[0-7+]|\brm\s+-rf\b"
        r"|\b(?:curl|wget)\s+\S+.*\|\s*(?:sh|bash)\b"
        r"|\bpowershell(?:\.exe)?\s+-|\bInvoke-(?:Expression|WebRequest)\b|\bcmd(?:\.exe)?\s+/c\b", re.M | re.I)),
    ("database statements", re.compile(
        r"\b(?:DROP\s+TABLE|DELETE\s+FROM|TRUNCATE\s+TABLE|INSERT\s+INTO|UPDATE\s+\w+\s+SET|ALTER\s+TABLE|GRANT\s+\w+\s+ON)\b")),
    ("encoded data", re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")),
]


# Attachments (Adrian, 2026-10-04: "files with attachments flagged for immediate review"). Enron kept them only as
# markers in the text ("<< File: X.doc >>", or a "- X.xls" line); a live connector passes them the same way. Seen in
# five Enron mailboxes: 1,168 threads with attachments, including 15 .exe files in one (State-of-the-artwatch.exe).
ATTACH_RE = re.compile(r"<<\s*File:\s*([^>]+?)\s*>>"
                       r"|^\s*-\s*([\w .()&'#-]+\.(?:xlsx?|docx?|pdf|pptx?|zip|txt|exe|com|bat|cmd|vbs|js|scr|msi|ps1|jar|"
                       r"docm|xlsm|pptm|msg|eml|html?|csv|rtf|tif|jpe?g|png|gif|dat))\s*$", re.I | re.M)
DANGEROUS = {"exe", "com", "bat", "cmd", "vbs", "js", "scr", "msi", "ps1", "jar", "docm", "xlsm", "pptm", "hta", "lnk"}


def attachments(bodies):
    """The attachments these messages name, in order, each once."""
    out = []
    for b in bodies:
        for a, c in ATTACH_RE.findall(b or ""):
            name = (a or c).strip()
            # a ".com" name with spaces is a sentence ending in a web address, not a DOS program (seen: the newsletter
            # line "- Spare plant parts become manageable with Sparesfinder.com" flagged as an executable)
            if name.lower().endswith(".com") and (" " in name or name.lower().startswith("www.")):
                continue
            if name and name not in out:
                out.append(name)
    return out


def reasons(bodies, ai_instructions=0):
    """Why these messages need a person, or []. `ai_instructions`: blocks the sanitizer took out. Dangerous
    attachments (executables, scripts, macro documents) come first: they are the ones to look at before anything."""
    text = "\n".join(bodies)
    out = []
    files = attachments(bodies)
    bad = [f for f in files if f.rsplit(".", 1)[-1].lower() in DANGEROUS]
    if bad:
        out.append("DANGEROUS attachment (executable, script or macro): " + ", ".join(bad))
    if files:
        out.append("attachments: " + ", ".join(files[:6]) + (f" and {len(files) - 6} more" if len(files) > 6 else ""))
    out += [name for name, rx in CHECKS if rx.search(text)]
    if ai_instructions:
        out.append("instructions addressed to an AI assistant (removed before any model read it)")
    return out
