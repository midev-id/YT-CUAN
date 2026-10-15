import random
import re

_INNER = re.compile(r"\{([^{}]*)\}")


def spin(text: str, rng=random) -> str:
    """Resolve {a|b|c} spintax, innermost groups first so nesting works."""
    while True:
        new = _INNER.sub(lambda m: rng.choice(m.group(1).split("|")), text)
        if new == text:
            return new
        text = new
