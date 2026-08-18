"""
Money helpers.

Everything in the database is stored as INTEGER cents to avoid floating-point
errors. These helpers convert to/from display strings.

We default to USD-style formatting ("$1,234.56"). Currency localization isn't
in v1 scope — adding it later is a single function swap.
"""
from __future__ import annotations


def format_cents(cents: int | None, *, with_symbol: bool = True) -> str:
    """
    Format an integer cents value as a human-readable amount.

    >>> format_cents(123456)
    '$1,234.56'
    >>> format_cents(-500)
    '-$5.00'
    >>> format_cents(0)
    '$0.00'
    """
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    abs_cents = abs(int(cents))
    dollars, remaining = divmod(abs_cents, 100)
    # Group the integer part with commas.
    whole = f"{dollars:,}"
    body = f"{whole}.{remaining:02d}"
    if with_symbol:
        return f"{sign}${body}"
    return f"{sign}{body}"


def parse_dollars(value: str) -> int:
    """
    Parse a user-entered dollar string into cents.

    Accepts inputs like "1234.56", "$1,234.56", "1234". Negative values are
    allowed (use for refunds/credits). Raises ValueError on garbage.
    """
    if value is None:
        raise ValueError("amount required")
    s = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
    if not s:
        raise ValueError("amount required")
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    if s.count(".") > 1:
        raise ValueError(f"invalid amount: {value!r}")
    if "." in s:
        whole, _, frac = s.partition(".")
        if not whole:
            whole = "0"
        if len(frac) > 2:
            # Truncate (don't round) extra digits.
            frac = frac[:2]
        elif len(frac) < 2:
            frac = frac.ljust(2, "0")
        if not whole.isdigit() or not frac.isdigit():
            raise ValueError(f"invalid amount: {value!r}")
        cents = int(whole) * 100 + int(frac)
    else:
        if not s.isdigit():
            raise ValueError(f"invalid amount: {value!r}")
        cents = int(s) * 100
    return -cents if negative else cents
