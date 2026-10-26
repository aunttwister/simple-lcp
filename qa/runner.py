#!/usr/bin/env python3
"""LCP staging QA runner — executes suite_cases.json against a base URL.

Case schema:
  {id, group, title, cmd, expect: {exit: 0, contains: [..], not_contains: [..]},
   sleep_secs, timeout}

Every case is a bash command (curl | python3 -c asserts carry their own pass/fail
via exit code); {{BASE}} is substituted with --base. Results: results.json + summary
printed. A skipped/missing case is a FAIL.

Auth
----
Most of the console is behind the admin gate, so a plain `curl` gets a 302 and the
case fails for a reason that has nothing to do with the code under test. The runner
logs in once, writes the cookie into a throwaway HOME/.curlrc, and runs each case with
that HOME — so every bare `curl` in a case is authenticated without the case having to
know about it.

Cases that must be *anonymous* use `curl -q`, which makes curl ignore the config file.
That is why the auth cases are written that way rather than by unsetting a variable.

  --base    default http://192.168.1.198:8735
  --token   admin token; falls back to $LCP_ADMIN_TOKEN
  --no-auth skip login (only for a suite that does not need it)
"""
import argparse
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def login(base: str, token: str) -> str:
    """POST the admin token and return the lcp_admin cookie value."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    data = urllib.parse.urlencode({"token": token, "next": "/"}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/admin/login", data=data)
    try:
        opener.open(req, timeout=20).read()
    except urllib.error.HTTPError:
        pass
    for c in cj:
        if c.name == "lcp_admin":
            return c.value
    raise SystemExit("login failed: no lcp_admin cookie (wrong token?)")


def main() -> int:
    ap = argparse.ArgumentParser(description="LCP QA runner")
    ap.add_argument("--base", default="http://192.168.1.198:8735")
    ap.add_argument("--suite", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="comma ids to run (optional)")
    ap.add_argument("--token", default=os.environ.get("LCP_ADMIN_TOKEN"))
    ap.add_argument("--no-auth", action="store_true")
    args = ap.parse_args()

    home = None
    if not args.no_auth:
        if not args.token:
            raise SystemExit("no --token and no $LCP_ADMIN_TOKEN: cannot authenticate")
        cookie = login(args.base, args.token)
        home = tempfile.mkdtemp(prefix="lcp_qa_home_")
        with open(os.path.join(home, ".curlrc"), "w") as fh:
            fh.write('cookie = "lcp_admin=%s"\n' % cookie)
        print("authenticated: lcp_admin cookie set for this run")

    suite = json.load(open(args.suite, encoding="utf-8"))
    if args.only:
        only = set(x.strip() for x in args.only.split(",") if x.strip())
        cases = [c for c in suite["cases"] if c.get("id") in only]
    else:
        cases = suite["cases"]

    results = {"base": args.base, "suite": suite.get("name"),
               "cases": [], "pass": 0, "fail": 0, "error": 0,
               "groups": {}}
    env = dict(os.environ)
    if home:
        env["HOME"] = home

    for c in cases:
        cid = c.get("id") or "?"
        group = c.get("group") or "misc"
        cmd = c["cmd"].replace("{{BASE}}", args.base)
        if c.get("sleep_secs"):
            time.sleep(c["sleep_secs"])
        expect = c.get("expect") or {}
        row = {"id": cid, "group": group, "title": c.get("title"), "ok": False}
        try:
            proc = subprocess.run(["bash", "-c", cmd], capture_output=True,
                                  text=True, timeout=c.get("timeout", 60), env=env)
            out = (proc.stdout or "") + (proc.stderr or "")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            results["cases"].append({**row, "ok": False, "note": "timeout"})
            results["fail"] += 1
            results["groups"].setdefault(group, [0, 0])[1] += 1
            continue
        except Exception as exc:  # noqa: BLE001
            results["cases"].append({**row, "ok": False, "note": "runner error: %s" % exc})
            results["error"] += 1
            continue

        ok = code == int(expect.get("exit", 0))
        notes = []
        if ok:
            for s in expect.get("contains", []):
                if s not in out:
                    ok = False
                    notes.append("missing %r" % s)
                    break
        if ok:
            for s in expect.get("not_contains", []):
                if s in out:
                    ok = False
                    notes.append("unexpected %r" % s)
                    break
        row["ok"] = ok
        if not ok:
            row["exit"] = code
            row["tail"] = "\n".join(out.splitlines()[-4:])
            if notes:
                row["note"] = "; ".join(notes)
        results["cases"].append(row)
        if ok:
            results["pass"] += 1
        else:
            results["fail"] += 1
        g = results["groups"].setdefault(group, [0, 0])
        g[0] += 1 if ok else 0
        g[1] += 1

    out_path = args.out or (args.suite + ".results.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)

    print("QA suite: %d passed, %d failed, %d errors (of %d)" % (
        results["pass"], results["fail"], results["error"], len(cases)))
    for g, (p, n) in sorted(results["groups"].items()):
        print("  %-24s %3d / %3d" % (g[:24], p, n))
    if home:
        shutil.rmtree(home, ignore_errors=True)
    return 0 if results["fail"] == 0 and results["error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
