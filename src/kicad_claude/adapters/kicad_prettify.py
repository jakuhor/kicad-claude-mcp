"""A port of KiCAD's own s-expression pretty-printer.

Line for line from `common/io/kicad/kicad_io_utils.cpp::KICAD_FORMAT::Prettify`
(KiCAD 10, commit 9ffcbf13). It is a character-level text-to-text pass: hand it
a well-formed s-expression with any whitespace and it returns the layout KiCAD
writes.

Porting it replaces a set of measured rules that got 66 of KiCAD 10's 78 demo
schematics byte-identical. The rules were close but could not be right: the one
that eluded measurement — how `(members ...)` wraps inside `(bus_alias ...)` —
is not a line-length budget at all. KiCAD breaks when the column *before* the
next token has already reached `CONSECUTIVE_TOKEN_WRAP_THRESHOLD`, so the line
ends wherever that token happens to end. No single maximum width describes it,
which is why the measurement found none (P6).

The two numbers that matter:

- `XY_SPECIAL_CASE_COLUMN_LIMIT = 99` — consecutive `(xy ...)` lists pack onto
  one line until this column.
- `CONSECUTIVE_TOKEN_WRAP_THRESHOLD = 72` — any other run of tokens inside one
  list wraps past this column.

`FORMAT_MODE.NORMAL` is what KiCAD uses for schematics and boards;
`m_CompactSave` (off by default) switches it to `COMPACT_TEXT_PROPERTIES`.
"""

from __future__ import annotations

from enum import Enum

QUOTE_CHAR = '"'
INDENT_CHAR = "\t"
INDENT_SIZE = 1

# To visually compress PCB files, long runs of (xy ...) lists stay on one line
# until this column.
XY_SPECIAL_CASE_COLUMN_LIMIT = 99

# Whitespace inside a list past this column becomes a newline plus one more
# level of indentation. This is what wraps image data and group member lists.
CONSECUTIVE_TOKEN_WRAP_THRESHOLD = 72

# Lists kept on one line in COMPACT_TEXT_PROPERTIES mode.
SHORT_FORM_TOKENS = frozenset(
    {"font", "stroke", "fill", "teardrop", "offset", "rotate", "scale"}
)


class FormatMode(Enum):
    NORMAL = "normal"
    COMPACT_TEXT_PROPERTIES = "compact_text_properties"
    LIBRARY_TABLE = "library_table"


def _is_whitespace(ch: str) -> bool:
    return ch in (" ", "\t", "\n", "\r")


def prettify(source: str, mode: FormatMode = FormatMode.NORMAL) -> str:
    """Lay `source` out the way KiCAD does. Returns the formatted text.

    Faithful to the C++: same cursor walk, same state flags, same column
    arithmetic. Keep it that way — the value here is that it is not a
    reinterpretation.
    """
    text_special_case = mode is FormatMode.COMPACT_TEXT_PROPERTIES
    lib_special_case = mode is FormatMode.LIBRARY_TABLE

    formatted: list[str] = []

    list_depth = 0
    lib_depth = 0
    last_non_whitespace = ""
    in_quote = False
    has_inserted_space = False
    in_multi_line_list = False
    in_xy = False
    in_short_form = False
    in_lib_row = False
    short_form_depth = 0
    column = 0
    backslash_count = 0

    n = len(source)

    def next_non_whitespace(i: int) -> str:
        while i < n and _is_whitespace(source[i]):
            i += 1
        return source[i] if i < n else ""

    def is_xy(i: int) -> bool:
        return source[i + 1 : i + 4] == "xy "

    def _word_after(i: int) -> str:
        j = i + 1
        out = []
        while j < n and source[j].isalpha():
            out.append(source[j])
            j += 1
        return "".join(out)

    def is_short_form(i: int) -> bool:
        return _word_after(i) in SHORT_FORM_TOKENS

    def is_lib(i: int) -> bool:
        return _word_after(i) == "lib"

    cursor = 0
    while cursor < n:
        ch = source[cursor]
        nxt = next_non_whitespace(cursor)

        if _is_whitespace(ch) and not in_quote:
            if (
                not has_inserted_space  # only one space between chars
                and list_depth > 0  # no spaces in the outer list
                and last_non_whitespace != "("  # none right after a list opens
                and nxt != ")"  # none right before a list closes
                and nxt != "("  # none before a newline
            ):
                if in_xy or column < CONSECUTIVE_TOKEN_WRAP_THRESHOLD:
                    formatted.append(" ")
                    column += 1
                elif in_short_form or in_lib_row:
                    formatted.append(" ")
                else:
                    formatted.append("\n" + INDENT_CHAR * (list_depth * INDENT_SIZE))
                    column = list_depth * INDENT_SIZE
                    in_multi_line_list = True

                has_inserted_space = True
        else:
            has_inserted_space = False

            if ch == "(" and not in_quote:
                current_is_xy = is_xy(cursor)
                current_is_short_form = text_special_case and is_short_form(cursor)
                current_is_lib = lib_special_case and is_lib(cursor)

                if not formatted:
                    formatted.append("(")
                    column += 1
                elif in_xy and current_is_xy and column < XY_SPECIAL_CASE_COLUMN_LIMIT:
                    # List-of-points special case.
                    formatted.append(" (")
                    column += 2
                elif in_short_form or in_lib_row:
                    formatted.append(" (")
                    column += 2
                else:
                    formatted.append(
                        "\n" + INDENT_CHAR * (list_depth * INDENT_SIZE) + "("
                    )
                    column = list_depth * INDENT_SIZE + 1

                in_xy = current_is_xy

                if current_is_short_form:
                    in_short_form = True
                    short_form_depth = list_depth
                elif current_is_lib:
                    in_lib_row = True
                    lib_depth = list_depth

                list_depth += 1

            elif ch == ")" and not in_quote:
                if list_depth > 0:
                    list_depth -= 1

                if in_short_form:
                    formatted.append(")")
                    column += 1
                elif in_lib_row and list_depth == lib_depth:
                    formatted.append(")")
                    in_lib_row = False
                elif last_non_whitespace == ")" or in_multi_line_list:
                    formatted.append(
                        "\n" + INDENT_CHAR * (list_depth * INDENT_SIZE) + ")"
                    )
                    column = list_depth * INDENT_SIZE + 1
                    in_multi_line_list = False
                else:
                    formatted.append(")")
                    column += 1

                if short_form_depth == list_depth:
                    in_short_form = False
                    short_form_depth = 0

            else:
                # A quote ends the string only when an even number of
                # backslashes precedes it — `\\"` closes, `\"` does not.
                if ch == "\\":
                    backslash_count += 1
                elif ch == QUOTE_CHAR and (backslash_count & 1) == 0:
                    in_quote = not in_quote

                if ch != "\\":
                    backslash_count = 0

                formatted.append(ch)
                column += 1

            last_non_whitespace = ch

        cursor += 1

    # Trailing newline, for POSIX and for clean git diffs.
    formatted.append("\n")
    return "".join(formatted)
