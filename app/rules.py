"""The program's rules as plain functions, each tied to the section of the Act it implements.

These functions decide nothing on their own: they compute what the Act requires, and an examiner,
a member vote or a published procedure makes the decision on the record.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, timedelta

# Periods fixed by the Act
COMMENT_PERIOD_DAYS = 60          # s.2(c): at least 60 days for evidence and responses
EVENT_DECISION_DAYS = 180         # s.2(c): reasoned decision within 180 days
CLAIM_FILING_YEARS = 2            # s.3(d)
CLAIM_DECISION_DAYS = 90          # s.3(d)
HEARING_REQUEST_DAYS = 60         # s.3(d)
ENROLLMENT_DAYS = 180             # s.4(a)
MIN_FOUNDING_UNITS = 2            # s.4(a)
MAX_FOUNDING_UNITS = 2500         # s.4(a), ordinarily
OFFER_PUBLIC_DAYS = 90            # s.5(a): public at least 90 days before rankings close
NEGOTIATION_DAYS = 180            # s.5(c)
INITIAL_TERM_YEARS = 30           # s.6(c)
RENEWAL_YEARS = 10                # s.6(c)
PRINCIPAL_LIVELIHOOD_HOURS = 1000  # s.3(a)


# --------------------------------------------------------------------------
# Section 3: individual eligibility
# --------------------------------------------------------------------------
@dataclass
class Finding:
    test: str
    passed: bool | None  # None = needs the examiner's judgment
    explanation: str


@dataclass
class EligibilityResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def failed(self) -> list[Finding]:
        return [f for f in self.findings if f.passed is False]

    @property
    def open_questions(self) -> list[Finding]:
        return [f for f in self.findings if f.passed is None]

    @property
    def recommendation(self) -> str:
        if self.failed:
            return "deny"
        return "certify" if not self.open_questions else "examine"


def assess_claim(*, work_type: str, baseline_total_earned: int, baseline_covered_earned: int,
                 baseline_covered_hours: int, separation_type: str, separation_date: date,
                 after_covered_hours: int, causal_start: date | None, causal_end: date | None,
                 is_supplier: bool = False, supplier_share_pct: int = 0, supplier_line_covered: bool = False,
                 filed_on: date | None = None, certified_on: date | None = None) -> EligibilityResult:
    """Apply section 3(a)-(b) and the filing window of 3(d) to the facts a worker states."""
    r = EligibilityResult()
    if work_type not in {"employee", "independent", "owner_operator"}:
        r.findings.append(Finding("Covered work", False, "Passive owners are not eligible; the Act covers employees, "
                                  "independent workers and working owner-operators."))
    else:
        r.findings.append(Finding("Covered work", True, f"Performed covered work as {work_type.replace('_', ' ')}."))

    share = (baseline_covered_earned / baseline_total_earned) if baseline_total_earned > 0 else 0
    principal = share > 0.5 or baseline_covered_hours >= PRINCIPAL_LIVELIHOOD_HOURS
    r.findings.append(Finding(
        "Principal livelihood", principal,
        f"Covered work gave {share:.0%} of earned income and {baseline_covered_hours:,} hours in the representative "
        f"12 months. The Act requires more than half of earned income or at least {PRINCIPAL_LIVELIHOOD_HOURS:,} hours."))

    if separation_type in {"total", "forced_retirement", "business_cancellation"}:
        r.findings.append(Finding("Separation", True, f"Stated {separation_type.replace('_', ' ')} of the covered livelihood."))
    elif separation_type == "substantial":
        lost = 1 - (after_covered_hours / baseline_covered_hours) if baseline_covered_hours else 0
        r.findings.append(Finding(
            "Substantial separation", None if lost >= 0.25 else False,
            f"Covered hours fell by {lost:.0%} from the baseline. A substantial separation is a material loss judged "
            "from duration, hours and earned income; the examiner decides it on the record."))
    else:
        r.findings.append(Finding("Separation", False, "A compliance expense or a lost prospect alone is not a separation."))

    if causal_start:
        inside = separation_date >= causal_start and (causal_end is None or separation_date <= causal_end)
        r.findings.append(Finding(
            "Within the causal period", inside,
            f"Separation on {separation_date:%d %B %Y}; the certified causal period runs from {causal_start:%d %B %Y}"
            + (f" to {causal_end:%d %B %Y}." if causal_end else " with no end date yet.")))

    if is_supplier:
        ok = supplier_line_covered or supplier_share_pct > 50
        r.findings.append(Finding(
            "Direct supplier or contractor", ok if not ok else None,
            f"The targeted industry accounted for {supplier_share_pct}% of covered work. A supplier qualifies only if the "
            "action encompassed that line of work or the targeted industry was more than half of the work, and a direct "
            "supply relationship is proven."))

    if filed_on and certified_on:
        deadline = max(_add_years(certified_on, CLAIM_FILING_YEARS), _add_years(separation_date, CLAIM_FILING_YEARS))
        r.findings.append(Finding(
            "Filed on time", True if filed_on <= deadline else None,
            f"The filing period ends {deadline:%d %B %Y}. Late filings may be accepted for lack of notice, incapacity, "
            "agency error or comparable reasons."))
    return r


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


# --------------------------------------------------------------------------
# Section 4(c): majority of ALL founding units
# --------------------------------------------------------------------------
def ballot_outcome(yes: int, no: int, founding_units: int) -> str:
    """'passed' once yes votes exceed half of all founding units (not merely of votes cast);
    'failed' once that is no longer possible; otherwise 'open'."""
    needed = founding_units // 2 + 1
    if yes >= needed:
        return "passed"
    if founding_units - no < needed:
        return "failed"
    return "open"


def votes_needed(founding_units: int) -> int:
    return founding_units // 2 + 1


# --------------------------------------------------------------------------
# Section 5(b): matching
# --------------------------------------------------------------------------
@dataclass
class Applicant:
    association_id: int
    priority_date: date  # first proven attributable separation of its oldest unremedied certified event
    ranking: list[int]   # offer ids, most preferred first


def tiebreak_key(seed: str, association_id: int) -> str:
    """Equal priority dates are ordered by one public random drawing fixed for the round. The drawing is the
    published seed; anyone can recompute every Association's draw number from it."""
    return hashlib.sha256(f"{seed}:{association_id}".encode()).hexdigest()


def run_matching(applicants: list[Applicant], offer_ids: list[int], seed: str) -> tuple[dict[int, int], list[str]]:
    """Association-proposing deferred acceptance, exactly as section 5(b) describes it.

    Each Association proposes to its highest remaining choice; each offer provisionally keeps the proposer with
    priority (oldest priority date, then the public draw) and rejects the others, who propose to their next
    choice; this repeats until no further proposal is possible. Returns {offer_id: association_id} and a
    step-by-step log that lets anyone reproduce the result.
    """
    valid = set(offer_ids)
    prefs = {a.association_id: [o for o in a.ranking if o in valid] for a in applicants}
    rank_key = {a.association_id: (a.priority_date, tiebreak_key(seed, a.association_id)) for a in applicants}
    next_choice = {a: 0 for a in prefs}
    held: dict[int, int] = {}
    free = sorted(prefs, key=lambda a: rank_key[a])
    log: list[str] = []
    step = 0
    while free:
        a = free.pop(0)
        if next_choice[a] >= len(prefs[a]):
            log.append(f"Association {a} has no remaining acceptable offers and stays unmatched.")
            continue
        o = prefs[a][next_choice[a]]
        next_choice[a] += 1
        step += 1
        current = held.get(o)
        if current is None:
            held[o] = a
            log.append(f"{step}. Association {a} proposes to offer {o}; the offer holds it.")
        elif rank_key[a] < rank_key[current]:
            held[o] = a
            free.append(current)
            log.append(f"{step}. Association {a} proposes to offer {o} and has priority over Association {current}, "
                       f"which is released to its next choice.")
        else:
            free.append(a)
            log.append(f"{step}. Association {a} proposes to offer {o}; Association {current} has priority, so {a} "
                       "moves to its next choice.")
    return held, log


# --------------------------------------------------------------------------
# Section 8: Revolving Founding Fund
# --------------------------------------------------------------------------
@dataclass
class FundTerms:
    principal: int          # dollars actually disbursed
    return_bp: int          # fixed public return, basis points of principal
    rate_bp: int            # share of quarterly operating receipts, basis points

    @property
    def max_obligation(self) -> int:
        return self.principal + self.principal * self.return_bp // 10_000


def fund_terms(principal: int, projected_annual_receipts: int, repayment_years: int = 10,
               return_bp: int = 500) -> FundTerms:
    """Fix the flat percentage of receipts so that, on the published projection, disbursed principal plus its
    fixed return is repaid within `repayment_years`. Identical inputs always give identical terms (s.8(b))."""
    obligation = principal + principal * return_bp // 10_000
    if projected_annual_receipts <= 0:
        rate = 1000
    else:
        rate = -(-obligation * 10_000 // (projected_annual_receipts * repayment_years))  # ceiling, in basis points
    return FundTerms(principal=principal, return_bp=return_bp, rate_bp=max(1, min(rate, 1000)))


def fund_remittance(quarter_receipts: int, rate_bp: int, remaining: int) -> int:
    """s.8(c): the fixed percentage of operating receipts, reduced in the final quarter to the remaining balance."""
    return max(0, min(quarter_receipts * rate_bp // 10_000, remaining))


def state_share(shareable_surplus: int, rate_pct: int) -> int:
    """s.6(d): a flat percentage of positive shareable surplus; no surplus means no payment and no accrued minimum."""
    return shareable_surplus * rate_pct // 100 if shareable_surplus > 0 else 0


# --------------------------------------------------------------------------
# Section 6(c): compact term
# --------------------------------------------------------------------------
def compact_dates(operative_on: date) -> dict:
    initial_end = _add_years(operative_on, INITIAL_TERM_YEARS)
    return {"operative": operative_on, "initial_end": initial_end,
            "renewal_end": _add_years(initial_end, RENEWAL_YEARS)}


def days_from(d: date, n: int) -> date:
    return d + timedelta(days=n)

OBJECTION_DAYS = 60               # Model State Act s.4(a): at least 60 days for objections and contrary evidence
DRAFT_COMPACT_PUBLIC_DAYS = 30    # Model State Act s.4(d): draft compact public 30 days before execution
