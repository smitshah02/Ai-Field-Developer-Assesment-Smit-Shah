import re
import string

REDACTED = "[REDACTED]"

MAX_EMAIL_LENGTH = 330
MAX_DIGIT_PATTERN_LENGTH = 23

EMAIL_CHARS = string.ascii_letters + string.digits + "._%+-@"
DIGIT_SEPARATORS = " -"
DIGIT_PATTERN_CHARS = string.digits + DIGIT_SEPARATORS

PII_RE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,240}\.[A-Za-z]{2,24})"
    r"|(?P<ssn>(?<!\d)\d{3}([- ])\d{2}\3\d{4}(?!\d))"
    r"|(?P<card>(?<!\d)(?:\d{13,19}"
    r"|\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?P<tail>[ -]\d{3})?"
    r"|\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7}"
    r"|\d{4}[ -]\d{6}[ -]\d{4,5})(?!\d))",
    re.ASCII,
)

TRUNCATED_PII_RE = re.compile(
    r"(?:[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]*"
    r"|(?<!\d)\d{3}[- ]\d{2}[- ]\d{0,3}"
    r"|(?<!\d)\d{12,18}"
    r"|(?<!\d)\d{4}[ -]\d{4}[ -]\d{0,4}(?:[ -]\d{0,4}(?:[ -]\d{0,2})?)?"
    r"|(?<!\d)\d{4}[ -]\d{6}[ -]\d{0,4})\Z",
    re.ASCII,
)


def passes_luhn(candidate: str) -> bool:
    digits = [int(char) for char in candidate if char.isdigit()]
    doubled = [sum(divmod(digit * 2, 10)) for digit in digits[-2::-2]]
    return (sum(digits[-1::-2]) + sum(doubled)) % 10 == 0


def card_stop(match: re.Match) -> int | None:
    stops = [match.end()]
    if match["tail"]:
        stops.append(match.start("tail"))
    for stop in stops:
        if passes_luhn(match.string[match.start():stop]):
            return stop
    return None


def find_pii_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    position = 0
    while match := PII_RE.search(text, position):
        stop = card_stop(match) if match.lastgroup == "card" else match.end()
        if stop is None:
            position = match.start() + 1
            continue
        spans.append((match.start(), stop))
        position = stop
    return spans


class StreamRedactor:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> str:
        self._buffer += text
        return self._release_until(self._hold_start())

    def flush(self) -> str:
        return TRUNCATED_PII_RE.sub(REDACTED, self._release_until(len(self._buffer)))

    def _hold_start(self) -> int:
        length = len(self._buffer)
        email_start = len(self._buffer.rstrip(EMAIL_CHARS))
        digit_run = self._buffer[len(self._buffer.rstrip(DIGIT_PATTERN_CHARS)):]
        digit_start = length - len(digit_run.lstrip(DIGIT_SEPARATORS))
        return min(
            max(email_start, length - MAX_EMAIL_LENGTH),
            max(digit_start, length - MAX_DIGIT_PATTERN_LENGTH),
        )

    def _release_until(self, end: int) -> str:
        spans = find_pii_spans(self._buffer)
        for start, stop in spans:
            if start < end < stop:
                end = start
                break

        released = []
        position = 0
        for start, stop in spans:
            if stop > end:
                break
            released.append(self._buffer[position:start])
            released.append(REDACTED)
            position = stop
        released.append(self._buffer[position:end])

        self._buffer = self._buffer[end:]
        return "".join(released)
