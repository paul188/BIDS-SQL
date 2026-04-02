"""
rules.py
--------
BIDS dataset discovery rules, modelled after the Rule/RuleSet pattern
from idiscore (see rules_idiscore.py).

Each Rule encapsulates a single structural check on a candidate directory.
A RuleSet groups rules and evaluates them all at once, returning a structured
list of RuleResults rather than bare strings.

Severity levels:
  HARD — a failed hard rule immediately disqualifies the directory (skipped).
  SOFT — a failed soft rule is recorded as a warning; the dataset is still indexed.

Usage::

    from rules import DEFAULT_BIDS_RULESET

    qualifies, results = DEFAULT_BIDS_RULESET.evaluate(Path("/data/my_study"))
    for r in results:
        print(r)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional


# ─── Severity & Result ────────────────────────────────────────────────────────

class Severity(Enum):
    """Controls whether a failing rule blocks indexing or only warns."""
    HARD = "hard"
    SOFT = "soft"


@dataclass
class RuleResult:
    """Outcome of evaluating a single BIDSRule against a directory."""
    rule_name: str
    passed: bool
    severity: Severity
    message: str

    @property
    def is_blocker(self) -> bool:
        """True when this result should prevent the dataset from being indexed."""
        return not self.passed and self.severity == Severity.HARD

    def __str__(self) -> str:
        status = "PASS" if self.passed else ("FAIL" if self.severity == Severity.HARD else "WARN")
        return f"[{status}] {self.rule_name}: {self.message}"


# ─── Rule ─────────────────────────────────────────────────────────────────────

class BIDSRule:
    """
    A single structural check applied to a candidate BIDS directory.

    Analogous to `Rule` in rules_idiscore.py — it pairs an identifier
    (the rule name / what is being checked) with an operation (the callable
    that does the actual check).

    Parameters
    ----------
    name:
        Short machine-readable identifier, e.g. ``"has_description_json"``.
    description:
        Human-readable explanation of what this rule checks.
    severity:
        HARD rules block indexing on failure; SOFT rules only warn.
    check:
        Callable ``(Path) -> (passed: bool, message: str)``.
    """

    def __init__(
        self,
        name: str,
        description: str,
        severity: Severity,
        check: Callable[[Path], tuple[bool, str]],
    ) -> None:
        self.name = name
        self.description = description
        self.severity = severity
        self._check = check

    def evaluate(self, path: Path) -> RuleResult:
        """Run the check against `path` and return a RuleResult."""
        passed, message = self._check(path)
        return RuleResult(
            rule_name=self.name,
            passed=passed,
            severity=self.severity,
            message=message,
        )

    def __str__(self) -> str:
        return f"{self.name} [{self.severity.value.upper()}]"

    def as_human_readable(self) -> str:
        return f"{self.name} [{self.severity.value.upper()}] — {self.description}"


# ─── RuleSet ──────────────────────────────────────────────────────────────────

class BIDSRuleSet:
    """
    A named collection of BIDSRules.

    Analogous to `RuleSet` in rules_idiscore.py.  Evaluates all contained
    rules against a path and aggregates the results.  Hard rules are always
    evaluated before soft rules so that blockers are surfaced first.

    Parameters
    ----------
    rules:
        Iterable of BIDSRule objects.
    name:
        Human-readable label for this set (used in logs and repr).
    """

    def __init__(self, rules: Iterable[BIDSRule], name: str = "BIDSRuleSet") -> None:
        # Store hard rules first so blockers surface before warnings
        all_rules = list(rules)
        self._hard_rules = [r for r in all_rules if r.severity == Severity.HARD]
        self._soft_rules = [r for r in all_rules if r.severity == Severity.SOFT]
        self.name = name

    @property
    def rules(self) -> list[BIDSRule]:
        """All rules in this set, hard rules first."""
        return self._hard_rules + self._soft_rules

    @property
    def hard_rules(self) -> list[BIDSRule]:
        return list(self._hard_rules)

    @property
    def soft_rules(self) -> list[BIDSRule]:
        return list(self._soft_rules)

    def evaluate(self, path: Path) -> tuple[bool, list[RuleResult]]:
        """
        Evaluate all rules against `path`.

        Returns
        -------
        qualifies : bool
            False if any HARD rule failed (dataset should be skipped).
        results : list[RuleResult]
            One result per rule, in hard-first order.

        Notes
        -----
        Evaluation short-circuits after the first blocking (HARD + failed) result
        so that later rules are not run against a directory that already can't
        possibly qualify.
        """
        results: list[RuleResult] = []

        for rule in self._hard_rules:
            result = rule.evaluate(path)
            results.append(result)
            if result.is_blocker:
                # No point running remaining rules on an unqualified path
                return False, results

        for rule in self._soft_rules:
            results.append(rule.evaluate(path))

        qualifies = not any(r.is_blocker for r in results)
        return qualifies, results

    def get_rule(self, name: str) -> Optional[BIDSRule]:
        """Return the rule with the given name, or None if not found."""
        for rule in self.rules:
            if rule.name == name:
                return rule
        return None

    def remove(self, name: str) -> None:
        """
        Remove a rule by name.

        Raises
        ------
        KeyError
            If no rule with that name exists in this set.
        """
        for collection in (self._hard_rules, self._soft_rules):
            for rule in collection:
                if rule.name == name:
                    collection.remove(rule)
                    return
        raise KeyError(f"No rule named '{name}' in {self}")

    def as_human_readable_list(self) -> str:
        """All rules formatted for display, hard rules first."""
        return "\n".join(r.as_human_readable() for r in self.rules)

    def __str__(self) -> str:
        return f'BIDSRuleSet "{self.name}" ({len(self._hard_rules)} hard, {len(self._soft_rules)} soft)'

    def __len__(self) -> int:
        return len(self._hard_rules) + len(self._soft_rules)

    def __repr__(self) -> str:
        return (
            f"BIDSRuleSet(name={self.name!r}, "
            f"hard={len(self._hard_rules)}, soft={len(self._soft_rules)})"
        )


# ─── Check functions ──────────────────────────────────────────────────────────
# Each function has signature (Path) -> (bool, str) and is kept private;
# public access is through the pre-built BIDSRule instances below.

def _has_description_json(path: Path) -> tuple[bool, str]:
    if (path / "dataset_description.json").exists():
        return True, "dataset_description.json found"
    return False, "Missing dataset_description.json"


def _description_is_valid_json(path: Path) -> tuple[bool, str]:
    desc_file = path / "dataset_description.json"
    if not desc_file.exists():
        return False, "dataset_description.json not present (checked by prior rule)"
    try:
        content = json.loads(desc_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"dataset_description.json parse error: {exc}"
    if not isinstance(content, dict):
        return False, "dataset_description.json must be a JSON object, not a list or scalar"
    return True, "dataset_description.json is valid JSON"


def _has_name_field(path: Path) -> tuple[bool, str]:
    try:
        desc = json.loads((path / "dataset_description.json").read_text(encoding="utf-8"))
    except Exception:
        return False, "Cannot read dataset_description.json"
    if "Name" in desc:
        return True, f"Name = '{desc['Name']}'"
    return False, "dataset_description.json missing required 'Name' field"


def _has_bids_version(path: Path) -> tuple[bool, str]:
    try:
        desc = json.loads((path / "dataset_description.json").read_text(encoding="utf-8"))
    except Exception:
        return False, "Cannot read dataset_description.json"
    if "BIDSVersion" in desc:
        return True, f"BIDSVersion = '{desc['BIDSVersion']}'"
    return False, "dataset_description.json missing required 'BIDSVersion' field"


def _has_subject_directories(path: Path) -> tuple[bool, str]:
    subject_dirs = [d for d in path.iterdir() if d.is_dir() and d.name.startswith("sub-")]
    n = len(subject_dirs)
    if n:
        return True, f"{n} subject director{'y' if n == 1 else 'ies'} found"
    return False, "No subject directories (sub-XX/) found — dataset may be empty"


def _has_participants_tsv(path: Path) -> tuple[bool, str]:
    if (path / "participants.tsv").exists():
        return True, "participants.tsv present"
    return False, "participants.tsv not found (recommended for multi-subject datasets)"


def _has_readme(path: Path) -> tuple[bool, str]:
    for candidate in ("README", "README.md", "README.txt", "README.rst"):
        if (path / candidate).exists():
            return True, f"{candidate} present"
    return False, "No README file found (recommended)"


def _not_inside_derivatives(path: Path) -> tuple[bool, str]:
    if "derivatives" in path.parts:
        return False, "Path is inside a 'derivatives/' subtree — this is a derivative dataset"
    return True, "Dataset is not nested inside a derivatives folder"


# ─── Pre-built rule instances ─────────────────────────────────────────────────

RULE_HAS_DESCRIPTION_JSON = BIDSRule(
    name="has_description_json",
    description="Directory must contain a dataset_description.json file",
    severity=Severity.HARD,
    check=_has_description_json,
)

RULE_DESCRIPTION_IS_VALID_JSON = BIDSRule(
    name="description_json_is_valid",
    description="dataset_description.json must be parseable as a JSON object",
    severity=Severity.HARD,
    check=_description_is_valid_json,
)

RULE_HAS_NAME_FIELD = BIDSRule(
    name="has_name_field",
    description="dataset_description.json must contain the required 'Name' field",
    severity=Severity.SOFT,
    check=_has_name_field,
)

RULE_HAS_BIDS_VERSION = BIDSRule(
    name="has_bids_version",
    description="dataset_description.json must contain the required 'BIDSVersion' field",
    severity=Severity.SOFT,
    check=_has_bids_version,
)

RULE_HAS_SUBJECT_DIRECTORIES = BIDSRule(
    name="has_subject_directories",
    description="Dataset root must contain at least one sub-XX/ directory",
    severity=Severity.SOFT,
    check=_has_subject_directories,
)

RULE_HAS_PARTICIPANTS_TSV = BIDSRule(
    name="has_participants_tsv",
    description="A participants.tsv file is recommended at the dataset root",
    severity=Severity.SOFT,
    check=_has_participants_tsv,
)

RULE_HAS_README = BIDSRule(
    name="has_readme",
    description="A README file is recommended at the dataset root",
    severity=Severity.SOFT,
    check=_has_readme,
)

RULE_NOT_INSIDE_DERIVATIVES = BIDSRule(
    name="not_inside_derivatives",
    description="Dataset should not be nested inside a derivatives/ folder",
    severity=Severity.SOFT,
    check=_not_inside_derivatives,
)


# ─── Pre-built rule sets ──────────────────────────────────────────────────────

HARD_RULESET = BIDSRuleSet(
    rules=[
        RULE_HAS_DESCRIPTION_JSON,
        RULE_DESCRIPTION_IS_VALID_JSON,
    ],
    name="BIDS Hard Rules",
)

SOFT_RULESET = BIDSRuleSet(
    rules=[
        RULE_HAS_NAME_FIELD,
        RULE_HAS_BIDS_VERSION,
        RULE_HAS_SUBJECT_DIRECTORIES,
        RULE_HAS_PARTICIPANTS_TSV,
        RULE_HAS_README,
        RULE_NOT_INSIDE_DERIVATIVES,
    ],
    name="BIDS Soft Rules",
)

# The default ruleset used by the discovery pipeline
DEFAULT_BIDS_RULESET = BIDSRuleSet(
    rules=[*HARD_RULESET.rules, *SOFT_RULESET.rules],
    name="Default BIDS Discovery Rules",
)
