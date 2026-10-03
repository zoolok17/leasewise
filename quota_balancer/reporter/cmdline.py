"""Two pure helpers for a receipt command (card L6a-1b, step 1). Nothing here starts a program or touches the system.

* `names_reserved_option`: would a configured argument be read as one of the options the reporter adds itself
  (`--after`, `--limit`, `--json`)? The reporter appends those last, so the operator may not name them.
* `quote_command`: one Windows command line from an argument list, by the standard rules of the C runtime that every
  ordinary Windows program uses to split its line again.
"""

RESERVED_OPTIONS = ("after", "limit", "json")


def names_reserved_option(argument):
    """True when `argument` could be read as one of the reporter's own options.

    Only an argument that starts with `--` is looked at. An option parser that allows shortened options accepts a long
    option when the typed text is a PREFIX of the full name, so the name (the text after `--`, up to the first `=`) is
    refused when it is empty or a prefix of a reserved word. That covers the exact word, a shortened word and the
    `--name=value` form. Exactly `--` has an empty name: it would end option parsing, so it is refused too. A plain word
    (`json`), a single-dash argument (`-m`) and a longer word that merely starts with a reserved one (`--afterx`) pass."""
    if not argument.startswith("--"):
        return False
    name = argument[2:].split("=", 1)[0]
    return name == "" or any(word.startswith(name) for word in RESERVED_OPTIONS)


def quote_command(arguments):
    """The command line for `arguments` (a list of texts): each one quoted when it is empty or holds a space or a tab,
    backslashes doubled only where a quote follows them, and every quote escaped. The result is the same as the
    standard library's `subprocess.list2cmdline`, which a test compares it with on many inputs."""
    parts = []
    for argument in arguments:
        pending = []                    # backslashes waiting to see what follows them
        quoted = " " in argument or "\t" in argument or not argument
        piece = ['"'] if quoted else []
        for char in argument:
            if char == "\\":
                pending.append(char)
            elif char == '"':
                piece.append("\\" * len(pending) * 2)
                pending = []
                piece.append('\\"')
            else:
                piece.extend(pending)
                pending = []
                piece.append(char)
        piece.extend(pending)
        if quoted:
            piece.extend(pending)
            piece.append('"')
        parts.append("".join(piece))
    return " ".join(parts)
