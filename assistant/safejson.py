"""JSON from outside the app, read without letting its shape crash it.

What arrives from a server - the models' replies and their streams, the model lists, the
web search, the attestation services, nano-gpt's relay - and every file the app reads
that someone could have written (config.json edited by hand, the model-list cache, a
dictionary's or the encyclopedia's data, a desktop program's answer) is parsed here.
json.loads recurses into every list and object: nested a thousand levels it raises
RecursionError, which nothing expects and which, raised on a worker, stopped it with a
traceback in place of the error the window says. Nested less deep than that, but past
what anything the app keeps holds, it was taken in, and a deep copy or a save of it
recursed too far later, far from where it came in.

So loads() refuses anything nested past MAX_NESTING levels - and anything json.loads
could not read for its depth - as a NestedTooDeep, a json.JSONDecodeError, which every
caller already takes for "that wasn't JSON". The same module as DA Media Browser's
(core/safejson.py), from SealedLore's storage/atomic.nests_deeper (Nov 2026); netguard's
session reads every answer's JSON through it (Response.json).
"""

import json

# Deeper than anything the app keeps, or reads from a service, nests: a
# settings file is three levels, an attestation a dozen.
MAX_NESTING = 200


class NestedTooDeep(json.JSONDecodeError):
    """JSON nested past MAX_NESTING levels: refused, as JSON that couldn't be read."""

    def __init__(self):
        super().__init__(f"it is nested more than {MAX_NESTING} levels deep, deeper "
                         f"than anything this app reads", "", 0)

    def __str__(self):
        return self.msg         # no "line 1 column 1": the place says nothing here


def nests_deeper(data, limit=MAX_NESTING):
    """Whether `data` (as json.loads made it) nests past `limit` levels.
    Walked, never recursed into."""
    stack = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        stack.extend((child, depth + 1) for child in children
                     if isinstance(child, (dict, list)))
    return False


def loads(text):
    """json.loads(`text`) - str, bytes or bytearray - refusing JSON nested past
    MAX_NESTING levels, or too deep for json.loads to read at all, with
    NestedTooDeep (a json.JSONDecodeError)."""
    try:
        data = json.loads(text)
    except RecursionError:
        raise NestedTooDeep() from None
    if nests_deeper(data):
        raise NestedTooDeep()
    return data


def load(handle):
    """loads() of what an open file holds."""
    return loads(handle.read())
