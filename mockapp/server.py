"""
A deliberately old-fashioned "core banking" app used as the automation target.

It imitates the kind of server-rendered teller screens we see at credit unions:
framesets, table layouts, <font> tags, inputs named f1/f2, no ids, no test ids,
no labels. Everything is fake data.

There is a small test hook (/_test/fault) so the replay tests can inject the
runtime conditions we care about: an interstitial notice, a session expiry,
a slow response, a system error and an unknown supervisor-override screen.

Run:  python -m mockapp.server --port 8099
"""
from __future__ import annotations

import argparse
import html
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USERS = {"teller1": "demo-pass-1"}  # test credentials, not real

MEMBERS = {
    "12345": {
        "name": "JANE Q SAMPLE",
        "ssn": "123-45-6789",
        "dob": "04/12/1981",
        "accounts": [
            ("S01", "SHARE SAVINGS", "1,234.56"),
            ("S10", "CHRISTMAS CLUB", "310.00"),
            ("D20", "SHARE DRAFT CHECKING", "2,087.13"),
        ],
    },
    "20001": {
        "name": "ROBERT T EXAMPLE",
        "ssn": "987-65-4321",
        "dob": "11/02/1975",
        "accounts": [
            ("S01", "SHARE SAVINGS", "88.02"),
            ("D20", "SHARE DRAFT CHECKING", "15.40"),
        ],
    },
}

SESSIONS: dict[str, dict] = {}
# one-shot faults, consumed by the next matching request
FAULTS: set[str] = set()


def page(body: str, title: str = "CU*CORE") -> str:
    return (
        "<html><head><title>%s</title></head>"
        '<body bgcolor="#d4d0c8"><font face="Courier New" size="2">%s</font></body></html>'
        % (title, body)
    )


FRAMESET = """<html><head><title>CU*CORE Teller Workstation</title></head>
<frameset rows="40,*" border="1">
  <frame name="banner" src="/banner">
  <frameset cols="160,*">
    <frame name="menu" src="/menu">
    <frame name="main" src="/main">
  </frameset>
</frameset></html>"""

LOGIN = """
<table width="100%" height="80%"><tr><td align="center">
<form method="post" action="/signon" target="_top">
<table border="1" cellpadding="4" bgcolor="#ffffff">
<tr><td colspan="2" bgcolor="#000080"><font color="#ffffff"><b>SIGN ON</b></font></td></tr>
<tr><td>User ID</td><td><input type="text" name="f1" size="12"></td></tr>
<tr><td>Password</td><td><input type="password" name="f2" size="12"></td></tr>
<tr><td colspan="2" align="right"><input type="submit" value="Sign On"></td></tr>
</table>
{msg}
</form></td></tr></table>
"""

INQUIRY = """
<b>MEMBER INQUIRY - MI01</b><hr>
<form method="post" action="/inquiry">
<table border="0" cellpadding="3">
<tr><td>Member Number</td><td><input type="text" name="f1" size="10" maxlength="10"></td>
<td><input type="submit" value="Go"></td></tr>
</table>
</form>
{msg}
"""

TRANSFER = """
<b>SHARE TRANSFER - TR02</b><hr>
<form method="post" action="/transfer">
<table border="0" cellpadding="3">
<tr><td>From Member</td><td><input type="text" name="f1" size="10"></td></tr>
<tr><td>From Suffix</td><td><input type="text" name="f2" size="4"></td></tr>
<tr><td>To Suffix</td><td><input type="text" name="f3" size="4"></td></tr>
<tr><td>Amount</td><td><input type="text" name="f4" size="10"></td></tr>
<tr><td></td><td><input type="submit" value="Post Transfer"></td></tr>
</table>
</form>
"""


def member_detail(member_no: str, m: dict) -> str:
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td align=\"right\">%s</td></tr>" % a for a in m["accounts"]
    )
    return """
<b>MEMBER DETAIL - MI02</b><hr>
<table border="0" cellpadding="2">
<tr><td>Member #</td><td>%s</td></tr>
<tr><td>Name</td><td>%s</td></tr>
<tr><td>SSN</td><td>%s</td></tr>
<tr><td>DOB</td><td>%s</td></tr>
</table><br>
<table border="1" cellpadding="3" cellspacing="0">
<tr bgcolor="#c0c0c0"><td><b>Suffix</b></td><td><b>Description</b></td><td><b>Balance</b></td></tr>
%s
</table><br>
<a href="/inquiry">New Inquiry</a>
""" % (member_no, m["name"], m["ssn"], m["dob"], rows)


class Handler(BaseHTTPRequestHandler):
    server_version = "CUCORE/3.2"

    def log_message(self, fmt, *args):  # keep test output quiet
        pass

    # --- helpers -----------------------------------------------------------
    def _send(self, body: str, status: int = 200, headers: dict | None = None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _session(self) -> dict | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "CUSESS" and v in SESSIONS:
                return SESSIONS[v]
        return None

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _take_fault(self, name: str) -> bool:
        if name in FAULTS:
            FAULTS.discard(name)
            return True
        return False

    def _guard(self) -> bool:
        """Common checks for pages inside the signed-on area."""
        if self._take_fault("slow"):
            time.sleep(3)
        if self._take_fault("error"):
            self._send(page("<h3>SYSTEM ERROR CB-500</h3>Host communication failure. Contact IT."), 500)
            return False
        if self._take_fault("timeout"):
            # kill every session, like the host does after inactivity
            SESSIONS.clear()
        if not self._session():
            self._send(page("<font color=\"#800000\"><b>SESSION EXPIRED - PLEASE SIGN ON AGAIN</b></font>"
                            + LOGIN.replace("{msg}", "")))
            return False
        return True

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        if p == "/_test/fault":
            q = parse_qs(url.query)
            for f in q.get("set", []):
                FAULTS.add(f)
            if "clear" in q:
                FAULTS.clear()
            return self._send("ok %s" % sorted(FAULTS))
        if p == "/":
            return self._send(FRAMESET)
        if p == "/banner":
            s = self._session()
            who = s["user"] if s else "NOT SIGNED ON"
            return self._send(page("<b>CU*CORE 3.2</b> &nbsp; Branch 001 &nbsp; Operator: %s" % who))
        if p == "/menu":
            if not self._session():
                return self._send(page("&nbsp;"))
            return self._send(page(
                '<b>MAIN MENU</b><br><br>'
                '<a href="/inquiry" target="main">MI01 Member Inquiry</a><br>'
                '<a href="/transfer" target="main">TR02 Share Transfer</a><br>'
                '<a href="/reports" target="main">RP01 Reports</a><br><br>'
                '<a href="/signoff" target="_top">Sign Off</a>'))
        if p == "/main":
            if not self._session():
                return self._send(page(LOGIN.replace("{msg}", "")))
            return self._send(page("<b>WELCOME</b><br>Select a function from the menu."))
        if p == "/signoff":
            return self._send(FRAMESET, headers={"Set-Cookie": "CUSESS=; Max-Age=0"})
        if p == "/inquiry":
            if not self._guard():
                return
            return self._send(page(INQUIRY.replace("{msg}", "")))
        if p == "/transfer":
            if not self._guard():
                return
            return self._send(page(TRANSFER))
        if p == "/reports":
            if not self._guard():
                return
            return self._send(page("<b>REPORTS</b><br>No reports scheduled."))
        self._send(page("NOT FOUND"), 404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/signon":
            f = self._form()
            if USERS.get(f.get("f1", "")) == f.get("f2", ""):
                sid = secrets.token_hex(8)
                SESSIONS[sid] = {"user": f["f1"]}
                return self._send(FRAMESET, headers={"Set-Cookie": "CUSESS=%s; Path=/" % sid})
            return self._send(page(LOGIN.replace("{msg}", '<font color="#ff0000">INVALID USER ID OR PASSWORD</font>')))
        if p == "/inquiry":
            if not self._guard():
                return
            f = self._form()
            no = f.get("f1", "").strip()
            if self._take_fault("override"):
                # a screen the recorded flow has never seen
                return self._send(page(
                    "<b>SUPERVISOR OVERRIDE REQUIRED - SV09</b><hr>"
                    "Member account flagged for review. Enter supervisor code to continue.<br>"
                    '<form method="post" action="/override"><input type="hidden" name="m" value="%s">'
                    'Code <input type="password" name="f9" size="6"> '
                    '<input type="submit" value="Approve"></form>' % html.escape(no)))
            if self._take_fault("notice"):
                return self._send(page(
                    "<b>SYSTEM NOTICE</b><hr>End of day processing begins at 18:00. "
                    "Postings after 18:00 will be dated next business day.<br><br>"
                    '<form method="post" action="/notice_ack"><input type="hidden" name="m" value="%s">'
                    '<input type="submit" value="Continue"></form>' % html.escape(no)))
            return self._send(page(self._inquiry_result(no)))
        if p in ("/notice_ack", "/override"):
            if not self._guard():
                return
            f = self._form()
            return self._send(page(self._inquiry_result(f.get("m", ""))))
        if p == "/transfer":
            if not self._guard():
                return
            return self._send(page("<b>TRANSFER POSTED</b> Confirmation #%s" % secrets.randbelow(10**6)))
        self._send(page("NOT FOUND"), 404)

    def _inquiry_result(self, no: str) -> str:
        if not no.isdigit():
            return INQUIRY.replace("{msg}", '<font color="#ff0000">ERR 104 INVALID MEMBER NUMBER</font>')
        if no not in MEMBERS:
            return INQUIRY.replace("{msg}", '<font color="#ff0000">NO RECORD FOUND FOR MEMBER %s</font>' % no)
        return member_detail(no, MEMBERS[no])


def serve(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    print("CU*CORE mock listening on http://127.0.0.1:%d" % a.port)
    serve(a.port).serve_forever()
