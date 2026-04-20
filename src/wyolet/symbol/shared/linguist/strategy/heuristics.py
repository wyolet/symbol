import re

from ..blob import Blob
from ..config.load import load_config
from ..language import Language
from .base import BaseStrategy


class And:
    def __init__(self, patterns):
        self.patterns = patterns

    def match(self, input_data):
        return all(pattern.match(input_data) for pattern in self.patterns)


class AlwaysMatch:
    def match(self, input_data):
        return True


class NegativePattern:
    def __init__(self, pattern):
        self.pattern = pattern

    def match(self, input_data):
        return not self.pattern.match(input_data)


class Heuristic:
    def __init__(self, extensions, rules):
        self.extensions = extensions
        self.rules = rules

    def matches(self, filename, candidates):
        filename = filename.lower()
        candidate_names = [c.name for c in candidates]
        return any(filename.endswith(ext) for ext in self.extensions) and any(
            rule["language"] in candidate_names for rule in self.rules
        )

    def call(self, data):
        for rule in self.rules:
            if rule["pattern"].match(data):
                languages = rule["language"]
                if isinstance(languages, list):
                    return [Language.find_by_name(lang) for lang in languages]
                return [Language.find_by_name(languages)]
        return []


class HeuristicsStrategy(BaseStrategy):
    HEURISTICS_CONSIDER_BYTES = 50 * 1024
    heuristics: list = []

    def __init__(self):
        pass

    def call(self, blob: Blob, candidates: list):
        self.load()
        data = blob.data[: self.HEURISTICS_CONSIDER_BYTES]

        for heuristic in self.heuristics:
            if heuristic.matches(blob.name, candidates):
                return heuristic.call(data) or []

        return []

    @staticmethod
    def load():
        if HeuristicsStrategy.heuristics:
            return

        data = load_config("heuristics")

        named_patterns = {k: HeuristicsStrategy.to_regex(v) for k, v in data.get("named_patterns", {}).items()}

        for disambiguation in data.get("disambiguations", []):
            extensions = disambiguation.get("extensions", [])
            rules = disambiguation.get("rules", [])

            for rule in rules:
                rule["pattern"] = HeuristicsStrategy.parse_rule(named_patterns, rule)

            HeuristicsStrategy.heuristics.append(Heuristic(extensions, rules))

    @staticmethod
    def parse_rule(named_patterns, rule):
        if "and" in rule:
            rules = [HeuristicsStrategy.parse_rule(named_patterns, r) for r in rule["and"]]
            return And(rules)
        elif "pattern" in rule:
            return HeuristicsStrategy.to_regex(rule["pattern"])
        elif "negative_pattern" in rule:
            return NegativePattern(HeuristicsStrategy.to_regex(rule["negative_pattern"]))
        elif "named_pattern" in rule:
            return named_patterns.get(rule["named_pattern"])
        else:
            return AlwaysMatch()

    @staticmethod
    def to_regex(pattern):
        if isinstance(pattern, list):
            return [re.compile(p) for p in pattern]
        return re.compile(pattern)
