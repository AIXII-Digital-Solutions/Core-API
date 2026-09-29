"""How values are written on a certificate — the one place the house style lives."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Sequence

_ONES = ("ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE", "TEN",
         "ELEVEN", "TWELVE", "THIRTEEN", "FOURTEEN", "FIFTEEN", "SIXTEEN", "SEVENTEEN", "EIGHTEEN",
         "NINETEEN")
_TENS = ("", "", "TWENTY", "THIRTY", "FORTY", "FIFTY", "SIXTY", "SEVENTY", "EIGHTY", "NINETY")


def money(amount, currency: Optional[str]) -> str:
    """'USD 62,605,200' — whole amounts without decimals, anything else with two."""
    value = Decimal(str(amount))
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    return f"{currency} {text}" if currency else text


def long_date(value: date) -> str:
    """'05 August 2024' — the form the schedules and the certificates use."""
    return value.strftime("%d %B %Y")


def _integer_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} HUNDRED" + (f" AND {_integer_words(rest)}" if rest else "")
    raise ValueError("percent above 999 cannot be worded")


def percent_number(value) -> str:
    """97.5 -> '97.5', 100 -> '100', 12.250 -> '12.25'."""
    text = format(Decimal(str(value)).normalize(), "f")
    return text


def percent_words(value) -> str:
    """97.5 -> 'NINETY SEVEN AND A HALF PERCENT', 100 -> 'ONE HUNDRED PERCENT',
    12.25 -> 'TWELVE POINT TWO FIVE PERCENT'."""
    d = Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP).normalize()
    whole = int(d)
    fraction = d - whole
    words = _integer_words(whole)
    if fraction == Decimal("0.5"):
        return f"{words} AND A HALF PERCENT"
    if fraction:
        digits = format(fraction, "f").split(".")[1]
        words += " POINT " + " ".join(_ONES[int(ch)] for ch in digits)
    return f"{words} PERCENT"


def join_names(names: Sequence[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def roman(n: int) -> str:
    """1 -> 'i' — the numbering of the contract party and contract lists."""
    table = ((10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
    out = ""
    for value, symbol in table:
        while n >= value:
            out += symbol
            n -= value
    return out
