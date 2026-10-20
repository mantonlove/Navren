"""Exact ordered alias substitution without scanning every registered record ID.

The lookup changes only candidate discovery. Replacement order, NFKC handling,
exclusions and cascading replacements are identical to the sequential contract.
No credential filtering, alias issuance, or source authority is implemented here.
"""
import unicodedata


class AliasMap(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self._raw = {}
        self._normalized = {}
        self._order = {}
        self._next_order = 0
        self.update(*args, **kwargs)

    def __setitem__(self, key, value):
        if key not in self:
            if not isinstance(key, str):
                raise TypeError('alias_key_must_be_text')
            self._order[key] = self._next_order
            self._next_order += 1
            for buckets, text in ((self._raw, key),
                                  (self._normalized, unicodedata.normalize('NFKC', key))):
                buckets.setdefault(len(text), {}).setdefault(text, set()).add(key)
        super().__setitem__(key, value)

    def __delitem__(self, key):
        super().__delitem__(key)
        self._order.pop(key)
        for buckets, text in ((self._raw, key),
                              (self._normalized, unicodedata.normalize('NFKC', key))):
            rows = buckets[len(text)]
            rows[text].remove(key)
            if not rows[text]:
                del rows[text]
            if not rows:
                del buckets[len(text)]

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if len(default) > 1:
            raise TypeError('pop_expected_at_most_two_arguments')
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def popitem(self):
        if not self:
            raise KeyError('popitem_from_empty_dictionary')
        key = next(reversed(self))
        return key, self.pop(key)

    def clear(self):
        super().clear()
        self._raw.clear()
        self._normalized.clear()
        self._order.clear()
        self._next_order = 0

    def __ior__(self, other):
        self.update(other)
        return self

    def __deepcopy__(self, memo):
        from copy import deepcopy
        result = type(self)()
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result

    def _candidates(self, text, buckets, public, nonprose, after):
        found = set()
        for length, rows in buckets.items():
            if length > len(text):
                continue
            for start in range(len(text) - length + 1):
                for original in rows.get(text[start:start + length], ()):
                    if original in public or original in nonprose:
                        continue
                    rank = (-len(original), self._order[original])
                    if after is None or rank > after:
                        found.add(original)
        return found

    def replace_text(self, text, public, nonprose, *, normalize):
        buckets = self._normalized if normalize else self._raw
        after = None
        while True:
            candidates = self._candidates(text, buckets, public, nonprose, after)
            if not candidates:
                return text
            original = min(candidates, key=lambda key: (-len(key), self._order[key]))
            after = (-len(original), self._order[original])
            needle = unicodedata.normalize('NFKC', original) if normalize else original
            # Re-scan after replacement to preserve cascading matches and
            # matches created across a replacement boundary. Earlier-ranked
            # aliases remain consumed exactly as in the original ordered loop.
            text = text.replace(needle, self[original])
