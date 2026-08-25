"""
Persistence race tests, against REAL git repositories.

These build an actual bare remote and real clones, then reproduce the reported
production failure: a scan starts on commit A, `main` advances to commit B that
modifies the generated dashboard, and the scan must still persist -- keeping
BOTH the remote's source change and its own fresh state and dashboard, with no
force-push and nothing silently discarded.

Mocking git here would prove nothing: the bug was in git's behaviour on a
generated file, not in our bookkeeping around it.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts import persist as P  # noqa: E402


def git(*args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, check=False,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{r.stdout}")
    return r.stdout.strip()


def _identity(repo):
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "test", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)


@pytest.fixture
def world(tmp_path):
    """A bare remote plus two clones: the scan runner, and 'someone else'."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    remote.mkdir(); seed.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=str(remote))

    git("init", "--initial-branch=main", cwd=str(seed))
    _identity(str(seed))
    # A minimal repo carrying the real source the persister needs.
    for sub in ("src", "config", "state", "docs", "scripts"):
        (seed / sub).mkdir(parents=True, exist_ok=True)
    for f in ("dashboard.py", "__init__.py"):
        shutil.copy(os.path.join(ROOT, "src", f), seed / "src" / f)
    for f in ("risk_config.json", "stations.json"):
        shutil.copy(os.path.join(ROOT, "config", f), seed / "config" / f)
    shutil.copy(os.path.join(ROOT, "scripts", "persist.py"), seed / "scripts")
    (seed / "README.md").write_text("# seed\n")
    (seed / "docs" / "index.html").write_text("<html>ORIGINAL DASHBOARD</html>")
    (seed / "state" / ".gitkeep").write_text("")
    git("add", "-A", cwd=str(seed))
    git("commit", "-m", "seed", cwd=str(seed))
    git("remote", "add", "origin", str(remote), cwd=str(seed))
    git("push", "-u", "origin", "main", cwd=str(seed))

    runner = tmp_path / "runner"
    other = tmp_path / "other"
    git("clone", str(remote), str(runner), cwd=str(tmp_path))
    git("clone", str(remote), str(other), cwd=str(tmp_path))
    _identity(str(runner)); _identity(str(other))
    return {"remote": str(remote), "runner": str(runner), "other": str(other)}


def _write_scan_state(repo, *, equity=48.96, marker="scan-1"):
    """Simulate what a scan writes into state/."""
    st = os.path.join(repo, "state")
    os.makedirs(st, exist_ok=True)
    json.dump({"equity": equity, "marker": marker, "markets_scanned": 5,
               "weather_markets_discovered": 5, "broad_scan_enabled": False,
               "last_successful_scan": "2026-08-25T01:00:00Z"},
              open(os.path.join(st, "status.json"), "w"))
    json.dump({"cash": 41.89, "starting_bankroll": 50.0, "peak_bankroll": 50.0,
               "positions": []}, open(os.path.join(st, "portfolio.json"), "w"))
    json.dump({"started_at": "2026-08-25T00:00:00Z", "complete": False,
               "completed_at": None, "hours": 48, "runs_observed": 1},
              open(os.path.join(st, "evaluation.json"), "w"))
    json.dump([], open(os.path.join(st, "opportunities.json"), "w"))
    open(os.path.join(st, "audit.jsonl"), "a").close()


def _advance_remote(world, *, path="docs/index.html",
                    content="<html>REMOTE CHANGED THIS</html>", msg="remote change"):
    """Someone else pushes to main while the scan is running."""
    other = world["other"]
    git("pull", "--ff-only", "origin", "main", cwd=other, check=False)
    full = os.path.join(other, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    open(full, "w").write(content)
    git("add", "-A", cwd=other)
    git("commit", "-m", msg, cwd=other)
    git("push", "origin", "main", cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


# ---------------------------------------------------------------------------
# THE REPORTED RACE
# ---------------------------------------------------------------------------

def test_scan_persists_when_remote_changed_the_generated_dashboard(world):
    """Scan at A; remote advances to B touching docs/index.html; persist works.

    Both must survive: the remote's source update AND this scan's fresh state
    and dashboard.
    """
    runner = world["runner"]
    commit_a = git("rev-parse", "HEAD", cwd=runner)

    # The remote moves while our scan is mid-flight, and touches the exact file
    # that used to make `git pull --rebase` conflict.
    _advance_remote(world, path="README.md", content="# updated by someone else\n",
                    msg="B: source change")
    _advance_remote(world, path="docs/index.html",
                    content="<html>REMOTE REGENERATED</html>", msg="B: dashboard")

    # Our scan finishes and writes its state.
    _write_scan_state(runner, marker="scan-under-test")

    res = P.persist(cwd=runner, branch="main", message="scan persist")
    assert res["pushed"] is True, res
    assert res["attempts"] == 1
    assert any("adopted" in n for n in res["notes"]), res["notes"]

    # --- verify on the REMOTE, which is what actually matters --------------
    check = os.path.join(os.path.dirname(runner), "verify")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))

    # 1. the remote's source change survived
    assert "updated by someone else" in open(os.path.join(check, "README.md")).read(), \
        "the remote's source change was discarded"

    # 2. our fresh state survived
    status = json.load(open(os.path.join(check, "state", "status.json")))
    assert status["marker"] == "scan-under-test", "the scan's state was lost"

    # 3. the dashboard was REGENERATED, not merged and not left stale
    html = open(os.path.join(check, "docs", "index.html")).read()
    assert "REMOTE REGENERATED" not in html, "stale remote dashboard was kept"
    assert "ORIGINAL DASHBOARD" not in html, "stale original dashboard was kept"
    assert "Polymarket Cowork Agent" in html, "dashboard was not regenerated"
    assert "<<<<<<<" not in html, "conflict markers reached the dashboard"

    # 4. history is intact -- commit A is still an ancestor, nothing was forced
    assert commit_a in git("log", "--format=%H", cwd=check).splitlines()


def test_no_force_push_anywhere_in_the_persister():
    """Check CODE, not prose -- the module's own comments mention --force."""
    import tokenize
    path = os.path.join(ROOT, "scripts", "persist.py")
    with open(path, "rb") as fh:
        toks = list(tokenize.tokenize(fh.readline))
    code_no_str = " ".join(
        t.string for t in toks
        if t.type not in (tokenize.COMMENT, tokenize.STRING,
                          tokenize.NL, tokenize.NEWLINE))
    for bad in ("--force", "+HEAD", "force_with_lease"):
        assert bad not in code_no_str, f"force-push construct {bad!r} in code"
    # No string literal may be a force flag either -- those become argv values.
    for t in toks:
        if t.type == tokenize.STRING:
            v = t.string.strip().strip('"').strip("'")
            assert v not in ("--force", "-f", "--force-with-lease"), \
                f"force flag {v!r} passed as an argument"


def test_remote_state_change_aborts_rather_than_clobbering(world):
    """Another scan's results must never be overwritten."""
    runner = world["runner"]
    _advance_remote(world, path="state/status.json",
                    content=json.dumps({"marker": "other-scan", "equity": 47.0}),
                    msg="B: another scan's state")
    _write_scan_state(runner, marker="mine")

    with pytest.raises(P.RemoteStateAdvanced):
        P.persist(cwd=runner, branch="main")

    check = os.path.join(os.path.dirname(runner), "verify2")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))
    kept = json.load(open(os.path.join(check, "state", "status.json")))
    assert kept["marker"] == "other-scan", "the other scan's state was clobbered"


def test_remote_state_abort_is_reported_not_crashed(world, capsys):
    """The CLI turns that refusal into a warning, not a red build."""
    runner = world["runner"]
    _advance_remote(world, path="state/status.json",
                    content=json.dumps({"marker": "other"}), msg="B")
    _write_scan_state(runner)
    os.chdir(runner)
    old_root = P.REPO_ROOT
    P.REPO_ROOT = runner
    try:
        rc = P.main(["persist", "--branch", "main"])
    finally:
        P.REPO_ROOT = old_root
    assert rc == 0
    assert "::warning::" in capsys.readouterr().out


def test_sync_before_scan_fast_forwards(world):
    runner = world["runner"]
    _advance_remote(world, path="README.md", content="# newer\n", msg="B")
    sha = P.sync_before_scan(cwd=runner, branch="main")
    assert "newer" in open(os.path.join(runner, "README.md")).read()
    assert sha == git("rev-parse", "origin/main", cwd=runner)


def test_sync_refuses_to_destroy_uncommitted_work(world):
    runner = world["runner"]
    open(os.path.join(runner, "README.md"), "a").write("local edit\n")
    with pytest.raises(P.PersistError, match="dirty"):
        P.sync_before_scan(cwd=runner, branch="main")


def test_no_changes_is_not_an_error(world):
    """With nothing new to write, persist is a clean no-op.

    Regeneration is off here because the dashboard embeds a generation
    timestamp and so differs on every build by design -- which is what makes
    the no-change branch only reachable with regeneration disabled.
    """
    res = P.persist(cwd=world["runner"], branch="main", regenerate=False)
    assert res["pushed"] is False and "no changes" in res["reason"]


def test_every_scan_leaves_a_heartbeat_commit(world):
    """Each scan leaves a commit, even when it trades nothing.

    The heartbeat comes from state, not from the dashboard: every scan rewrites
    status.json with fresh scan timestamps, so state always differs run to run.
    (The dashboard's own timestamp is minute-resolution, so it cannot be relied
    on for this -- two builds inside the same minute are byte-identical.)

    It matters operationally: GitHub disables scheduled workflows after 60 days
    without repository activity, and the commit log is how you can see from
    outside that scans are still running.
    """
    runner = world["runner"]
    _write_scan_state(runner, marker="scan-1")
    assert P.persist(cwd=runner, branch="main")["pushed"] is True

    # A later scan that traded nothing still records that it ran.
    _write_scan_state(runner, marker="scan-2")
    res = P.persist(cwd=runner, branch="main")
    assert res["pushed"] is True, "an idle scan should still leave a heartbeat"

    check = os.path.join(os.path.dirname(runner), "verify-hb")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))
    assert json.load(open(os.path.join(
        check, "state", "status.json")))["marker"] == "scan-2"


def test_retries_when_the_remote_moves_between_fetch_and_push(world, monkeypatch):
    """A push rejected by a concurrent write is retried, never forced."""
    runner = world["runner"]
    _write_scan_state(runner, marker="retry-case")

    calls = {"n": 0}
    real_run = subprocess.run

    def flaky(cmd, *a, **kw):
        # Fail the first push only, by racing the remote forward first.
        if (isinstance(cmd, list) and cmd[:2] == ["git", "push"]
                and kw.get("cwd") == runner and calls["n"] == 0):
            calls["n"] += 1
            _advance_remote(world, path="README.md", content="# raced\n",
                            msg="B: raced in")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", flaky)
    res = P.persist(cwd=runner, branch="main")
    assert res["pushed"] is True
    assert res["attempts"] >= 2, "the first push should have been rejected"

    check = os.path.join(os.path.dirname(runner), "verify3")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))
    assert "raced" in open(os.path.join(check, "README.md")).read()
    assert json.load(open(os.path.join(check, "state", "status.json")))["marker"] \
        == "retry-case"


def test_dashboard_is_regenerated_from_post_sync_state(world):
    """The dashboard must reflect OUR state, not whatever the remote had."""
    runner = world["runner"]
    _advance_remote(world, path="docs/index.html",
                    content="<html>STALE</html>", msg="B")
    _write_scan_state(runner, equity=12.34, marker="regen")
    P.persist(cwd=runner, branch="main")

    check = os.path.join(os.path.dirname(runner), "verify4")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))
    html = open(os.path.join(check, "docs", "index.html")).read()
    assert "STALE" not in html
    assert "weather markets only" in html, "regenerated from our weather-only state"


def test_the_old_rebase_approach_really_did_conflict(world):
    """Proof the regression above is real, not hypothetical.

    Reproduces the ORIGINAL production failure with the ORIGINAL strategy:
    commit the regenerated dashboard, then `git pull --rebase`. Git cannot
    merge two independently generated HTML files, so the rebase halts with
    conflict markers and the push becomes impossible -- exactly what was
    observed. If this ever stops failing, the scenario has drifted and the
    passing test above has stopped proving anything.
    """
    runner = world["runner"]
    _advance_remote(world, path="docs/index.html",
                    content="<html>REMOTE VERSION</html>", msg="B: dashboard")

    _write_scan_state(runner, marker="old-way")
    open(os.path.join(runner, "docs", "index.html"), "w").write(
        "<html>MY REGENERATED DASHBOARD</html>")
    git("add", "-A", cwd=runner)
    git("commit", "-m", "scan", cwd=runner)

    out = git("pull", "--rebase", "origin", "main", cwd=runner, check=False)
    rebase_halted = (os.path.isdir(os.path.join(runner, ".git", "rebase-merge"))
                     or os.path.isdir(os.path.join(runner, ".git", "rebase-apply")))
    assert rebase_halted, f"expected a rebase conflict, got:\n{out}"

    html = open(os.path.join(runner, "docs", "index.html")).read()
    assert "<<<<<<<" in html, "expected conflict markers in the generated file"

    # And here is the part that made this dangerous rather than merely noisy.
    # Mid-rebase HEAD is detached at the REMOTE commit, so `git push HEAD:main`
    # exits 0 -- it pushes nothing at all. The workflow could therefore report
    # a clean run while the scan's state never left the runner.
    push = subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=runner,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    check = os.path.join(os.path.dirname(runner), "verify-old")
    git("clone", world["remote"], check, cwd=os.path.dirname(runner))
    status_path = os.path.join(check, "state", "status.json")
    landed = (os.path.exists(status_path)
              and json.load(open(status_path)).get("marker") == "old-way")
    assert not landed, (
        "the old approach was expected to lose the scan's state; if it now "
        "lands, this scenario no longer reproduces the reported failure")

    git("rebase", "--abort", cwd=runner, check=False)
