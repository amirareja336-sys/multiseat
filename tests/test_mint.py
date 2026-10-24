"""Run with python -m unittest discover -s tests (Bash required). No root needed."""
import ast
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
SCRIPT = (ROOT / "multiseat-mint.sh").read_text()


class MintSeats(unittest.TestCase):
    def shell(self, body):
        functions = SCRIPT[SCRIPT.index("function start_seat2()"):
                           SCRIPT.index('case "$1" in', SCRIPT.index("function get_conf2()"))]
        result = subprocess.run([BASH, "-c", functions + "\n" + body],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_config_reads_per_seat_user(self):
        output = self.shell('''
oIFS=$IFS; isnumber='^[0-9]+$'
conf=$(mktemp); trap 'rm -f "$conf"' EXIT
printf 'card0-VGA-1\n    user alice\ncard0-HDMI-A-1\n\tuser bob\n' > "$conf"
get_conf2 user card0-VGA-1; echo
get_conf2 user card0-HDMI-A-1; echo
''')
        self.assertEqual(output.splitlines(), ["alice", "bob"])

    def seat_shell(self, user):
        return self.shell('''
oIFS=$IFS; default_compositor='labwc ; sfwbar'; ms_dir=/nonexistent
get_conf2() { [[ "$1" == user ]] && echo "''' + user + '''"; return 0; }
id() { if [[ "$*" == '-u root' ]]; then echo 0; elif [[ "$1" == '-u' ]]; then echo 1001; elif [[ "$1" == '-gn' ]]; then echo users; fi; }
getent() { echo 'alice:x:1001:1001::/tmp/alice:/bin/bash'; }
install() { :; }; chown() { :; }; wait_files() { :; }
localectl() { :; }; find() { :; }
systemctl() { [[ "$1" != set-environment ]] || exit 90; }
systemd-run() { printf 'RUN'; printf ' <%s>' "$@"; echo; }
start_seat2 card0-VGA-1
result=$?
echo "RESULT=$result"
''')

    def test_root_is_rejected_before_compositor_launch(self):
        output = self.seat_shell("root")
        self.assertIn("RESULT=21", output)
        self.assertNotIn("RUN <", output)

    def test_user_and_seat_environment_are_per_unit(self):
        output = self.seat_shell("alice")
        self.assertIn("RESULT=0", output)
        runs = [line for line in output.splitlines() if line.startswith("RUN")]
        self.assertEqual(len(runs), 2)
        for line in runs:
            self.assertIn("<--uid=1001>", line)
            self.assertIn("<--setenv=XDG_SEAT=seat-card0-VGA-1>", line)
            self.assertIn("<--setenv=DRM_LEASE=card0-VGA-1>", line)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=", runs[1])

    def test_gui_roundtrips_user_setting(self):
        tree = ast.parse((ROOT / "multiseat-gui.py").read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name in ("Seat", "Config")]
        namespace = {"re": __import__("re"), "Path": Path}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "config", "exec"), namespace)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "seats.conf"
            path.write_text("card0-VGA-1\n\tuser alice\ncard0-HDMI-A-1\n\tuser bob\n")
            config = namespace["Config"].load(path)
            path.write_text(config.to_text())
            restored = namespace["Config"].load(path)
            self.assertEqual([s.extra["user"] for s in restored.seats], ["alice", "bob"])

    def test_start_refuses_display_manager_and_cleans_up_failure(self):
        branch = SCRIPT[SCRIPT.index('    "-s" | "-S")'):SCRIPT.index('    "-q" | "-Q")')]
        for dm_active, expected in ((True, 16), (False, 17)):
            with self.subTest(dm_active=dm_active):
                prelude = '''
systemctl() { if [[ "$1" == is-active ]]; then return DM_RESULT; fi; }
bash() { echo "CHILD=$2"; [[ "$2" != -d ]]; }
systemd-run() { echo UNEXPECTED_GUARD; }
'''.replace("DM_RESULT", "0" if dm_active else "1")
                result = subprocess.run([BASH, "-c", prelude + '\ncase -S in\n' + branch + '\nesac'],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertNotIn("UNEXPECTED_GUARD", result.stdout)
                if dm_active:
                    self.assertNotIn("CHILD=", result.stdout)
                else:
                    self.assertIn("CHILD=-Q", result.stdout)
                    self.assertNotIn("CHILD=-r", result.stdout)


if __name__ == "__main__":
    unittest.main()
