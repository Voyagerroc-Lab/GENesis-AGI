"""A wrapper's option arity must match the tool's, and a wrong entry fails OPEN.

``_WRAPPER_SPEC`` tells the resolver which of a wrapper's own options consume the
NEXT token, so the walk can reach the command being wrapped. It is hand-written,
and it has two failure directions that are not symmetric:

* An option MISSING from it: its value is read as the command. The resolved exe
  is then the value, which usually matches no gate — bad, but visible.
* An option WRONGLY listed: the walk eats the command WORD. The exe becomes the
  command's first argument, and a gate keyed on the exe never fires.

The second is the fail-open, and it is the one that had gone unnoticed here.
MEASURED 2026-09-13 against the real binaries and the real guards, every form
executed first so that a mis-parse of an unrunnable command could not be mistaken
for a bypass:

    xargs -i <push>     RUNS, resolved past `git`, push guard exit 0
    xargs -e <push>     RUNS, resolved past `git`, push guard exit 0
    env -S '<push>'     RUNS, resolved the whole string as the exe, guard exit 0

against a control of the same push written plainly, which exits 2. `xargs`
documents ``--eof[=END]`` and ``--replace[=R]`` — OPTIONAL values, so a bare
``-e``/``-i`` consumes nothing. This is the failure ``--isolated`` already taught
the uv table, which is why the last class here is a LOCK rather than a longer
list: the table is re-derived from each tool's own ``--help`` and a wrongly-listed
option fails the suite.

Not in scope, and deliberately: an option that is simply ABSENT from a table.
`env --unknown-flag x <push>` does resolve wrongly, but `env` exits 125 and never
runs the command, so nothing is permitted by it — the same reason #1686 stopped
decoding unterminated ANSI-C spans instead of parsing them.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


def _load():
    spec = importlib.util.spec_from_file_location("shell_parse_arity", _HOOKS / "shell_parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shell_parse_arity"] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load()

PUSH = "git " + "push --" + "for" + "ce origin main"


def exes(cmd: str) -> list[str]:
    return [s.exe for s in sp.analyze(cmd)]


def guard_rc(cmd: str, guard: str = "git_push_guard.py") -> int:
    proc = subprocess.run(
        [sys.executable, str(_HOOKS / guard)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )
    return proc.returncode


class TestControlsThatMustNotMove:
    """Every one of these resolved and blocked correctly BEFORE the fix too."""

    def test_the_plain_form_blocks(self):
        assert exes(PUSH) == ["git"]
        assert guard_rc(PUSH) == 2

    @pytest.mark.parametrize(
        "cmd",
        [
            "env -u FOO " + PUSH,
            "timeout -k 1 5 " + PUSH,
            "nice -n 5 " + PUSH,
            "stdbuf -o L " + PUSH,
            "echo x | xargs -I R " + PUSH,
            "echo x | xargs -n 1 " + PUSH,
            "echo x | xargs -E END " + PUSH,
        ],
    )
    def test_correctly_tabled_options_still_reveal_the_command(self, cmd):
        assert "git" in exes(cmd)
        assert guard_rc(cmd) == 2


class TestWronglyListedOptionsNoLongerEatTheCommand:
    """`-e`/`-i` take OPTIONAL values, so a bare one consumes nothing."""

    @pytest.mark.parametrize("flag", ["-i", "-e", "--replace", "--eof"])
    def test_an_optional_value_flag_does_not_consume_the_command(self, flag):
        cmd = f"echo x | xargs {flag} {PUSH}"
        assert "git" in exes(cmd), f"{flag} ate the command word"
        assert guard_rc(cmd) == 2

    def test_the_required_value_siblings_are_unaffected(self):
        # -E and -I take REQUIRED separate values and must still consume them.
        assert exes("echo x | xargs -E END " + PUSH) == ["echo", "git"]
        assert exes("echo x | xargs -I R " + PUSH) == ["echo", "git"]


class TestEnvSplitStringCarriesACommand:
    """`env -S 'cmd'` RUNS cmd; all four accepted spellings are measured."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "env -S '" + PUSH + "'",
            "env -S'" + PUSH + "'",
            "env --split-string='" + PUSH + "'",
            "env --split-string '" + PUSH + "'",
        ],
    )
    def test_the_carried_command_is_visible_to_the_guard(self, cmd):
        assert "git" in exes(cmd), f"carried command not revealed: {exes(cmd)}"
        assert guard_rc(cmd) == 2

    def test_the_string_is_never_itself_the_executable(self):
        # The pre-fix reading: the whole command line resolved as one exe name.
        assert PUSH not in exes("env -S '" + PUSH + "'")

    def test_env_without_the_flag_is_unchanged(self):
        assert exes("env FOO=1 " + PUSH) == ["git"]
        assert exes("env -u FOO " + PUSH) == ["git"]


# ── the lock ────────────────────────────────────────────────────────────

_HELP_LINE = re.compile(
    r"^\s*(?P<short>-[A-Za-z0-9])?(?:,\s*)?(?P<long>--[A-Za-z][-A-Za-z0-9]*)?(?P<tail>.*)$"
)


def _optional_value_options(help_text: str) -> set[str]:
    """Options the tool documents as taking an OPTIONAL value.

    Both spellings on a line share one arity, so ``-e, --eof[=END]`` marks BOTH
    `-e` and `--eof`. Reading only the long form is what let the original defect
    through an earlier version of this check: the dangerous entries were the
    SHORT ones.
    """
    found: set[str] = set()
    for line in help_text.splitlines():
        if not line.strip().startswith("-"):
            continue
        m = _HELP_LINE.match(line)
        if not m:
            continue
        tail = m.group("tail")
        if not tail.startswith("["):
            continue
        found.update(f for f in (m.group("short"), m.group("long")) if f)
    return found


def _help_text(tool: str) -> str | None:
    if not shutil.which(tool):
        return None
    for args in ((tool, "--help"), (tool, "-h")):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        except Exception:
            continue
        text = (proc.stdout or "") + (proc.stderr or "")
        if len(text) > 80:
            return text
    return None


class TestTableAgreesWithTheToolsThemselves:
    """Re-derive arity from each installed tool and fail on the FAIL-OPEN direction.

    Only the dangerous direction is enforced. A missing entry is left alone on
    purpose: it is the visible direction, and several omissions are correct
    (sudo documents `--preserve-env[=list]`, which must NOT be added).

    Tools absent from this machine are skipped rather than assumed. A table
    written from memory about an uninstalled tool is the thing this lock exists
    to prevent, so it declines to guess in exactly that case.
    """

    @pytest.mark.parametrize("tool", sorted(sp._WRAPPER_SPEC))
    def test_no_listed_option_is_documented_as_optional_valued(self, tool):
        help_text = _help_text(tool)
        if help_text is None:
            pytest.skip(f"{tool} is not installed here — arity cannot be measured")
        listed = sp._WRAPPER_SPEC[tool][0]
        optional = _optional_value_options(help_text)
        wrong = sorted(listed & optional)
        assert not wrong, (
            f"{tool}: {wrong} documented with an OPTIONAL value but listed as "
            f"value-consuming — the resolver will eat the wrapped command word"
        )

    def test_the_lock_can_see_a_short_form(self):
        """Guard the guard: the defect this was written for was a SHORT option.

        A version of this check that only read long forms passed over `-e`/`-i`
        entirely, so it would have reported the broken table as clean.
        """
        assert _optional_value_options("  -e, --eof[=END]  set logical EOF") == {
            "-e",
            "--eof",
        }
        assert _optional_value_options("  -s, --signal=SIGNAL  specify") == set()
