# Shared terminal colors for the shell scripts in this repo.
#
# Source it, do not execute it:
#
#     . "$(dirname "$0")/../common/colors.sh"
#     echo "  ${C_GREEN}ok${C_RESET}     ${label}"
#
# The palette matches common/style.py exactly, so a FAIL from a shell script
# and a DIFF from a Python one are the same red. The meanings are fixed:
#
#     C_RED      a difference, a failure, something wrong
#     C_YELLOW   a warning, or agreement that is not reassurance
#     C_GREEN    clean, identical, as expected
#     C_CYAN     a heading
#     C_DIM      an aside -- context, provenance, "expected to differ"
#
# When color is off, every variable is the empty string, so callers never
# branch on whether it is enabled.
#
# Control:
#
#     COLOR=auto     (default) on for a terminal, off when piped or redirected
#     COLOR=always   force on, e.g. when piping to `less -R`
#     COLOR=never    force off
#     NO_COLOR=<any> force off, per no-color.org -- presence alone counts,
#                    so NO_COLOR= (empty) still disables it
#
# Two cautions, both learned the hard way:
#
#   * Put color at the END of a printf field, never inside the width. An
#     escape sequence counts toward '%-20s' and silently breaks alignment.
#     Color the text after formatting it, or color only trailing words.
#   * Anything redirected to a file should be uncolored. These scripts write
#     reports meant for tickets; escape sequences in them are noise.
#
# Written for bash 3.2, which is what macOS ships -- no associative arrays,
# no ${var^^}, nothing from bash 4.

# These are exported, not merely set, because several scripts here generate
# their output from a quoted heredoc (`python3 - <<'PY'`) where no shell
# expansion happens. Exporting lets that Python read the same palette out of
# os.environ instead of each script inventing its own.
#
# shellcheck disable=SC2034  # these are for the sourcing script, not this one
if [ "${COLOR:-auto}" = "always" ] ||
   { [ "${COLOR:-auto}" = "auto" ] && [ -t 1 ] && [ -z "${NO_COLOR+set}" ]; }
then
    C_RED=$'\033[31;1m'
    C_YELLOW=$'\033[33;1m'
    C_GREEN=$'\033[32m'
    C_CYAN=$'\033[36m'
    C_DIM=$'\033[2m'
    C_BOLD=$'\033[1m'
    C_RESET=$'\033[0m'
else
    C_RED=''
    C_YELLOW=''
    C_GREEN=''
    C_CYAN=''
    C_DIM=''
    C_BOLD=''
    C_RESET=''
fi

export C_RED C_YELLOW C_GREEN C_CYAN C_DIM C_BOLD C_RESET
