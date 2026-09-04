#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-push privacy sweep — refuse to publish personal or machine-specific data.

Scans every git-tracked file (and, with --log, recent commit messages; with
--history, every blob that has ever existed in this repository) for things
that should not leave this machine: absolute home paths, usernames, e-mail
addresses, adapter/device identifiers, IPs, keys, session URLs.

  ./venv/bin/python scripts/privacy_sweep.py            # tracked files
  ./venv/bin/python scripts/privacy_sweep.py --log 50   # + last 50 commit messages
  ./venv/bin/python scripts/privacy_sweep.py --history  # + every blob in history
  ./venv/bin/python scripts/privacy_sweep.py --strict   # warnings also fail

Exit 1 on any ERROR (or on WARN with --strict). Allow-listed lines carry the
marker  privacy-ok  in the same line (use sparingly, e.g. the contact e-mail
in SECURITY.md).

Why --history exists: a working tree can be spotless while the history still
carries the leak. That happened here — an adapter UUID was committed and
scrubbed eight minutes later, and unblurred screenshots were replaced by
blurred ones thirteen minutes later. Both stayed reachable in history for
months because this tool only ever looked at `git ls-files`. History hits
cannot be fixed by editing a file; see the note the report prints.
"""
import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (severity, label, regex)
RULES = [
    ("ERROR", "home path",        re.compile(r"(/Users/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+|C:\\\\Users\\\\)")),
    ("ERROR", "secret-looking",   re.compile(r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
    ("ERROR", "claude session",   re.compile(r"claude\.ai/code/session_[A-Za-z0-9]+")),
    ("ERROR", "device UUID",      re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b")),
    ("ERROR", "VIN",              re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")),
    ("WARN",  "e-mail",           re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("WARN",  "IPv4",             re.compile(r"\b(?!127\.0\.0\.1)(?!0\.0\.0\.0)(\d{1,3}\.){3}\d{1,3}\b")),
    ("WARN",  "username",         re.compile(r"\b(dk|kn6irv|hustleyourcity)\b")),
    ("WARN",  "serial port",      re.compile(r"/dev/(tty|cu)\.usbserial-[A-Za-z0-9]+")),
    ("WARN",  "phone number",     re.compile(r"\b\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
]
SKIP_DIRS = ("venv/", ".venv/", "research/")

# This tool's own test corpus. test_privacy_sweep.py must contain strings that
# look exactly like the things we hunt for — that is how it proves the rules
# fire — so scanning it guarantees a false positive on every rule at once.
# The fixtures there are synthetic by construction (the sample adapter UUID is
# deliberately one character off from any real one). Keep this list to exactly
# this file: a general "skip tests" rule would be a hole big enough to hide a
# real leak in.
SKIP_FILES = ("tests/test_privacy_sweep.py",)
BINARY = (".png", ".jpg", ".jpeg", ".gif", ".db", ".dmg", ".pdf", ".ico")
IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic")

# Rules skipped when scanning *commit messages* (not file contents).
#   e-mail / username: git records the maintainer's own name and address as
#   author and committer on every single commit. Flagging them in the log
#   would mean 100% noise with no action available short of rewriting every
#   commit — and public authorship is the point of a public repo.
#   The "claude session" rule is deliberately NOT skipped: assistant session
#   URLs in trailers are private links and are exactly what we want to catch.
LOG_SKIP = ("e-mail", "username")

MAX_BLOB = 2 * 1024 * 1024   # do not scan blobs larger than this


def git(*args, **kw):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, **kw).stdout


def tracked_files():
    return [f for f in git("ls-files").splitlines() if f]


def scan_text(label, text, findings):
    for n, line in enumerate(text.splitlines(), 1):
        if "privacy-ok" in line:
            continue
        for sev, name, rx in RULES:
            m = rx.search(line)
            if m:
                findings.append((sev, name, f"{label}:{n}", line.strip()[:110]))


# ---------------------------------------------------------------- history ---

def history_objects():
    """[(sha, path)] for every blob ever reachable from any ref.

    `git rev-list --all --objects` lists each object once; blobs carry the
    path they were last seen under. A single `cat-file --batch-check` run
    tells us which of those are blobs and how big they are — far cheaper
    than one `git show` per object.
    """
    lines = git("rev-list", "--all", "--objects").splitlines()
    cand = {}
    for line in lines:
        sha, _, path = line.partition(" ")
        if path:
            cand[sha] = path
    if not cand:
        return []
    check = subprocess.run(["git", "cat-file", "--batch-check"], cwd=ROOT, text=True,
                           input="\n".join(cand) + "\n", capture_output=True).stdout
    out = []
    for line in check.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            out.append((parts[0], cand[parts[0]], int(parts[2])))
    return out


def blob_commit(sha, _cache={}):
    """'<short-sha> <date>' of the oldest commit that carries this blob.

    `git log --find-object` lists newest first, so the last line is the commit
    that introduced the content. (--reverse together with --max-count returns
    nothing, hence the full list.)
    """
    if sha not in _cache:
        out = git("log", "--all", "--find-object=" + sha,
                  "--format=%h %ad", "--date=short").splitlines()
        _cache[sha] = out[-1] if out else "unreachable?"
    return _cache[sha]


def scan_history(strict=False):
    """Scan every historical blob. Returns (rows, image_warnings)."""
    at_head = set(tracked_files())
    objs = [(s, p, n) for s, p, n in history_objects()
            if not any(p.startswith(d) for d in SKIP_DIRS) and p not in SKIP_FILES]

    # ---- text blobs -------------------------------------------------------
    text_objs = [(s, p, n) for s, p, n in objs if not p.endswith(BINARY) and n <= MAX_BLOB]
    rows = []
    if text_objs:
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=ROOT,
                              input=("\n".join(s for s, _, _ in text_objs) + "\n").encode(),
                              capture_output=True)
        buf, pos = proc.stdout, 0
        paths = {s: p for s, p, _ in text_objs}
        while pos < len(buf):
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            header = buf[pos:nl].decode("utf-8", "replace").split()
            pos = nl + 1
            if len(header) < 3 or header[1] != "blob":
                break
            sha, size = header[0], int(header[2])
            body, pos = buf[pos:pos + size], pos + size + 1
            if b"\x00" in body[:8000]:
                continue                            # binary in disguise
            hits = []
            scan_text(paths.get(sha, "?"), body.decode("utf-8", "replace"), hits)
            for sev, name, where, line in hits:
                rows.append((sev, name, sha, paths.get(sha, "?"),
                             where.rsplit(":", 1)[-1], line))

    # ---- image blobs: same path at HEAD, different bytes in history -------
    # This is the unblurred-screenshot case. A WARN, never an ERROR: editing
    # an image legitimately produces exactly this signature.
    by_path = {}
    for sha, path, _ in objs:
        if path.endswith(IMAGE):
            by_path.setdefault(path, set()).add(sha)
    head_blobs = {}
    for line in git("ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        bits = meta.split()
        if len(bits) >= 3:
            head_blobs[path] = bits[2]
    images = []
    for path, shas in sorted(by_path.items()):
        if path in at_head and len(shas) > 1:
            old = sorted(s for s in shas if s != head_blobs.get(path))
            images.append((path, len(shas), old))
    return rows, images, head_blobs


def report_history(rows, images, head_blobs):
    at_head = set(tracked_files())
    print("\n--- history scan (every blob ever committed) ---")
    if not rows and not images:
        print("no findings in history")
        return [], []

    # One line of one file may appear in dozens of historical versions of that
    # file. Collapse by (rule, path, offending text) and report the commit that
    # first introduced it plus how many blob versions carry it.
    groups = {}
    for sev, name, sha, path, ln, line in rows:
        g = groups.setdefault((sev, name, path, line), {"shas": set(), "ln": ln})
        g["shas"].add(sha)
    for (sev, name, path, line), g in sorted(
            groups.items(), key=lambda kv: (kv[0][0] != "ERROR", kv[0][1], kv[0][2])):
        first = min((blob_commit(s) for s in g["shas"]), key=lambda t: (t.split()[-1], t))
        # Three different situations, three different fixes:
        if head_blobs.get(path) in g["shas"]:
            where = "STILL AT HEAD — fix the file"
        elif path in at_head:
            where = "history only, path still tracked"
        else:
            where = "history only, path gone"
        vers = f", {len(g['shas'])} blob versions" if len(g["shas"]) > 1 else ""
        print(f"{sev:5} {name:15} {first}  {path}:{g['ln']}  [{where}{vers}]\n      {line}")
    for path, n, old in images:
        print(f"WARN  image history   {path}  [STILL AT HEAD]\n"
              f"      {n} distinct versions in history; older blob(s): {', '.join(s[:10] for s in old)}\n"
              f"      an image replaced after committing (e.g. blurring a screenshot) still "
              f"exposes the original blob")
    herr = [k for k in groups if k[0] == "ERROR"]
    hwarn = [k for k in groups if k[0] == "WARN"]
    print(f"\nhistory: {len(herr)} distinct error(s), {len(hwarn)} distinct warning(s), "
          f"{len(images)} changed-image warning(s) across {len(rows)} raw hits")
    print("NOTE: history findings CANNOT be fixed by editing files. The bytes stay reachable")
    print("      in .git until the history is rewritten (git filter-repo / a fresh squashed")
    print("      initial commit) and every stale clone, fork and remote ref is replaced.")
    print("      Anything that was ever pushed should also be treated as compromised and rotated.")
    return herr, hwarn


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=int, default=0, help="also scan the last N commit messages")
    ap.add_argument("--history", action="store_true",
                    help="also scan every blob in the repository's history")
    ap.add_argument("--strict", action="store_true", help="treat WARN as failure")
    args = ap.parse_args()

    findings = []
    for f in tracked_files():
        if f.endswith(BINARY) or any(f.startswith(d) for d in SKIP_DIRS) or f in SKIP_FILES:
            continue
        try:
            with open(os.path.join(ROOT, f), encoding="utf-8", errors="replace") as fh:
                scan_text(f, fh.read(), findings)
        except OSError:
            continue
    if args.log:
        log = git("log", f"-{args.log}", "--format=%H%n%B")
        for sev, name, rx in RULES:
            if name in LOG_SKIP:            # see LOG_SKIP for why these two only
                continue
            for n, line in enumerate(log.splitlines(), 1):
                if rx.search(line) and "privacy-ok" not in line:
                    findings.append((sev, name, f"git-log:{n}", line.strip()[:110]))

    errors = [x for x in findings if x[0] == "ERROR"]
    warns = [x for x in findings if x[0] == "WARN"]
    for sev, name, where, line in sorted(findings, key=lambda x: (x[0] != "ERROR", x[2])):
        print(f"{sev:5} {name:15} {where}\n      {line}")
    print(f"\n{len(errors)} error(s), {len(warns)} warning(s) across {len(tracked_files())} tracked files")

    herr, hwarn = [], []
    if args.history:
        rows, images, head_blobs = scan_history()
        herr, hwarn = report_history(rows, images, head_blobs)

    if errors or herr or (args.strict and (warns or hwarn)):
        print("privacy sweep FAILED — fix, move to research/, or mark the line privacy-ok if it is meant to be public")
        return 1
    print("privacy sweep OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
