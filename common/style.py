"""
Shared terminal styling for the Python tools in this repo.

One palette, in one place, so every report reads the same way:

    red      a difference, a failure, something wrong
    yellow   a warning, or agreement that is not reassurance
    green    clean, identical, as expected
    cyan     a heading
    dim      an aside -- context, provenance, "expected to differ"

These reports get long. Color is what lets someone scan one instead of reading
it, so the meaning of each color has to stay fixed across tools: a reader who
learns "red means look here" in one report should not have to relearn it in
the next.

Two rules that matter more than they look:

1.  **Put color codes at the END of a line, never inside a width-formatted
    field.** An escape sequence counts toward `{:<30}` and silently breaks
    column alignment. Format the text first, then color the formatted string.

2.  **Strip color before writing anything to a file.** Reports are meant to be
    pasted into tickets and read years later; escape sequences in a saved
    report are noise. `strip_ansi` does this, and `Reporter.write` calls it.

Usage, from a script one directory below the repo root:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.style import Reporter, Style, want_color

    rep = Reporter(Style(want_color(args.color)))
    rep('plain line')
    rep(f'  {name:<30} {rep.s.green("identical")}')   # color last
    rep.diff('nodes / Neuron: 50564 vs 50570')        # recorded for the roll-up

`common/` has no `__init__.py` on purpose -- it is a namespace package, so the
sys.path line above is all a script needs.
"""
import os
import re
import sys
from pathlib import Path

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi(text):
    """Remove every ANSI escape sequence, for file output."""
    return ANSI_RE.sub('', text)


class Style:
    """Color wrapper. Disabled instances return their input unchanged, so
    callers never need to branch on whether color is on."""

    CODES = {
        'red': '31;1',
        'yellow': '33;1',
        'green': '32',
        'cyan': '36',
        'dim': '2',
        'bold': '1',
    }

    def __init__(self, enabled):
        self.on = bool(enabled)

    def _wrap(self, code, text):
        return f'\x1b[{code}m{text}\x1b[0m' if self.on else str(text)

    def __getattr__(self, name):
        if name in self.CODES:
            return lambda text: self._wrap(self.CODES[name], text)
        raise AttributeError(name)

    # Convenience: the two verdicts almost every tool prints.
    def ok(self, text='OK'):
        return self.green(text)

    def bad(self, text='FAILED'):
        return self.red(text)


def want_color(choice=None, stream=None):
    """Resolve auto|always|never, in this precedence order:

        1. an explicit --color on the command line (including --color auto)
        2. the COLOR environment variable
        3. NO_COLOR (presence alone disables, per no-color.org)
        4. whether stdout is a terminal

    `choice` is None when the flag was not given, which is what lets an
    explicit `--color auto` out-rank COLOR: something the caller typed for
    this run beats their ambient environment.

    COLOR is read here so that one setting covers this whole repo. The shell
    scripts take positional arguments and cannot spare a --color flag, so they
    are driven by COLOR (see common/colors.sh); without this fallback you
    would have to remember which mechanism each tool used. `COLOR=always
    ./anything` now works whether that anything is Python or bash.

    An unrecognized COLOR value is ignored rather than fatal: it is ambient
    environment, not something the caller typed for this run, and failing a
    diagnostic run over a typo in a shell profile would be obnoxious.
    """
    if choice in ('always', 'never'):
        return choice == 'always'

    if choice is None:                      # flag absent -- consult the env
        env_choice = os.environ.get('COLOR', '').strip().lower()
        if env_choice in ('always', 'never'):
            return env_choice == 'always'
        if os.environ.get('NO_COLOR') is not None:
            return False

    stream = stream or sys.stdout
    return hasattr(stream, 'isatty') and stream.isatty()


def load_baseline(path):
    """Read a file of accepted-difference patterns.

    One pattern per line; `#` comments and blank lines ignored. A pattern is
    matched as a plain substring of the finding text, NOT a regex and not a
    glob -- so a typo silently accepts nothing rather than silently accepting
    everything, which is the failure direction that matters here.

    Key on the stable part of a finding. Finding text looks like

        endpoint / skeletons/skeleton: failed on neuprint-pre:wasp3:v0.0 only

    and the tail names instances that change between runs, so the pattern to
    write is the part before the colon:

        endpoint / skeletons/skeleton

    Returns a list of patterns, empty if path is None.
    """
    if not path:
        return []
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            out.append(line)
    return out


def add_baseline_argument(parser):
    """Add the standard --baseline flag."""
    parser.add_argument(
        '--baseline', metavar='FILE',
        help='a file of known, intended differences. Each line is matched as '
             'a substring against the findings; anything matched is reported '
             'as ACCEPTED rather than counted as a failure, so a difference '
             'you have signed off stops drowning out new ones. Still listed '
             'in the report -- accepted is not hidden.')
    return parser


def add_color_argument(parser):
    """Add the standard --color flag, so every tool spells it the same way."""
    parser.add_argument(
        '--color', choices=['auto', 'always', 'never'], default=None,
        metavar='WHEN',
        help='when to colorize differences and warnings. '
             '"auto" (the default) turns color on for a terminal and off when '
             'output is piped or redirected, so escape codes never land in a '
             'file. '
             '"always" forces it on even through a pipe -- use this when the '
             'thing reading can render color, such as `| less -R`. '
             '"never" forces it off, for a terminal that mangles ANSI or a log '
             'you want clean. '
             'With no --color, the COLOR environment variable is consulted '
             '(the shell scripts here use that), then NO_COLOR. Saved reports '
             'are always plain text regardless.')
    return parser


class Reporter:
    """Print to the terminal, collect plain text for a report file, and record
    findings for a roll-up at the end.

    The roll-up exists because these reports are long enough to skim past.
    Color helps you scan; the roll-up means you do not have to.
    """

    def __init__(self, style=None, baseline=None):
        self.lines = []
        self.findings = []          # (severity, text)
        self.fatals = []            # reasons the run could not complete
        self.s = style or Style(False)
        # Differences already known and accepted -- see load_baseline().
        self.baseline = baseline or []
        self.accepted = []          # (text, the pattern that accepted it)

    def __call__(self, text=''):
        print(text)
        self.lines.append(text)

    def heading(self, text):
        self(f'\n{self.s.cyan(text)}')

    def diff(self, text):
        """A real difference between the things being compared.

        A difference matching the baseline is recorded as accepted instead:
        still shown, still counted, but not a failure. Without this, a known
        and intended difference prints as DIFF on every run and steadily
        teaches the reader to skim the findings block -- which is the exact
        habit that block exists to prevent.
        """
        for pattern in self.baseline:
            if pattern in text:
                self.accepted.append((text, pattern))
                return
        self.findings.append(('DIFF', text))

    def warn(self, text):
        """Worth attention, but not a difference."""
        self.findings.append(('WARN', text))

    def fatal(self, text):
        """The run could not complete.

        Kept apart from differences on purpose: an unreachable server is not
        evidence that two things disagree, and reporting it as one invites
        exactly the wrong conclusion.
        """
        self.fatals.append(text)

    def counts(self):
        d = sum(1 for sev, _ in self.findings if sev == 'DIFF')
        w = sum(1 for sev, _ in self.findings if sev == 'WARN')
        return d, w

    def print_findings(self, title='findings'):
        """Replay everything flagged, so nothing depends on spotting a marker
        mid-scroll. Returns (diffs, warnings)."""
        d, w = self.counts()
        if self.findings:
            extra = (f', {len(self.accepted)} accepted' if self.accepted else '')
            self(f'\n--- {title} ({d} difference(s), {w} warning(s){extra}) ---')
            self(self.s.dim('  Everything flagged above, collected so a long '
                            'report cannot hide one.'))
            for sev, text in self.findings:
                tag = self.s.red('DIFF') if sev == 'DIFF' else self.s.yellow('WARN')
                self(f'  {tag}  {text}')
            if d:
                # Make accepting a difference a deliberate, copyable act
                # rather than something you do by editing until it is quiet.
                self(self.s.dim('\n  To accept one of these as intended, add '
                                'a line to the baseline file:'))
                for sev, text in self.findings:
                    if sev == 'DIFF':
                        self(self.s.dim(f'      {text.split(":")[0].strip()}'))

        if self.accepted:
            self(f'\n--- accepted differences ({len(self.accepted)}) ---')
            self(self.s.dim('  Real differences, known and signed off. Listed '
                            'so they stay visible, not hidden.'))
            for text, pattern in self.accepted:
                self(f'  {self.s.yellow("ACCEPTED")}  {text}')
                self(self.s.dim(f'          matched baseline: {pattern}'))
        return d, w

    def print_fatals(self, title='could not complete'):
        if not self.fatals:
            return False
        self(f'\n--- {title} ({len(self.fatals)}) ---')
        for text in self.fatals:
            self(f'  {self.s.red("ABORT")}  {text}')
        return True

    def write(self, path):
        """Save the report as plain text -- no escape sequences."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(strip_ansi(line) for line in self.lines) + '\n')
