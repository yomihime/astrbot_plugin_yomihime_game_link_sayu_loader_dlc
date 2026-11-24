"""Plugin trigger matching on adapter Event objects."""

import re


GAPS = " \t\u3000\u00a0"
RANKS = {
    "fullmatch": 0,
    "command": 1,
    "prefix": 2,
    "suffix": 2,
    "regex": 3,
    "keyword": 4,
    "message": 5,
    "meta": 5,
    "file": 5,
}
_version = 0


def gap_chars():
    return GAPS


def bump_registry_version():
    global _version
    _version += 1


def registry_version():
    return _version


def dispatch_sort_key(trigger, priority):
    length = (
        len(trigger.keyword)
        if trigger.type in {"prefix", "suffix", "command", "fullmatch"}
        else 0
    )
    return (not trigger.prefix, priority, RANKS[trigger.type], -length)


def literal(keyword):
    pieces = []
    previous = ""
    gaps = "[" + GAPS + "]"

    def han(ch):
        return (
            "\u3400" <= ch <= "\u4dbf"
            or "\u4e00" <= ch <= "\u9fff"
            or "\uf900" <= ch <= "\ufaff"
        )

    for char in keyword:
        if char in GAPS:
            if not previous or previous not in GAPS:
                pieces.append(gaps + "+")
        else:
            if (han(previous) and char.isascii() and char.isalnum()) or (
                han(char) and previous.isascii() and previous.isalnum()
            ):
                pieces.append(gaps + "*")
            pieces.append(re.escape(char))
        previous = char
    return "".join(pieces)


class Trigger:
    def __init__(
        self,
        type,
        keyword,
        func,
        prefix="",
        block=False,
        to_me=False,
        aside=None,
        sv=None,
    ):
        self.type, self.keyword, self.func = type, keyword, func
        self.prefix, self.block, self.to_me, self._aside, self.sv = (
            prefix,
            block,
            to_me,
            aside,
            sv,
        )
        self._literal = re.compile(literal(keyword))
        self._suffix = re.compile(literal(keyword) + r"\Z")
        self._flex = any(
            ch in GAPS for ch in keyword
        ) or self._literal.pattern != re.escape(keyword)

    @property
    def aside(self):
        if self._aside is not None:
            return self._aside
        if self.sv is None:
            return True
        return self.sv.pm > 2 if self.sv.aside is None else self.sv.aside

    @aside.setter
    def aside(self, value):
        self._aside = value

    @property
    def head(self):
        return self.prefix + self.keyword

    def trie_head(self):
        return self.head

    def gap_positions(self):
        if self._flex and self.head:
            return frozenset(range(len(self.head) + 1))
        return frozenset([len(self.prefix)]) if self.prefix else frozenset()

    def _body(self, raw):
        text = raw.strip(GAPS)
        return (
            text[len(self.prefix) :].lstrip(GAPS)
            if text.startswith(self.prefix)
            else None
        )

    def check_command(self, ev):
        if self.to_me and not ev.is_tome:
            return False
        mode = self.type
        if mode == "message":
            return True
        if mode == "meta":
            return ev.meta_event_type == self.keyword
        if mode == "file":
            return bool(
                ev.file and ev.file_name and ev.file_name.split(".")[-1] == self.keyword
            )
        raw = ev.raw_text
        body = self._body(raw)
        if body is None:
            return False
        if mode == "keyword":
            return self.keyword in raw.strip(GAPS)
        if mode == "regex":
            return bool(re.findall(self.keyword, body))
        if mode == "suffix":
            if (
                raw.startswith(self.prefix)
                and raw.endswith(self.keyword)
                and raw != self.head
            ):
                return True
            if not self.prefix and not self._flex and raw[:1] not in GAPS:
                return False
            return bool(self._suffix.search(body) and not self._literal.fullmatch(body))
        if mode not in {"fullmatch", "command", "prefix"}:
            return False
        if not self.prefix and not self._flex and raw and raw[0] not in GAPS:
            if mode == "fullmatch":
                return raw == self.head
            if mode == "command":
                return raw.startswith(self.head)
        match = self._literal.match(body)
        if match is None:
            return False
        tail = body[match.end() :]
        if mode == "fullmatch":
            return not tail
        return bool(tail) if mode == "prefix" else True

    async def get_command(self, msg):
        body = self._body(msg.raw_text)
        if body is None:
            body = msg.raw_text
        if self.type == "regex":
            match = re.search(self.keyword, body)
            if match:
                msg.regex_group = match.groups()
                msg.regex_dict = match.groupdict()
                msg.command = "|".join(part or "" for part in msg.regex_group)
            msg.text = "|".join(
                part or "" for part in re.split(self.keyword, msg.raw_text)
            )
        else:
            msg.command = self.keyword
            match = self._literal.match(body)
            msg.text = (
                body[match.end() :] if match else body.replace(self.keyword, "", 1)
            ).strip(GAPS)
        return msg
