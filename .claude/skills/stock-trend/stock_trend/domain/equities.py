"""Pure A-share identifier rules used by scanners and replays."""


def is_a_share(code):
    if not code or not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        return False
    return code.startswith(("6", "0", "3")) and code[:2] not in ("50", "51", "55", "56", "58", "15", "16", "18")


def resolve_ts_code(code):
    if len(code) != 6 or not code.isdigit():
        return code
    return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
