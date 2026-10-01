"""Money engine: exact currency parsing, addition, grouping, and formatting using Decimal."""
import re
from decimal import Decimal

from ._base import EngineError

SPEC = {
    "name": "money",
    "version": 1,
    "summary": "exact currency parsing, addition, grouping, and formatting using Decimal",
    "functions": {
        "parse_amount": {
            "args": ["text"],
            "returns": "Decimal amount with 2 decimal places",
            "io": False,
            "examples": [
                {"args": {"text": "$1,234.50"}, "returns": Decimal("1234.50")},
                {"args": {"text": "1234.5"}, "returns": Decimal("1234.50")},
                {"args": {"text": "USD 12"}, "returns": Decimal("12.00")},
                {"args": {"text": "-$5.00"}, "returns": Decimal("-5.00")},
                {"args": {"text": "($5.00)"}, "returns": Decimal("-5.00")},
                {"args": {"text": "12.00 USD"}, "returns": Decimal("12.00")},
                {"args": {"text": "1,234"}, "returns": Decimal("1234.00")},
                {"args": {"text": "1.234,50"}, "refuses": "European"},
                {"args": {"text": "12,5"}, "refuses": "ambiguous"},
                {"args": {"text": "1.005"}, "refuses": "more than 2 decimal places"},
                {"args": {"text": "abc"}, "refuses": "no numeric amount"},
                {"args": {"text": "$12 USD"}, "refuses": "multiple currency"},
            ],
        },
        "add": {
            "args": ["amounts"],
            "returns": "Decimal sum with 2 decimal places",
            "io": False,
            "examples": [
                {"args": {"amounts": [Decimal("10.00"), "$5.50"]}, "returns": Decimal("15.50")},
                {"args": {"amounts": ["$1,200.00", "50.00", "USD 10"]}, "returns": Decimal("1260.00")},
                {"args": {"amounts": [1.5]}, "refuses": "float"},
            ],
        },
        "total_by": {
            "args": ["rows", "key", "amount_field"],
            "returns": "dict mapping group key to Decimal sum with 2 decimal places",
            "io": False,
            "examples": [
                {
                    "args": {
                        "rows": [
                            {"dept": "eng", "cost": "$10.00"},
                            {"dept": "eng", "cost": "20.50"},
                            {"dept": "hr", "cost": "5"},
                        ],
                        "key": "dept",
                        "amount_field": "cost",
                    },
                    "returns": {"eng": Decimal("30.50"), "hr": Decimal("5.00")},
                },
                {
                    "args": {"rows": [{"cost": "$10.00"}], "key": "dept", "amount_field": "cost"},
                    "refuses": "missing",
                },
            ],
        },
        "format_amount": {
            "args": ["amount"],
            "returns": "plain two-place decimal string for CSV",
            "io": False,
            "examples": [
                {"args": {"amount": Decimal("1234.50")}, "returns": "1234.50"},
                {"args": {"amount": "$1,234.50"}, "returns": "1234.50"},
                {"args": {"amount": Decimal("-5.00")}, "returns": "-5.00"},
                {"args": {"amount": 12.34}, "refuses": "float"},
            ],
        },
    },
}


def parse_amount(text):
    """Parse a currency amount into a Decimal with exactly 2 decimal places.

    Accepts strings with standard currency symbols or codes (e.g. '$1,234.50', 'USD 12',
    '12.00 USD'), negative formats (e.g. '-$5.00', '($5.00)'), integers, and Decimals.
    Refuses floats (never float), European comma decimals (e.g. '1.234,50'), ambiguous commas
    (e.g. '12,5'), multiple currency symbols, and precision beyond 2 decimal places (e.g. '1.005').
    """
    if isinstance(text, float):
        raise EngineError(f"floats are not accepted (got {text!r}); pass a string or Decimal")
    if isinstance(text, Decimal):
        if text.as_tuple().exponent < -2:
            raise EngineError(f"more than 2 decimal places in Decimal {text}")
        return text.quantize(Decimal("0.01"))
    if isinstance(text, int) and not isinstance(text, bool):
        return Decimal(text).quantize(Decimal("0.01"))
    if not isinstance(text, str) or not text.strip():
        raise EngineError("amount text must be a non-empty string")

    s = text.strip()

    is_negative = False
    if s.startswith("(") and s.endswith(")"):
        is_negative = True
        s = s[1:-1].strip()

    if "-" in s:
        if s.count("-") > 1:
            raise EngineError(f"multiple minus signs in amount {text!r}")
        is_negative = True
        s = s.replace("-", "").strip()

    # Detect currency symbols and 3-letter codes
    symbols = re.findall(r"[$£€¥₹₽₩]", s)
    codes = re.findall(r"\b[A-Za-z]{3}\b", s)

    if len(symbols) + len(codes) > 1:
        raise EngineError(f"multiple currency symbols or codes in {text!r}")

    s = re.sub(r"[$£€¥₹₽₩]", "", s).strip()
    s = re.sub(r"\b[A-Za-z]{3}\b", "", s).strip()

    if re.search(r"[A-Za-z]", s):
        raise EngineError(f"unrecognized text in amount {text!r}")
    if not s:
        raise EngineError(f"no numeric amount found in {text!r}")
    if re.search(r"[^0-9.,]", s):
        raise EngineError(f"invalid characters in amount {text!r}")

    # Inspect comma and dot usage
    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            raise EngineError(f"European format {text!r} with comma decimal is ambiguous; use '.' for decimal")
        parts = s.split(".")
        if len(parts) > 2:
            raise EngineError(f"multiple decimal points in amount {text!r}")
        int_part, dec_part = parts
        if len(dec_part) > 2:
            raise EngineError(f"more than 2 decimal places in {text!r}")
        if len(dec_part) == 0:
            raise EngineError(f"missing digits after decimal point in {text!r}")
        groups = int_part.split(",")
        if not groups[0].isdigit() or len(groups[0]) > 3 or len(groups[0]) == 0:
            raise EngineError(f"invalid thousands grouping in {text!r}")
        for grp in groups[1:]:
            if len(grp) != 3 or not grp.isdigit():
                raise EngineError(f"invalid thousands grouping in {text!r}")
        clean_num = int_part.replace(",", "") + "." + dec_part

    elif has_comma and not has_dot:
        groups = s.split(",")
        if not groups[0].isdigit() or len(groups[0]) > 3 or len(groups[0]) == 0:
            raise EngineError(f"invalid thousands grouping in {text!r}")
        for grp in groups[1:]:
            if len(grp) != 3 or not grp.isdigit():
                raise EngineError(f"ambiguous comma in amount {text!r}; group after comma must have exactly 3 digits")
        clean_num = s.replace(",", "") + ".00"

    elif has_dot and not has_comma:
        parts = s.split(".")
        if len(parts) > 2:
            raise EngineError(f"multiple dots in amount {text!r}; European dot thousands separator is not accepted")
        int_part, dec_part = parts
        if not int_part.isdigit():
            raise EngineError(f"invalid integer part in {text!r}")
        if len(dec_part) > 2:
            raise EngineError(f"more than 2 decimal places in {text!r}")
        if len(dec_part) == 0:
            raise EngineError(f"missing digits after decimal point in {text!r}")
        clean_num = int_part + "." + dec_part

    else:
        if not s.isdigit():
            raise EngineError(f"invalid numeric amount in {text!r}")
        clean_num = s + ".00"

    val = Decimal(clean_num)
    if is_negative and val != 0:
        val = -val
    return val.quantize(Decimal("0.01"))


def add(amounts):
    """Sum a list of amounts (Decimals, integers, or parseable strings). Never accepts floats.

    Returns Decimal sum with 2 decimal places.
    """
    if not isinstance(amounts, (list, tuple)):
        raise EngineError("amounts must be a list or tuple")

    total = Decimal("0.00")
    for a in amounts:
        if isinstance(a, float):
            raise EngineError(f"float {a!r} is not accepted in add(); pass Decimal or string")
        if isinstance(a, Decimal):
            if a.as_tuple().exponent < -2:
                raise EngineError("amount has more than 2 decimal places")
            total += a.quantize(Decimal("0.01"))
        elif isinstance(a, int) and not isinstance(a, bool):
            total += Decimal(a).quantize(Decimal("0.01"))
        elif isinstance(a, str):
            total += parse_amount(a)
        else:
            raise EngineError(f"unsupported amount type {type(a).__name__} in add()")

    return total.quantize(Decimal("0.01"))


def total_by(rows, key, amount_field):
    """Group rows by key and sum amount_field for each group.

    Returns a dict mapping group key to Decimal sum with 2 decimal places.
    Refuses rows missing key or amount_field.
    """
    if not isinstance(rows, (list, tuple)):
        raise EngineError("rows must be a list of dicts")
    if not isinstance(key, str) or not key:
        raise EngineError("key must be a non-empty string")
    if not isinstance(amount_field, str) or not amount_field:
        raise EngineError("amount_field must be a non-empty string")

    totals = {}
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise EngineError(f"row {i} must be a dict")
        if key not in row:
            raise EngineError(f"row {i} is missing key {key!r}")
        if amount_field not in row:
            raise EngineError(f"row {i} is missing amount field {amount_field!r}")

        group_val = row[key]
        raw_amount = row[amount_field]

        if isinstance(raw_amount, float):
            raise EngineError(f"row {i} amount {raw_amount!r} is a float; pass Decimal or string")
        if isinstance(raw_amount, Decimal):
            parsed = raw_amount.quantize(Decimal("0.01"))
        elif isinstance(raw_amount, int) and not isinstance(raw_amount, bool):
            parsed = Decimal(raw_amount).quantize(Decimal("0.01"))
        elif isinstance(raw_amount, str):
            parsed = parse_amount(raw_amount)
        else:
            raise EngineError(f"row {i} amount {raw_amount!r} has invalid type {type(raw_amount).__name__}")

        totals[group_val] = totals.get(group_val, Decimal("0.00")) + parsed

    return {k: v.quantize(Decimal("0.01")) for k, v in totals.items()}


def format_amount(amount):
    """Format an amount as a plain two-place decimal string for CSV (e.g. '1234.50').

    No currency symbols, no thousands separators. Refuses floats.
    """
    if isinstance(amount, float):
        raise EngineError(f"amount {amount!r} is a float; pass Decimal or string to avoid precision loss")
    if isinstance(amount, Decimal):
        d = amount.quantize(Decimal("0.01"))
    elif isinstance(amount, int) and not isinstance(amount, bool):
        d = Decimal(amount).quantize(Decimal("0.01"))
    elif isinstance(amount, str):
        d = parse_amount(amount)
    else:
        raise EngineError(f"cannot format amount of type {type(amount).__name__}")

    return f"{d:.2f}"
