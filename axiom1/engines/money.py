"""Money engine: exact currency parsing, addition, grouping, and formatting using Decimal."""
import re
from decimal import Decimal

from ._base import EngineError

SPEC = {
    "name": "money",
    "version": 2,
    "summary": "exact currency parsing, multi-currency support, addition, grouping, and formatting using Decimal",
    "functions": {
        "parse_amount": {
            "args": ["text"],
            "returns": "Decimal amount with 2 decimal places (accepts only $ or USD or no symbol)",
            "io": False,
            "examples": [
                {"args": {"text": "$1,234.50"}, "returns": Decimal("1234.50")},
                {"args": {"text": "1234.5"}, "returns": Decimal("1234.50")},
                {"args": {"text": "USD 12"}, "returns": Decimal("12.00")},
                {"args": {"text": "-$5.00"}, "returns": Decimal("-5.00")},
                {"args": {"text": "($5.00)"}, "returns": Decimal("-5.00")},
                {"args": {"text": "12.00 USD"}, "returns": Decimal("12.00")},
                {"args": {"text": "1,234"}, "returns": Decimal("1234.00")},
                {"args": {"text": "£12.00"}, "refuses": "parse_money"},
                {"args": {"text": "12.00 EUR"}, "refuses": "parse_money"},
                {"args": {"text": "1.234,50"}, "refuses": "European"},
                {"args": {"text": "12,5"}, "refuses": "ambiguous"},
                {"args": {"text": "1.005"}, "refuses": "more than 2 decimal places"},
                {"args": {"text": "abc"}, "refuses": "no numeric amount"},
                {"args": {"text": "$12 USD"}, "refuses": "multiple currency"},
            ],
        },
        "parse_money": {
            "args": ["text"],
            "returns": "dict with amount (Decimal with 2 places) and currency (code or None)",
            "io": False,
            "examples": [
                {"args": {"text": "$1,234.50"}, "returns": {"amount": Decimal("1234.50"), "currency": "USD"}},
                {"args": {"text": "£12.00"}, "returns": {"amount": Decimal("12.00"), "currency": "GBP"}},
                {"args": {"text": "12.00 EUR"}, "returns": {"amount": Decimal("12.00"), "currency": "EUR"}},
                {"args": {"text": "100"}, "returns": {"amount": Decimal("100.00"), "currency": None}},
                {"args": {"text": "-£5.00"}, "returns": {"amount": Decimal("-5.00"), "currency": "GBP"}},
                {"args": {"text": "1.234,50"}, "refuses": "European"},
                {"args": {"text": "1.005"}, "refuses": "more than 2 decimal places"},
            ],
        },
        "add": {
            "args": ["amounts"],
            "returns": "Decimal sum with 2 decimal places",
            "io": False,
            "examples": [
                {"args": {"amounts": [Decimal("10.00"), "$5.50"]}, "returns": Decimal("15.50")},
                {"args": {"amounts": [{"amount": Decimal("10.00"), "currency": "USD"}, {"amount": Decimal("5.00"), "currency": "USD"}]}, "returns": Decimal("15.00")},
                {"args": {"amounts": [{"amount": Decimal("10.00"), "currency": "USD"}, {"amount": Decimal("5.00"), "currency": "GBP"}]}, "refuses": "cannot mix"},
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
                    "args": {
                        "rows": [
                            {"dept": "eng", "cost": {"amount": Decimal("10.00"), "currency": "USD"}},
                            {"dept": "hr", "cost": {"amount": Decimal("5.00"), "currency": "GBP"}},
                        ],
                        "key": "dept",
                        "amount_field": "cost",
                    },
                    "refuses": "cannot mix",
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

_CURRENCY_SYMBOLS = {
    "$": "USD",
    "£": "GBP",
    "€": "EUR",
    "¥": "JPY",
    "₹": "INR",
    "₽": "RUB",
    "₩": "KRW",
}


def parse_money(text):
    """Parse a currency string into a dict with Decimal amount and currency code or None.

    Accepts standard currency symbols ($ £ € ¥ ₹ ₽ ₩) and 3-letter codes (USD, GBP, EUR, etc.),
    negative formats ('-$5.00', '($5.00)'), integers, and Decimals.
    Refuses floats (never float), European comma decimals (e.g. '1.234,50'), ambiguous commas
    (e.g. '12,5'), multiple currency symbols, and precision beyond 2 decimal places (e.g. '1.005').
    """
    if isinstance(text, float):
        raise EngineError(f"floats are not accepted (got {text!r}); pass a string or Decimal")
    if isinstance(text, Decimal):
        if text.as_tuple().exponent < -2:
            raise EngineError(f"more than 2 decimal places in Decimal {text}")
        return {"amount": text.quantize(Decimal("0.01")), "currency": None}
    if isinstance(text, int) and not isinstance(text, bool):
        return {"amount": Decimal(text).quantize(Decimal("0.01")), "currency": None}
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

    detected_currency = None
    if symbols:
        sym = symbols[0]
        detected_currency = _CURRENCY_SYMBOLS.get(sym, sym)
    elif codes:
        detected_currency = codes[0].upper()

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
    return {"amount": val.quantize(Decimal("0.01")), "currency": detected_currency}


def parse_amount(text):
    """Parse a currency amount into a Decimal with exactly 2 decimal places.

    Accepts ONLY standard dollar formats ($ / USD / no symbol).
    Refuses any other currency symbol or code (e.g. £, €, GBP, EUR); use parse_money(text) instead.
    """
    pm = parse_money(text)
    curr = pm["currency"]
    if curr is not None and curr != "USD":
        raise EngineError(f"currency {curr!r} is not USD/$; use parse_money(text) to handle other currencies")
    return pm["amount"]


def _extract_amount_and_currency(item):
    if isinstance(item, float):
        raise EngineError(f"float {item!r} is not accepted; pass Decimal, string, or parse_money dict")
    if isinstance(item, dict):
        if "amount" not in item:
            raise EngineError(f"dict amount must contain 'amount' key, got {item!r}")
        raw_amt = item["amount"]
        curr = item.get("currency")
        if isinstance(raw_amt, float):
            raise EngineError(f"float amount {raw_amt!r} is not accepted in dict")
        if isinstance(raw_amt, str):
            pm = parse_money(raw_amt)
            return pm["amount"], curr or pm["currency"]
        if isinstance(raw_amt, Decimal):
            if raw_amt.as_tuple().exponent < -2:
                raise EngineError(f"amount has more than 2 decimal places: {raw_amt}")
            return raw_amt.quantize(Decimal("0.01")), curr
        if isinstance(raw_amt, int) and not isinstance(raw_amt, bool):
            return Decimal(raw_amt).quantize(Decimal("0.01")), curr
        raise EngineError(f"unsupported amount type {type(raw_amt).__name__} in dict")
    if isinstance(item, Decimal):
        if item.as_tuple().exponent < -2:
            raise EngineError("amount has more than 2 decimal places")
        return item.quantize(Decimal("0.01")), None
    if isinstance(item, int) and not isinstance(item, bool):
        return Decimal(item).quantize(Decimal("0.01")), None
    if isinstance(item, str):
        pm = parse_money(item)
        return pm["amount"], pm["currency"]
    raise EngineError(f"unsupported amount type {type(item).__name__}")


def add(amounts):
    """Sum a list of amounts (Decimals, integers, parseable strings, or parse_money dicts). Never accepts floats.

    Refuses mixing different currencies. Returns Decimal sum with 2 decimal places.
    """
    if not isinstance(amounts, (list, tuple)):
        raise EngineError("amounts must be a list or tuple")

    total = Decimal("0.00")
    currencies = set()
    for a in amounts:
        amt, curr = _extract_amount_and_currency(a)
        if curr is not None:
            currencies.add(curr)
        total += amt

    if len(currencies) > 1:
        raise EngineError(f"cannot mix different currencies: {', '.join(sorted(currencies))}")

    return total.quantize(Decimal("0.01"))


def total_by(rows, key, amount_field):
    """Group rows by key and sum amount_field for each group.

    Accepts Decimals, parseable strings, or parse_money dicts for amount_field.
    Refuses rows missing key or amount_field, and refuses mixing different currencies.
    Returns a dict mapping group key to Decimal sum with 2 decimal places.
    """
    if not isinstance(rows, (list, tuple)):
        raise EngineError("rows must be a list of dicts")
    if not isinstance(key, str) or not key:
        raise EngineError("key must be a non-empty string")
    if not isinstance(amount_field, str) or not amount_field:
        raise EngineError("amount_field must be a non-empty string")

    totals = {}
    currencies = set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise EngineError(f"row {i} must be a dict")
        if key not in row:
            raise EngineError(f"row {i} is missing key {key!r}")
        if amount_field not in row:
            raise EngineError(f"row {i} is missing amount field {amount_field!r}")

        group_val = row[key]
        raw_amount = row[amount_field]
        amt, curr = _extract_amount_and_currency(raw_amount)
        if curr is not None:
            currencies.add(curr)

        totals[group_val] = totals.get(group_val, Decimal("0.00")) + amt

    if len(currencies) > 1:
        raise EngineError(f"cannot mix different currencies in total_by: {', '.join(sorted(currencies))}")

    return {k: v.quantize(Decimal("0.01")) for k, v in totals.items()}


def format_amount(amount):
    """Format an amount as a plain two-place decimal string for CSV (e.g. '1234.50').

    No currency symbols, no thousands separators. Refuses floats.
    """
    if isinstance(amount, float):
        raise EngineError(f"amount {amount!r} is a float; pass Decimal or string to avoid precision loss")
    if isinstance(amount, dict) and "amount" in amount:
        amount = amount["amount"]
    if isinstance(amount, Decimal):
        d = amount.quantize(Decimal("0.01"))
    elif isinstance(amount, int) and not isinstance(amount, bool):
        d = Decimal(amount).quantize(Decimal("0.01"))
    elif isinstance(amount, str):
        d = parse_amount(amount)
    else:
        raise EngineError(f"cannot format amount of type {type(amount).__name__}")

    return f"{d:.2f}"
