#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 David D. Karnowski
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-push privacy sweep — refuse to publish personal or machine-specific data.

Scans every git-tracked file (and, with --log, recent commit messages; with
--history, every blob that has ever existed in this repository) for things
that should not leave this machine: absolute home paths, usernames, e-mail
addresses, adapter/device identifiers, IPs, keys, session URLs — and any
committed trouble-code dictionary (the project ships the format and none of
the data; see docs/DTC_DICTIONARY.md).

  ./venv/bin/python scripts/privacy_sweep.py            # tracked files
  ./venv/bin/python scripts/privacy_sweep.py --log 50   # + last 50 commit messages
  ./venv/bin/python scripts/privacy_sweep.py --history  # + every blob in history
  ./venv/bin/python scripts/privacy_sweep.py --strict   # warnings also fail

Exit 1 on any ERROR (or on WARN with --strict). A line meant to be public
carries a marker: a bare  privacy-ok  silences WARN rules on that line only;
silencing an ERROR rule needs its label, e.g.  privacy-ok:e-mail  for the
security contact in SECURITY.md (labels: comma-separated). Use sparingly.

Beyond the line rules it also refuses, by path: any tracked file under
research/ (the private folder), and any SQLite database or *.db/*.sqlite file;
it scans file paths themselves; and it decodes base64-looking tokens and
scans what they hide with the ERROR rules. --push reads the refs a git
pre-push hook receives on stdin and scans exactly the commits being pushed —
their messages, every added line and every added path — so a leak that was
committed and then removed from the working tree is still caught.

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
HEX = "[0-9A-Fa-f]"
RULES = [
    ("ERROR", "home path",        re.compile(r"(/Users/[A-Za-z0-9_.-]+|/home/[A-Za-z0-9_.-]+|[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_.-]+)")),
    ("ERROR", "secret-looking",   re.compile(r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-proj-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}"
                                             r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}"
                                             r"|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
    ("ERROR", "claude session",   re.compile(r"claude\.ai/(code/session_[A-Za-z0-9]+|(chat|share|project)/[0-9A-Fa-f-]{8,})")),
    # macOS (CoreBluetooth) device identifiers are upper-case; the lower-case
    # form is how public GATT service/characteristic UUIDs are written, so it
    # is a WARN, and the Bluetooth base UUID (0000xxxx-0000-1000-8000-00805f9b34fb)
    # is not flagged at all.
    ("ERROR", "device UUID",      re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b")),
    ("WARN",  "uuid (lower)",     re.compile(r"\b(?!0000[0-9a-f]{4}-0000-1000-8000-00805f9b34fb)"
                                             r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("ERROR", "BLE/MAC address",  re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")),
    # A VIN always carries letters (the manufacturer code alone is alphabetic), so a
    # run of 17 digits is not one — it is a float literal like 0.15915494309189535,
    # which vendored three.js has four of. The lookarounds treat '_' as a boundary
    # (a VIN inside a file name like JN1..._drive.jsonl) without matching inside
    # longer alphanumeric runs.
    ("ERROR", "VIN",              re.compile(r"(?<![A-Za-z0-9])(?![0-9]{17}(?![A-Za-z0-9]))[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])")),
    # bounded to an address's real limits (64 before the @, 253 after): the
    # unbounded form was quadratic on a long line with no @ in it
    ("ERROR", "e-mail",           re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")),
    ("ERROR", "IPv4",             re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # a serial-port name that ends in a hardware serial number identifies the
    # adapter; a short generic one (usbserial-0001, -XXX) does not
    ("ERROR", "serial port",      re.compile(r"/dev/(?:tty|cu)\.(?:usbserial-|usbmodem|wchusbserial|SLAB_USBtoUART)[A-Za-z0-9]{6,}")),
    ("WARN",  "serial port",      re.compile(r"/dev/(?:tty|cu)\.(?:usbserial-|usbmodem|wchusbserial)[A-Za-z0-9]{1,5}\b")),
    ("WARN",  "username",         re.compile(r"\b(dk|kn6irv|hustleyourcity)\b")),
    ("WARN",  "phone number",     re.compile(r"\b\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
    ("WARN",  "secret assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"\s]{8,64}['\"]")),
]
# Matches that are public by construction, per rule.
ALLOW = {
    "e-mail": re.compile(r"^(noreply@anthropic\.com|[^@]+@(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|org|net)|example))$", re.I),
    "IPv4": re.compile(r"^(127\.\d+\.\d+\.\d+|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+)$"),
}
# Not scanned line by line. research/ is the private, gitignored folder: a
# tracked file there is itself the finding (see scan_paths), and the history
# scan keeps skipping it (the folder was once in the repo; history is reviewed
# by hand).
SKIP_DIRS = ("venv/", ".venv/", "research/")
PRIVATE_DIRS = ("research/",)
DB_EXT = (".db", ".sqlite", ".sqlite3")
SQLITE_MAGIC = b"SQLite format 3\x00"

# Trouble-code dictionaries are never committed. The project ships the format
# and none of the data (docs/DTC_DICTIONARY.md): description text is
# licence-sensitive, and a dictionary built from a service manual is not the
# builder's to redistribute. `dtc/` is gitignored, but an ignore rule is a
# habit — `git add -f`, a path outside dtc/, or a rename all defeat it — so the
# sweep looks at the *content* of tracked JSON as well as at the path. The one
# dictionary that may be tracked is the synthetic sample the loader tests
# against, whose codes and descriptions are invented.
DTC_ALLOW = ("tests/fixtures/dtc_sample.json",)
DTC_DIR = "dtc/"

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


def looks_like_dtc_dictionary(text):
    """Is this text a trouble-code dictionary in the loader's format?

    Cheap substring filter first, then a real parse: a file that merely
    mentions "codes" (this script, docs, the Lancer profile) must not trip.
    """
    if '"codes"' not in text or '"evidence"' not in text:
        return False
    try:
        import json as _json
        data = _json.loads(text)
    except Exception:
        return False
    rows = data.get("codes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return False
    return any(isinstance(r, dict) and {"code", "desc", "evidence"} <= set(r) for r in rows)


def scan_dtc(path, text, findings):
    """ERROR on a tracked trouble-code dictionary — by path or by content."""
    if path in DTC_ALLOW:
        return
    if path.startswith(DTC_DIR) or looks_like_dtc_dictionary(text):
        findings.append(("ERROR", "dtc dictionary", path,
                         "trouble-code dictionaries stay machine-local (dtc/, gitignored) "
                         "— see docs/DTC_DICTIONARY.md"))


MARKER = re.compile(r"privacy-ok(?::([A-Za-z0-9 ,/()-]+))?")


def _allowed(line):
    """(silence_warn, labels) from a privacy-ok marker on the line."""
    m = MARKER.search(line)
    if not m:
        return False, set()
    # trailing spaces/dashes belong to the comment syntax around it (<!-- … -->)
    return True, {x.strip(" -") for x in (m.group(1) or "").split(",") if x.strip(" -")}


def _hit(sev, name, rx, line):
    """The first match of `rx` in `line` that the per-rule allow list does not cover."""
    for m in rx.finditer(line):
        allow = ALLOW.get(name)
        if allow is None or not allow.match(m.group(0)):
            return m
    return None


def scan_text(label, text, findings, rules=None, skip=()):
    for n, line in enumerate(text.splitlines(), 1):
        marked, labels = _allowed(line)
        for sev, name, rx in rules or RULES:
            if name in skip or name in labels or (marked and sev == "WARN"):
                continue
            if _hit(sev, name, rx, line):
                findings.append((sev, name, f"{label}:{n}", line.strip()[:110]))
    scan_encoded(label, text, findings)


# ---------------------------------------------------- encoded secrets ----
# A secret can be committed base64-encoded (a config blob, a data: URL, a
# token pasted from a header). Bounded on purpose: at most MAX_TOKENS tokens
# per text and MAX_DECODED bytes decoded in total, and only bounded
# repetition, so a hostile file cannot make the sweep slow.
B64 = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,4096}={0,2}(?![A-Za-z0-9+/_=-])")
MAX_TOKENS, MAX_DECODED = 40, 64 * 1024


def _decode(token):
    import base64
    import binascii
    t = token.rstrip("=")
    t += "=" * (-len(t) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = dec(t)
        except (binascii.Error, ValueError):
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text and sum(c.isprintable() or c in "\r\n\t" for c in text) >= 0.95 * len(text):
            return text
    return None


def scan_encoded(label, text, findings):
    budget, n_tok = MAX_DECODED, 0
    errors = [r for r in RULES if r[0] == "ERROR"]
    for n, line in enumerate(text.splitlines(), 1):
        if n_tok >= MAX_TOKENS or budget <= 0:
            return
        marked, labels = _allowed(line)
        for m in B64.finditer(line):
            n_tok += 1
            if n_tok > MAX_TOKENS or budget <= 0:
                return
            decoded = _decode(m.group(0))
            if not decoded:
                continue
            budget -= len(decoded)
            for sev, name, rx in errors:
                if name not in labels and _hit(sev, name, rx, decoded):
                    findings.append((sev, name + " (base64)", f"{label}:{n}", line.strip()[:110]))


def scan_paths(paths, findings, read_head=None):
    """Rules on the path names themselves; refuse private and database files.
    `read_head(path)` returns a file's first bytes (working tree or a commit)."""
    for p in paths:
        for sev, name, rx in RULES:
            if sev == "ERROR" and _hit(sev, name, rx, p):
                findings.append((sev, name + " (path)", p, p[:110]))
        if p.startswith(PRIVATE_DIRS):
            findings.append(("ERROR", "private folder", p, "research/ is the private folder — never committed"))
            continue
        head = b""
        if read_head is not None:
            try:
                head = read_head(p) or b""
            except Exception:
                head = b""
        if p.lower().endswith(DB_EXT) or head.startswith(SQLITE_MAGIC):
            findings.append(("ERROR", "database file", p, "databases hold readings — never committed"))


# ------------------------------------------------------------ push mode ---
# git runs the pre-push hook with one line per ref on stdin:
#   <local ref> <local sha> <remote ref> <remote sha>
# The working tree is not what is pushed — a leak committed and then deleted
# from the files (but not from history) would pass a tree scan. So the hook
# hands those lines here and every commit being pushed is scanned: its message,
# every line it adds and every path it adds or renames.
ZERO = "0" * 40


def pushed_commits(ref_lines):
    shas = []
    for line in ref_lines:
        parts = line.split()
        if len(parts) != 4:
            continue
        _lref, lsha, _rref, rsha = parts
        if lsha == ZERO:                       # a branch deletion pushes nothing
            continue
        rng = [lsha, "--not", "--remotes"] if rsha == ZERO else [f"{rsha}..{lsha}"]
        for sha in git("rev-list", *rng).split():
            if sha not in shas:
                shas.append(sha)
    return shas


def _blob_head(sha, path, n=16):
    proc = subprocess.Popen(["git", "cat-file", "-p", f"{sha}:{path}"], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return proc.stdout.read(n)
    finally:
        proc.kill()
        proc.wait()


def scan_push(ref_lines, findings):
    """Scan every commit the push would publish. Returns the commits scanned."""
    shas = pushed_commits(ref_lines)
    for sha in shas:
        short = sha[:8]
        scan_text(f"commit {short} message", git("show", "-s", "--format=%B", sha), findings, skip=LOG_SKIP)
        added, cur = {}, None
        for line in git("show", "--format=", "-U0", "--no-color", "--no-ext-diff", sha).splitlines():
            if line.startswith("+++ "):
                cur = line[6:] if line.startswith("+++ b/") else None
            elif line.startswith("+") and cur is not None:
                added.setdefault(cur, []).append(line[1:])
        for path, lines in added.items():
            if path in SKIP_FILES or path.endswith(BINARY) or any(path.startswith(d) for d in SKIP_DIRS):
                continue
            scan_text(f"commit {short} {path} (+)", "\n".join(lines), findings)
            scan_dtc(path, "\n".join(lines), findings)
        paths = [p for p in git("show", "--format=", "--name-only", "--diff-filter=AR", sha).splitlines() if p]
        scan_paths(paths, findings, lambda p, sha=sha: _blob_head(sha, p))
    return shas


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
    ap.add_argument("--push", action="store_true",
                    help="also scan the commits being pushed (reads the pre-push hook's ref lines on stdin)")
    args = ap.parse_args()

    findings = []

    def head(p):
        with open(os.path.join(ROOT, p), "rb") as fh:
            return fh.read(16)
    scan_paths(tracked_files(), findings, head)
    for f in tracked_files():
        if f.endswith(BINARY) or any(f.startswith(d) for d in SKIP_DIRS) or f in SKIP_FILES:
            continue
        try:
            with open(os.path.join(ROOT, f), encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            scan_text(f, body, findings)
            scan_dtc(f, body, findings)
        except OSError:
            continue
    if args.log:
        log = git("log", f"-{args.log}", "--format=%H%n%B")
        scan_text("git-log", log, findings, skip=LOG_SKIP)   # see LOG_SKIP for why these two

    pushed = None
    if args.push:
        ref_lines = [] if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read().splitlines()
        pushed = scan_push(ref_lines, findings)

    errors = [x for x in findings if x[0] == "ERROR"]
    warns = [x for x in findings if x[0] == "WARN"]
    for sev, name, where, line in sorted(findings, key=lambda x: (x[0] != "ERROR", x[2])):
        print(f"{sev:5} {name:15} {where}\n      {line}")
    print(f"\n{len(errors)} error(s), {len(warns)} warning(s) across {len(tracked_files())} tracked files"
          + (f" and {len(pushed)} commit(s) being pushed" if pushed is not None else ""))

    herr, hwarn = [], []
    if args.history:
        rows, images, head_blobs = scan_history()
        herr, hwarn = report_history(rows, images, head_blobs)

    if errors or herr or (args.strict and (warns or hwarn)):
        print("privacy sweep FAILED — fix it, move the material to research/, or mark a line meant to be "
              "public with privacy-ok (WARN) or privacy-ok:<rule> (ERROR)")
        return 1
    print("privacy sweep OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
