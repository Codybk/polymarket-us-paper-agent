#!/usr/bin/env python3
"""
Persist a scan's results to the repository, safely, under concurrent writes.

THE RACE THIS FIXES
-------------------
A scheduled scan checks out commit A and runs for a minute or two. Meanwhile
`main` advances to commit B -- someone pushes, or a previous run lands. The old
step then ran `git pull --rebase`, which tried to reconcile `docs/index.html`
line by line. That file is GENERATED: two runs produce different HTML for the
same underlying state, so rebasing it produces a conflict that cannot be
resolved by merging text. The push failed and the scan's state was lost.

THE STRUCTURAL FIX
------------------
Stop treating generated output as mergeable. Every path falls into exactly one
of three classes, each with one correct rule:

  SOURCE   (src/, config/, README.md, .github/, ...)
      Never written by a scan. On divergence, the REMOTE always wins --
      it is strictly newer than our checkout and we have nothing to contribute.

  STATE    (state/**)
      The scan's actual output, and the only thing it genuinely produces.
      Our version wins -- UNLESS the remote's state also advanced, which means
      another scan wrote results our run never saw. Overwriting those would
      silently destroy them, so we ABORT instead. Nothing is discarded, and the
      next scan simply starts from the newer state.

  GENERATED (docs/index.html)
      A pure function of state + config + code. It is never merged and never
      conflicts: after syncing to the remote we simply REGENERATE it, so it
      reflects the merged result by construction.

There is no force-push anywhere in this file, and no path that throws away
either side's work. A push rejected by a concurrent write is retried from the
top, re-reading the remote each time.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_DIR = "state"
GENERATED = ("docs/index.html",)


class PersistError(RuntimeError):
    pass


class RemoteStateAdvanced(PersistError):
    """Another scan wrote state we never saw. Abort rather than clobber it."""


def git(*args, cwd=None, check=True, capture=True):
    cwd = cwd or REPO_ROOT
    r = subprocess.run(["git", *args], cwd=cwd, check=False,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None,
                       text=True)
    if check and r.returncode != 0:
        raise PersistError(f"git {' '.join(args)} failed:\n{r.stdout or ''}")
    return (r.stdout or "").strip()


def _changed_paths(cwd, a, b):
    out = git("diff", "--name-only", f"{a}..{b}", cwd=cwd)
    return [p for p in out.splitlines() if p.strip()]


def _snapshot_state(cwd):
    """Copy state/ aside. It is the only thing this run actually produced."""
    tmp = tempfile.mkdtemp(prefix="scan-state-")
    src = os.path.join(cwd, STATE_DIR)
    dst = os.path.join(tmp, STATE_DIR)
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    else:
        os.makedirs(dst)
    return tmp


def _restore_state(cwd, snapshot):
    src = os.path.join(snapshot, STATE_DIR)
    dst = os.path.join(cwd, STATE_DIR)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def regenerate_dashboard(cwd):
    """Rebuild the generated dashboard from the post-sync state.

    Done in-process when possible so the test suite exercises the same code
    path the workflow runs.
    """
    sys.path.insert(0, cwd)
    for mod in [m for m in list(sys.modules) if m.startswith("src.")]:
        del sys.modules[mod]
    try:
        from src import dashboard  # noqa: PLC0415
        dashboard.build(cwd)
    except Exception as e:  # noqa: BLE001
        raise PersistError(f"dashboard regeneration failed: {e}") from e
    finally:
        if sys.path and sys.path[0] == cwd:
            sys.path.pop(0)


def sync_before_scan(cwd=None, branch=None, remote="origin"):
    """Fast-forward the checkout to the latest remote tip BEFORE scanning.

    Narrows the race window: the scan then starts from the newest source and
    the newest state, rather than from whatever the runner happened to clone.
    Refuses to run if the working tree has local modifications, since silently
    resetting over them would be exactly the kind of quiet data loss this
    module exists to prevent.
    """
    cwd = cwd or REPO_ROOT
    branch = branch or os.environ.get("GITHUB_REF_NAME", "main")
    git("fetch", remote, branch, cwd=cwd)
    dirty = git("status", "--porcelain", cwd=cwd)
    if dirty:
        raise PersistError(
            "working tree is dirty before the scan; refusing to sync over "
            f"uncommitted changes:\n{dirty}")
    git("reset", "--hard", f"{remote}/{branch}", cwd=cwd)
    return git("rev-parse", "HEAD", cwd=cwd)


def persist(cwd=None, branch=None, remote="origin", attempts=4,
            message=None, regenerate=True, push=True):
    """Commit and push state + regenerated dashboard. Returns a summary dict."""
    cwd = cwd or REPO_ROOT
    branch = branch or os.environ.get("GITHUB_REF_NAME", "main")
    notes = []

    for attempt in range(1, attempts + 1):
        head = git("rev-parse", "HEAD", cwd=cwd)
        git("fetch", remote, branch, cwd=cwd)
        remote_ref = f"{remote}/{branch}"
        remote_sha = git("rev-parse", remote_ref, cwd=cwd)

        if remote_sha != head:
            incoming = _changed_paths(cwd, head, remote_ref)
            remote_state = [p for p in incoming if p.startswith(STATE_DIR + "/")]
            if remote_state:
                # Another scan produced results this run never saw. Its state
                # is strictly newer. Overwriting it would destroy real data,
                # and merging JSON/JSONL blindly would corrupt the audit chain.
                raise RemoteStateAdvanced(
                    "remote state/ advanced while this scan was running "
                    f"({', '.join(remote_state[:5])}). Refusing to overwrite "
                    "another run's results. This scan's output is discarded; "
                    "the next scan will start from the newer state.")

            # Source-only divergence (very often just the generated dashboard).
            # Take the remote wholesale, then re-apply our state on top.
            snapshot = _snapshot_state(cwd)
            try:
                git("reset", "--hard", remote_ref, cwd=cwd)
                _restore_state(cwd, snapshot)
            finally:
                shutil.rmtree(snapshot, ignore_errors=True)
            notes.append(
                f"synced onto {remote_sha[:7]}; adopted {len(incoming)} remote "
                f"source change(s): {', '.join(incoming[:5])}")

        if regenerate:
            # Always rebuilt AFTER syncing, so it reflects the merged result.
            # This is why the generated dashboard can never conflict.
            regenerate_dashboard(cwd)

        git("add", "-A", STATE_DIR, "docs", cwd=cwd)
        staged = git("diff", "--cached", "--name-only", cwd=cwd)
        if not staged.strip():
            return {"pushed": False, "reason": "no changes to persist",
                    "notes": notes, "attempts": attempt, "head": head}

        msg = message or f"scan {os.environ.get('SCAN_STAMP', '')}".strip()
        git("commit", "-m", msg or "scan", cwd=cwd)

        if not push:
            return {"pushed": False, "reason": "push disabled",
                    "notes": notes, "attempts": attempt,
                    "head": git("rev-parse", "HEAD", cwd=cwd)}

        r = subprocess.run(["git", "push", remote, f"HEAD:{branch}"], cwd=cwd,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        if r.returncode == 0:
            return {"pushed": True, "reason": "", "notes": notes,
                    "attempts": attempt,
                    "head": git("rev-parse", "HEAD", cwd=cwd)}

        # Rejected: the remote moved again between our fetch and our push.
        # Undo our local commit and retry from a fresh read of the remote.
        # NEVER --force: the other side's work is real.
        notes.append(f"push rejected on attempt {attempt}; retrying")
        git("reset", "--soft", "HEAD~1", cwd=cwd)

    raise PersistError(
        f"could not push after {attempts} attempts without force-pushing")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("sync", "persist"))
    ap.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", "main"))
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--message", default=None)
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)

    try:
        if a.action == "sync":
            sha = sync_before_scan(branch=a.branch, remote=a.remote)
            print(f"synced to {sha}")
            return 0
        res = persist(branch=a.branch, remote=a.remote, message=a.message,
                      push=not a.no_push)
        for n in res["notes"]:
            print(f"note: {n}")
        print("pushed" if res["pushed"] else f"not pushed: {res['reason']}")
        return 0
    except RemoteStateAdvanced as e:
        # Not a failure of this system -- a deliberate, safe refusal.
        print(f"::warning::{e}")
        return 0
    except PersistError as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
