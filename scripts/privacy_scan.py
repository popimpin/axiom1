import os
import re
import sys

def run_privacy_scan(repo_dir):
    print(f"Starting privacy scan in: {repo_dir}")
    
    # Patterns to flag
    forbidden_patterns = [
        ("LOCAL_PATH_USERS", re.compile(r"c:[\/\\]users[\/\\][^\s\"']+", re.IGNORECASE)),
        ("LOCAL_PATH_PROJECTSFOLDER", re.compile(r"projectsfolder", re.IGNORECASE)),
        ("LOCAL_USERNAME", re.compile(r"[\/\\]Users[\/\\]adria", re.IGNORECASE)),
        ("FANTA_KEY_FILE", re.compile(r"fanta\.txt", re.IGNORECASE)),
        ("RAW_AXIOM_TOKEN", re.compile(r"axm_[a-zA-Z0-9_\-]{20,}")),
        ("OPENAI_KEY", re.compile(r"sk-[a-zA-Z0-9_\-]{20,}")),
        ("GENERIC_BEARER", re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]{25,}")),
    ]

    scanned_count = 0
    issues = []
    
    for root, dirs, files in os.walk(repo_dir):
        # Exclude git directory and internal git worktree files
        if ".git" in root.split(os.sep):
            continue
        for f in files:
            if f == ".git":
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in [".woff2", ".png", ".jpg", ".jpeg", ".ico", ".db", ".pyc"]:
                continue
                
            file_path = os.path.join(root, f)
            rel_path = os.path.relpath(file_path, repo_dir)
            if rel_path == os.path.normpath("scripts/privacy_scan.py"):
                continue
                
            scanned_count += 1
            
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                    
                for name, pat in forbidden_patterns:
                    matches = pat.findall(content)
                    if matches:
                        issues.append({
                            "file": rel_path,
                            "rule": name,
                            "matches": matches[:5]
                        })
                        
                # Check for IPs in published web files (index.html, claims.html, data/)
                if rel_path in ["index.html", "claims.html"] or rel_path.startswith("data" + os.sep):
                    ip_pat = re.compile(r"\b(?!0\.0\.0\.0)(?!255\.255\.255\.255)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
                    ip_matches = ip_pat.findall(content)
                    if ip_matches:
                        issues.append({
                            "file": rel_path,
                            "rule": "IP_IN_PUBLISHED_PAGE",
                            "matches": ip_matches
                        })
            except Exception as e:
                print(f"Error scanning {rel_path}: {e}")

    print(f"Scanned {scanned_count} files.")
    if issues:
        print(f"FAILED: Found {len(issues)} potential privacy issues:")
        for iss in issues:
            print(f"  - {iss['file']} [{iss['rule']}]: {iss['matches']}")
        return False
    else:
        print("PASSED: 0 API keys/tokens, 0 local filesystem paths, 0 disallowed IPs, 0 leaks.")
        return True

if __name__ == "__main__":
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    success = run_privacy_scan(base)
    sys.exit(0 if success else 1)
