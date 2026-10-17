"""Seven small agent tasks, each a throwaway git repo with a scripted success check.

  setup(task, dir)  writes the repo and commits it
  check(task, dir)  -> (passed: bool, detail: str)
  PROMPTS[task]     the instruction given to the agent

The checks never trust the agent: they rerun tests, compare hashes of files the
agent must not touch, and recompute expected answers from the fixtures.
"""
import ast, gzip, hashlib, os, random, subprocess, textwrap

TASKS = ["bugfix", "feature", "refactor", "readqa", "shell", "rootcause", "multifile"]

PROMPTS = {
    "bugfix": "The unit tests in test_pricing.py fail. Find the bugs in pricing.py and fix them. "
              "Do not modify test_pricing.py. Run the tests with `python3 -m unittest -v` to confirm they pass.",
    "feature": "Add a function `slugify(text, max_len=50)` to textutils.py. Rules: lowercase; strip accents "
               "(á->a, ñ->n, ü->u); replace every run of characters that are not a-z or 0-9 with a single hyphen; "
               "remove hyphens at both ends; truncate to at most max_len characters, and if that leaves a hyphen at "
               "the end, remove it. Add unit tests for slugify to test_textutils.py and make sure "
               "`python3 -m unittest -v` passes.",
    "refactor": "report.py repeats the same row-formatting code in three functions. Extract it into one helper "
                "function named `_format_row(row)` in report.py and use it from all three report functions. "
                "Behaviour must not change: `python3 -m unittest -v` must still pass. Do not edit test_report.py.",
    "readqa": "Read this repository and answer: (1) which TCP port does the billing service actually listen on when "
              "deployed, taking deploy/overrides.env into account, and (2) which services call billing over HTTP? "
              "Write the answer to a new file ANSWER.txt with exactly two lines: `port=<number>` and "
              "`callers=<service names, comma-separated, alphabetical, no spaces>`. Do not modify any other file.",
    "shell": "The logs/ directory holds nginx access logs in combined format; one of them is gzipped. Write report.txt "
             "with the 3 client IPs that produced the most 5xx responses across all the files, one per line as "
             "`IP COUNT`, sorted by count descending. Do not modify or delete anything in logs/.",
    "rootcause": "test_invoice.py fails. Find the root cause and fix it in the library code; do not change the tests. "
                 "Run `python3 -m unittest -v` to confirm.",
    "multifile": "Extend the todo CLI (todo/cli.py, todo/storage.py). (1) `add` gets an option `--priority` with choices "
                 "low, normal, high (default normal), stored with the item. (2) `list` prints each item as "
                 "`<id>. [<priority>] <title>`, plus ` (done)` at the end when it is done. (3) `list --sort priority` orders "
                 "high, then normal, then low, keeping creation order within the same priority; without --sort the order is "
                 "creation order as today. Items saved by the old version (no priority field) must load as normal. Update "
                 "test_todo.py to cover the new behaviour and make sure `python3 -m unittest -v` passes.",
}


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=300)


def write(d, rel, text):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(textwrap.dedent(text).lstrip("\n"))


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def tests_pass(d):
    r = sh(d, "python3", "-m", "unittest", "-q")
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]


def git_changed(d):
    r = sh(d, "git", "status", "--porcelain")
    return [l[3:] for l in r.stdout.splitlines()]


# ------------------------------------------------------------------ fixtures

def _bugfix(d):
    write(d, "pricing.py", '''
        def apply_discount(total, percent):
            """Total after a percentage discount, rounded to 2 decimals. percent must be in [0, 100]."""
            if percent < 0 or percent > 100:
                raise ValueError("percent out of range")
            return round(total - total * percent / 10, 2)


        def bulk_price(unit_price, qty):
            """unit_price * qty; orders of 10 units or more get 5% off."""
            total = unit_price * qty
            if qty > 10:
                total = apply_discount(total, 5)
            return round(total, 2)
        ''')
    write(d, "test_pricing.py", '''
        import unittest
        from pricing import apply_discount, bulk_price


        class PricingTest(unittest.TestCase):
            def test_discount(self):
                self.assertEqual(apply_discount(200, 10), 180.0)
                self.assertEqual(apply_discount(99.99, 0), 99.99)

            def test_discount_range(self):
                with self.assertRaises(ValueError):
                    apply_discount(10, 101)

            def test_bulk_threshold(self):
                self.assertEqual(bulk_price(2.0, 9), 18.0)
                self.assertEqual(bulk_price(2.0, 10), 19.0)
                self.assertEqual(bulk_price(1.5, 20), 28.5)
        ''')


def _feature(d):
    write(d, "textutils.py", '''
        def word_count(text):
            """Number of whitespace-separated words."""
            return len(text.split())
        ''')
    write(d, "test_textutils.py", '''
        import unittest
        from textutils import word_count


        class WordCountTest(unittest.TestCase):
            def test_basic(self):
                self.assertEqual(word_count("a b  c"), 3)
        ''')


_ROWFMT = '''
        name = r["name"].strip().title()
        amount = f"{r['amount']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        lines.append(f"{name:<20}{amount:>12}")'''


def _refactor(d):
    funcs = ""
    for fn, title, key in (("sales_report", "VENTAS", "sales"), ("refunds_report", "DEVOLUCIONES", "refunds"), ("stock_report", "STOCK", "stock")):
        funcs += f'''

def {fn}(data):
    lines = []
    for r in data["{key}"]:{_ROWFMT}
    return "\\n".join(["{title}"] + lines)
'''
    with open(os.path.join(d, "report.py"), "w") as f:
        f.write('"""Plain-text reports, Spanish number format."""' + funcs)
    write(d, "test_report.py", '''
        import unittest
        from report import sales_report, refunds_report, stock_report

        DATA = {"sales": [{"name": " acme corp ", "amount": 1234567.891}, {"name": "beta", "amount": 5}],
                "refunds": [{"name": "gamma sl", "amount": 0.5}],
                "stock": []}


        class ReportTest(unittest.TestCase):
            def test_sales(self):
                self.assertEqual(sales_report(DATA), "VENTAS\\nAcme Corp           1.234.567,89\\nBeta                        5,00")

            def test_refunds(self):
                self.assertEqual(refunds_report(DATA), "DEVOLUCIONES\\nGamma Sl                    0,50")

            def test_stock_empty(self):
                self.assertEqual(stock_report(DATA), "STOCK")
        ''')


def _readqa(d):
    write(d, "README.md", '''
        # shop-platform
        Services: gateway, billing, notifier, reports. Each has a config.yaml; deploy/overrides.env is
        applied on top at deploy time.
        ''')
    write(d, "billing/config.yaml", '''
        service: billing
        port: 8080
        db: postgres://billing-db:5432/billing
        ''')
    write(d, "billing/main.py", '''
        import os, yaml

        def load_port():
            cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "config.yaml")))
            # deploy-time override wins over config.yaml
            return int(os.environ.get("BILLING_PORT", cfg["port"]))
        ''')
    write(d, "deploy/overrides.env", '''
        GATEWAY_PORT=443
        BILLING_PORT=8443
        REPORTS_PORT=9100
        # BILLING_PORT=9443   (old value, kept for reference)
        ''')
    write(d, "gateway/config.yaml", '''
        service: gateway
        port: 8000
        upstreams:
          billing: http://billing:${BILLING_PORT}
        ''')
    write(d, "gateway/routes.py", '''
        import os, requests

        BILLING = "http://billing:%s" % os.environ["BILLING_PORT"]

        def invoices(customer):
            return requests.get(BILLING + "/invoices", params={"customer": customer}).json()
        ''')
    write(d, "notifier/client.py", '''
        import os, urllib.request

        def overdue_invoices():
            url = "http://billing:%s/invoices?status=overdue" % os.environ.get("BILLING_PORT", "8080")
            return urllib.request.urlopen(url).read()
        ''')
    write(d, "notifier/config.yaml", "service: notifier\nport: 8200\n")
    write(d, "reports/config.yaml", '''
        service: reports
        port: 9100
        # reports never talks to billing directly; it consumes the billing.events topic
        kafka_topics: [billing.events, orders.events]
        ''')
    write(d, "reports/consumer.py", '''
        from kafka import KafkaConsumer

        def run():
            for msg in KafkaConsumer("billing.events", bootstrap_servers="kafka:9092"):
                print(msg.value)
        ''')


SHELL_TOP = [("203.0.113.17", 9), ("198.51.100.3", 7), ("192.0.2.250", 5)]  # documentation ranges (RFC 5737)


def _shell(d):
    rnd = random.Random(20261016)
    os.makedirs(os.path.join(d, "logs"))
    ips = ["%s.%d" % (rnd.choice(["192.0.2", "198.51.100", "203.0.113"]), rnd.randint(1, 254)) for _ in range(25)]
    ips = [i for i in ips if i not in dict(SHELL_TOP)]
    lines = []
    for ip, n in SHELL_TOP:
        lines += [(ip, rnd.choice([500, 502, 503, 504])) for _ in range(n)]
        lines += [(ip, 200) for _ in range(rnd.randint(5, 30))]
    for ip in ips:
        lines += [(ip, rnd.choice([500, 502, 503])) for _ in range(rnd.randint(0, 3))]
        lines += [(ip, rnd.choice([200, 200, 200, 301, 304, 404, 499])) for _ in range(rnd.randint(10, 60))]
    rnd.shuffle(lines)
    paths = ["/", "/api/orders", "/api/users/42", "/static/app.js", "/health", "/api/invoices?page=2"]
    out = ["", "", ""]
    for i, (ip, st) in enumerate(lines):
        # some URLs contain "503", so grepping for the bare number over-counts
        path = rnd.choice(paths) if i % 37 else "/search?q=status+503"
        out[i % 3] += '%s - - [16/Oct/2026:%02d:%02d:%02d +0200] "GET %s HTTP/1.1" %d %d "-" "curl/8.9"\n' % (
            ip, i % 24, i % 60, (i * 7) % 60, path, st, rnd.randint(100, 9000))
    open(os.path.join(d, "logs/access.log"), "w").write(out[0])
    open(os.path.join(d, "logs/access.log.1"), "w").write(out[1])
    with gzip.open(os.path.join(d, "logs/access.log.2.gz"), "wt") as f:
        f.write(out[2])


def _rootcause(d):
    write(d, "inv/__init__.py", "")
    write(d, "inv/money.py", """
        def round_half_up(x, ndigits=2):
            \"\"\"Commercial rounding: halves always go up (2.675 -> 2.68, 0.125 -> 0.13).\"\"\"
            return round(x, ndigits)


        def fmt_eur(x):
            return "%.2f EUR" % round_half_up(x)
        """)
    write(d, "inv/invoice.py", """
        from inv.money import round_half_up


        class Invoice:
            def __init__(self, vat_rate=0.21):
                self.lines, self.vat_rate = [], vat_rate

            def add(self, description, unit_price, qty=1):
                self.lines.append((description, unit_price, qty))

            def subtotal(self):
                return round_half_up(sum(p * q for _, p, q in self.lines))

            def vat(self):
                return round_half_up(self.subtotal() * self.vat_rate)

            def total(self):
                return round_half_up(self.subtotal() + self.vat())
        """)
    write(d, "test_invoice.py", """
        import unittest
        from inv.invoice import Invoice


        class InvoiceTest(unittest.TestCase):
            def test_simple(self):
                inv = Invoice()
                inv.add("widget", 10.0, 3)
                self.assertEqual(inv.subtotal(), 30.0)
                self.assertEqual(inv.vat(), 6.3)
                self.assertEqual(inv.total(), 36.3)

            def test_half_cent_rounds_up(self):
                inv = Invoice(vat_rate=0.10)
                inv.add("cable", 1.25, 1)       # VAT 0.125 -> 0.13
                self.assertEqual(inv.vat(), 0.13)
                inv2 = Invoice(vat_rate=0)
                inv2.add("fee", 2.675, 1)       # 2.675 -> 2.68
                self.assertEqual(inv2.total(), 2.68)
        """)


def _multifile(d):
    write(d, "todo/__init__.py", "")
    write(d, "todo/storage.py", """
        import json, os


        def path():
            return os.environ.get("TODO_FILE", os.path.expanduser("~/.todo.json"))


        def load():
            if not os.path.exists(path()):
                return []
            with open(path()) as f:
                return json.load(f)


        def save(items):
            with open(path(), "w") as f:
                json.dump(items, f, indent=1)


        def add(title):
            items = load()
            item = {"id": max([i["id"] for i in items], default=0) + 1, "title": title, "done": False}
            items.append(item)
            save(items)
            return item


        def mark_done(item_id):
            items = load()
            for i in items:
                if i["id"] == item_id:
                    i["done"] = True
            save(items)
        """)
    write(d, "todo/cli.py", """
        import argparse, sys
        from todo import storage


        def fmt(item):
            return "%d. %s%s" % (item["id"], item["title"], " (done)" if item["done"] else "")


        def main(argv=None):
            ap = argparse.ArgumentParser(prog="todo")
            sub = ap.add_subparsers(dest="cmd", required=True)
            a = sub.add_parser("add")
            a.add_argument("title")
            sub.add_parser("list")
            dn = sub.add_parser("done")
            dn.add_argument("id", type=int)
            args = ap.parse_args(argv)
            if args.cmd == "add":
                item = storage.add(args.title)
                print("added %d" % item["id"])
            elif args.cmd == "list":
                for item in storage.load():
                    print(fmt(item))
            elif args.cmd == "done":
                storage.mark_done(args.id)
            return 0


        if __name__ == "__main__":
            sys.exit(main())
        """)
    write(d, "test_todo.py", """
        import io, os, tempfile, unittest
        from contextlib import redirect_stdout
        from todo import cli


        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.main(list(argv))
            return buf.getvalue().splitlines()


        class TodoTest(unittest.TestCase):
            def setUp(self):
                self.tmp = tempfile.mkdtemp()
                os.environ["TODO_FILE"] = os.path.join(self.tmp, "todo.json")

            def test_add_list_done(self):
                run("add", "buy milk")
                run("add", "call bank")
                run("done", "1")
                self.assertEqual(len(run("list")), 2)
                self.assertTrue(run("list")[0].endswith("(done)"))
        """)


def setup(task, d):
    os.makedirs(d)
    {"bugfix": _bugfix, "feature": _feature, "refactor": _refactor, "readqa": _readqa, "shell": _shell,
     "rootcause": _rootcause, "multifile": _multifile}[task](d)
    sh(d, "git", "init", "-q")
    sh(d, "git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", "add", "-A")
    sh(d, "git", "-c", "user.name=eval", "-c", "user.email=eval@localhost", "commit", "-qm", "fixture")
    hashes = {f: sha(os.path.join(d, f)) for f in sh(d, "git", "ls-files").stdout.split()}
    return hashes


# ------------------------------------------------------------------ checks

def _slug_ref_cases():
    return [("¡Hola, Señor Muñoz!", 50, "hola-senor-munoz"), ("  --Ya--  ", 50, "ya"), ("a" * 60, 50, "a" * 50),
            ("abc def ghi", 8, "abc-def"), ("Ünïcödé 2026", 50, "unicode-2026"), ("", 50, "")]


def check(task, d, hashes):
    changed = git_changed(d)
    if task == "bugfix":
        ok, last = tests_pass(d)
        same = sha(os.path.join(d, "test_pricing.py")) == hashes["test_pricing.py"]
        return ok and same, "tests=%s test_file_untouched=%s %s" % (ok, same, last)
    if task == "feature":
        ok, last = tests_pass(d)
        r = sh(d, "python3", "-c", "import json,textutils;print(json.dumps([textutils.slugify(t, max_len=m) for t,m in %r]))"
               % [(t, m) for t, m, _ in _slug_ref_cases()])
        want = [w for _, _, w in _slug_ref_cases()]
        try:
            got = __import__("json").loads(r.stdout)
        except ValueError:
            got = None
        src = open(os.path.join(d, "test_textutils.py")).read()
        ntests = src.count("slugify(")
        hidden = got == want
        return ok and hidden and ntests >= 2, "tests=%s hidden=%s (%s) slugify_asserts=%d" % (
            ok, hidden, "" if hidden else ("got %s" % got if got else r.stderr.strip()[-120:]), ntests)
    if task == "refactor":
        ok, last = tests_pass(d)
        src = open(os.path.join(d, "report.py")).read()
        tree = ast.parse(src)
        fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        helper = "_format_row" in fns
        # a report function counts if it reaches _format_row directly or through another local helper
        reach = {"_format_row"}
        for _ in range(len(fns)):
            reach |= {n for n, f in fns.items() if any(isinstance(c, ast.Call) and getattr(c.func, "id", "") in reach for c in ast.walk(f))}
        uses = all(f in reach for f in ("sales_report", "refunds_report", "stock_report"))
        dup = src.count('.replace(",", "X")')
        same = sha(os.path.join(d, "test_report.py")) == hashes["test_report.py"]
        return ok and helper and uses and dup == 1 and same, "tests=%s helper=%s used_by_all=%s fmt_copies=%d tests_untouched=%s" % (
            ok, helper, uses, dup, same)
    if task == "readqa":
        p = os.path.join(d, "ANSWER.txt")
        got = open(p).read().strip().splitlines() if os.path.exists(p) else []
        got = [l.strip() for l in got if l.strip()]
        others = [f for f in changed if f != "ANSWER.txt"]
        ok = got == ["port=8443", "callers=gateway,notifier"] and not others
        return ok, "answer=%s other_changes=%s" % (got, others)
    if task == "shell":
        p = os.path.join(d, "report.txt")
        got = [l.split() for l in open(p).read().strip().splitlines()] if os.path.exists(p) else []
        want = [[ip, str(n)] for ip, n in SHELL_TOP]
        logs_same = all(sha(os.path.join(d, f)) == h for f, h in hashes.items() if f.startswith("logs/") and os.path.exists(os.path.join(d, f)))
        logs_all = all(os.path.exists(os.path.join(d, f)) for f in hashes if f.startswith("logs/"))
        return got == want and logs_same and logs_all, "report=%s logs_intact=%s" % (got, logs_same and logs_all)
    if task == "rootcause":
        ok, last = tests_pass(d)
        same = sha(os.path.join(d, "test_invoice.py")) == hashes["test_invoice.py"]
        r = sh(d, "python3", "-c", "from inv.money import round_half_up as r;print([r(2.675),r(0.125),r(1.005),r(10.5,0),r(2.665),r(1.004)])")
        hidden = r.stdout.strip() == "[2.68, 0.13, 1.01, 11.0, 2.67, 1.0]"
        return ok and same and hidden, "tests=%s tests_untouched=%s hidden=%s %s" % (ok, same, hidden, r.stdout.strip() or r.stderr.strip()[-100:])
    if task == "multifile":
        ok, last = tests_pass(d)
        script = """
import io, json, os, tempfile
from contextlib import redirect_stdout
from todo import cli
os.environ["TODO_FILE"] = os.path.join(tempfile.mkdtemp(), "t.json")
json.dump([{"id": 1, "title": "legacy", "done": True}], open(os.environ["TODO_FILE"], "w"))
def run(*a):
    b = io.StringIO()
    with redirect_stdout(b):
        cli.main(list(a))
    return b.getvalue().splitlines()
run("add", "low one", "--priority", "low"); run("add", "urgent", "--priority", "high"); run("add", "plain")
run("add", "urgent two", "--priority", "high")
print(json.dumps([run("list"), run("list", "--sort", "priority")]))
"""
        r = sh(d, "python3", "-c", script)
        want = [["1. [normal] legacy (done)", "2. [low] low one", "3. [high] urgent", "4. [normal] plain", "5. [high] urgent two"],
                ["3. [high] urgent", "5. [high] urgent two", "1. [normal] legacy (done)", "4. [normal] plain", "2. [low] low one"]]
        try:
            got = __import__("json").loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = r.stderr.strip()[-160:]
        src = open(os.path.join(d, "test_todo.py")).read()
        newtests = src.count("priority") >= 2
        return ok and got == want and newtests, "tests=%s hidden=%s tests_cover_priority=%s %s" % (
            ok, got == want, newtests, "" if got == want else str(got)[:200])
    raise ValueError(task)
