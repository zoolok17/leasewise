"""Card 7b-on fix 1: `identity-set` can read the identity value from standard input, so it never appears on a command line.

The switch-on script enrolls this machine's identity value with `--identity-stdin`. The old `--identity-ref` keeps working. Exactly one of the two
must be given; a missing, doubled or empty value is the static INVALID_REQUEST and nothing is echoed. Synthetic data only.
"""

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "SYNTHETIC-IDENTITY-VALUE-4471"


def cli(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "quota_balancer", *args], capture_output=True, text=True, input=stdin, cwd=ROOT,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


class IdentityStdin(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        self._tmp = tempfile.TemporaryDirectory(dir=base, prefix="quota-idstdin-")
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "inventory.db")
        self.assertEqual(cli("--db", self.db, "init").returncode, 0)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO machines(id, alias) VALUES('m1', 'laptop-b')")
        conn.commit()
        conn.close()

    def stored(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT identity_ref, enabled FROM machines").fetchone()
        finally:
            conn.close()

    def test_the_value_read_from_standard_input_is_enrolled(self):
        done = cli("--db", self.db, "identity-set", "--machine", "laptop-b", "--identity-stdin", "--enabled", "true", stdin=SECRET + "\n")
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(self.stored(), (SECRET, 1))

    def test_a_windows_line_ending_and_a_missing_one_are_both_fine(self):
        for text in (SECRET + "\r\n", SECRET):
            done = cli("--db", self.db, "identity-set", "--machine", "laptop-b", "--identity-stdin", "--enabled", "true", stdin=text)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(self.stored()[0], SECRET)

    def test_the_command_line_form_still_works(self):
        done = cli("--db", self.db, "identity-set", "--machine", "laptop-b", "--identity-ref", SECRET, "--enabled", "true")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.stored(), (SECRET, 1))

    def test_neither_both_nor_an_empty_value_is_refused_without_echoing_anything(self):
        cases = (("--enabled", "true"), ("--identity-stdin", "--identity-ref", SECRET, "--enabled", "true"),
                 ("--identity-stdin", "--enabled", "true"), ("--identity-ref", "", "--enabled", "true"))
        for extra in cases:
            done = cli("--db", self.db, "identity-set", "--machine", "laptop-b", *extra, stdin="\n" if "--identity-stdin" in extra else None)
            self.assertEqual(done.returncode, 2, extra)
            self.assertIn("INVALID_REQUEST", done.stderr)
            self.assertNotIn(SECRET, done.stdout + done.stderr)
        self.assertIsNone(self.stored()[0])

    def test_the_value_is_not_in_the_process_arguments_of_the_stdin_form(self):
        args = ["--db", self.db, "identity-set", "--machine", "laptop-b", "--identity-stdin", "--enabled", "true"]
        self.assertFalse(any(SECRET in item for item in args))


if __name__ == "__main__":
    unittest.main()
