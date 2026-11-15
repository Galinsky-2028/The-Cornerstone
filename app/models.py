"""The Cornerstone's tables, following the Cornerstone Livelihood Restoration Act section by section.

Every program record carries `is_demo`. Records made while the site is in preview mode are demo
records; when the site is switched to enacted mode, demo records disappear from every view and the
demo accounts are switched off, so real records begin from an empty register.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from foundation.db import Base, utcnow


def demo_col():
    return mapped_column(Boolean, default=False, server_default="0", index=True)


# --------------------------------------------------------------------------
# Site state and people
# --------------------------------------------------------------------------
class SiteSetting(Base):
    __tablename__ = "cs_site_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


PROGRAM_ROLES = {
    "worker": "Displaced worker",
    "labor": "Department of Labor examiner",
    "commerce": "Department of Commerce (Exchange)",
    "state": "State official",
    "supervisor": "State supervisor",
}


class RoleGrant(Base):
    """A person's role in the program. Everyone who signs in is a worker; official roles are granted."""

    __tablename__ = "cs_role_grants"
    __table_args__ = (UniqueConstraint("user_id", "role"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    state_name: Mapped[str] = mapped_column(String(80), default="")  # for state officials and supervisors
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DemoAccount(Base):
    """An account created from the public preview page. Switched off when the law is enacted."""

    __tablename__ = "cs_demo_accounts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    persona: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Interest(Base):
    """People who ask to be kept informed: affected workers, supporters, officials, press."""

    __tablename__ = "cs_interest"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # worker | supporter | official | press
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    state_name: Mapped[str] = mapped_column(String(80), default="")
    organization: Mapped[str] = mapped_column(String(200), default="")
    census_id: Mapped[str] = mapped_column(String(20), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_demo: Mapped[bool] = demo_col()


# --------------------------------------------------------------------------
# Section 2: Event certification
# --------------------------------------------------------------------------
EVENT_STATUSES = ("comment_period", "under_review", "certified", "denied")


class Event(Base):
    __tablename__ = "cs_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    census_id: Mapped[str] = mapped_column(String(20), default="")
    sovereign: Mapped[str] = mapped_column(String(200), default="")
    action: Mapped[str] = mapped_column(Text, default="")
    targeted_work: Mapped[str] = mapped_column(Text, default="")
    affected_group: Mapped[str] = mapped_column(Text, default="")
    intent_evidence: Mapped[str] = mapped_column(Text, default="")
    attributed_losses: Mapped[str] = mapped_column(Text, default="")
    petitioner_type: Mapped[str] = mapped_column(String(40), default="workers")
    petitioner_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    petitioner_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="comment_period")
    filed_on: Mapped[date] = mapped_column(Date, default=date.today)
    comment_ends: Mapped[date] = mapped_column(Date)
    decision_due: Mapped[date] = mapped_column(Date)
    # findings, required by section 2(d) when certified
    occupations: Mapped[str] = mapped_column(Text, default="")
    worksites: Mapped[str] = mapped_column(Text, default="")
    geography: Mapped[str] = mapped_column(Text, default="")
    contrary_evidence: Mapped[str] = mapped_column(Text, default="")
    causal_mechanism: Mapped[str] = mapped_column(Text, default="")
    causal_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    causal_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_separation: Mapped[date | None] = mapped_column(Date, nullable=True)
    acceptable_proof: Mapped[str] = mapped_column(Text, default="")
    decision_reasons: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    # s.2(c): one extension of not more than 90 days, with public reasons
    extension_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    extension_reasons: Mapped[str] = mapped_column(Text, default="", server_default="")
    is_demo: Mapped[bool] = demo_col()

    comments: Mapped[list["EventComment"]] = relationship(back_populates="event", cascade="all, delete-orphan")


SUBPOENA_STATUSES = ("issued", "complied", "contested", "withdrawn", "referred")


class Subpoena(Base):
    """s.2(c): Labor may require relevant records by subpoena, enforceable in United States district court,
    subject to privilege and applicable privacy law. The register shows that a subpoena issued and what it seeks;
    the records produced stay private."""

    __tablename__ = "cs_subpoenas"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("cs_events.id", ondelete="CASCADE"), index=True)
    recipient: Mapped[str] = mapped_column(String(300))
    records: Mapped[str] = mapped_column(Text)  # the records required
    relevance: Mapped[str] = mapped_column(Text)  # why they are relevant to a material dispute
    return_by: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="issued")
    response: Mapped[str] = mapped_column(Text, default="")  # compliance, privilege or privacy objection, referral
    issued_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issued_on: Mapped[date] = mapped_column(Date, default=date.today)
    is_demo: Mapped[bool] = demo_col()


class EventComment(Base):
    __tablename__ = "cs_event_comments"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("cs_events.id", ondelete="CASCADE"), index=True)
    author_name: Mapped[str] = mapped_column(String(200))
    organization: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[str] = mapped_column(String(20), default="evidence")  # evidence | support | oppose
    body: Mapped[str] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_demo: Mapped[bool] = demo_col()

    event: Mapped[Event] = relationship(back_populates="comments")


# --------------------------------------------------------------------------
# Section 3: Individual claims
# --------------------------------------------------------------------------
CLAIM_STATUSES = ("filed", "certified", "denied", "hearing_requested", "remedied", "reopened")


class Claim(Base):
    __tablename__ = "cs_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("cs_events.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    worker_name: Mapped[str] = mapped_column(String(200))
    work_type: Mapped[str] = mapped_column(String(20))  # employee | independent | owner_operator
    occupation: Mapped[str] = mapped_column(String(200))
    employer: Mapped[str] = mapped_column(String(200), default="")
    worksite: Mapped[str] = mapped_column(String(300), default="")
    baseline_start: Mapped[date] = mapped_column(Date)
    baseline_end: Mapped[date] = mapped_column(Date)
    baseline_total_earned: Mapped[int] = mapped_column(Integer, default=0)  # dollars, all earned income
    baseline_covered_earned: Mapped[int] = mapped_column(Integer, default=0)  # dollars from covered work
    baseline_covered_hours: Mapped[int] = mapped_column(Integer, default=0)
    separation_type: Mapped[str] = mapped_column(String(30))
    separation_date: Mapped[date] = mapped_column(Date)
    after_covered_hours: Mapped[int] = mapped_column(Integer, default=0)  # same-length period after separation
    is_supplier: Mapped[bool] = mapped_column(Boolean, default=False)
    supplier_share_pct: Mapped[int] = mapped_column(Integer, default=0)
    prior_benefits: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="filed")
    deceased: Mapped[bool] = mapped_column(Boolean, default=False)
    successor_name: Mapped[str] = mapped_column(String(200), default="")
    successor_relationship: Mapped[str] = mapped_column(String(100), default="")
    decision_reasons: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    hearing_request: Mapped[str] = mapped_column(Text, default="")
    hearing_outcome: Mapped[str] = mapped_column(Text, default="")
    filed_on: Mapped[date] = mapped_column(Date, default=date.today)
    is_demo: Mapped[bool] = demo_col()

    event: Mapped[Event] = relationship()
    documents: Mapped[list["ClaimDocument"]] = relationship(cascade="all, delete-orphan")


class ClaimDocument(Base):
    __tablename__ = "cs_claim_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("cs_claims.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # payroll, tax, union, license, contract, archival, other
    filename: Mapped[str] = mapped_column(String(300))
    blob_key: Mapped[str] = mapped_column(String(1000))
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Section 4: Cornerstone Associations
# --------------------------------------------------------------------------
ASSOCIATION_STATUSES = ("chartering", "enrolling", "roll_closed", "matched", "compact", "restoration_provisional", "remedied")


class Association(Base):
    __tablename__ = "cs_associations"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    principal_state: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="chartering")
    enrollment_opens: Mapped[date] = mapped_column(Date)
    enrollment_closes: Mapped[date] = mapped_column(Date)
    chartered_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    members: Mapped[list["Membership"]] = relationship(back_populates="association", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "cs_memberships"
    id: Mapped[int] = mapped_column(primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"), index=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("cs_claims.id"), unique=True)  # one founding roll at a time
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    on_board: Mapped[bool] = mapped_column(Boolean, default=False)
    opportunity: Mapped[str] = mapped_column(String(20), default="undecided")  # training | employment | franchise | owner_only | undecided
    # s.4(a): founding = a certified claim holding one unit; cohort = a timely applicant whose eligibility is still under
    # review, with no unit or vote until certified. The roll must be resolved before it closes.
    status: Mapped[str] = mapped_column(String(20), default="founding", server_default="founding")
    joined_on: Mapped[date] = mapped_column(Date, default=date.today)

    association: Mapped[Association] = relationship(back_populates="members")
    claim: Mapped[Claim] = relationship()


class Ballot(Base):
    """Every decision that section 4(c) reserves to a majority of ALL founding units."""

    __tablename__ = "cs_ballots"
    id: Mapped[int] = mapped_column(primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # rankings | compact | board | other
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | passed | failed
    opens_on: Mapped[date] = mapped_column(Date, default=date.today)
    closes_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    votes: Mapped[list["Vote"]] = relationship(cascade="all, delete-orphan")
    association: Mapped[Association] = relationship()


class Vote(Base):
    __tablename__ = "cs_votes"
    __table_args__ = (UniqueConstraint("ballot_id", "membership_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    ballot_id: Mapped[int] = mapped_column(ForeignKey("cs_ballots.id", ondelete="CASCADE"), index=True)
    membership_id: Mapped[int] = mapped_column(ForeignKey("cs_memberships.id", ondelete="CASCADE"))
    choice: Mapped[str] = mapped_column(String(10))  # yes | no
    cast_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Sections 5 and 6: State offers, the Exchange and matching
# --------------------------------------------------------------------------
OFFER_STATUSES = ("draft", "objection_period", "submitted", "verified", "published", "matched", "compacted", "withdrawn")


class Offer(Base):
    __tablename__ = "cs_offers"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    state_name: Mapped[str] = mapped_column(String(80))
    district: Mapped[str] = mapped_column(String(200))
    trade: Mapped[str] = mapped_column(String(300))
    prohibition: Mapped[str] = mapped_column(Text, default="")  # the precise prohibition now in force
    lifting_authority: Mapped[str] = mapped_column(Text, default="")
    enabling_law: Mapped[str] = mapped_column(String(300), default="")
    technical_requirements: Mapped[str] = mapped_column(Text, default="")
    obligations: Mapped[str] = mapped_column(Text, default="")
    supervisor: Mapped[str] = mapped_column(String(300), default="")
    surplus_rate_pct: Mapped[int] = mapped_column(Integer, default=3)  # fixed by State general law
    negotiable_terms: Mapped[str] = mapped_column(Text, default="")
    no_new_harm: Mapped[str] = mapped_column(Text, default="")
    label: Mapped[str] = mapped_column(String(20), default="cornerstone")  # cornerstone | garden | both
    federal_authorization: Mapped[str] = mapped_column(Text, default="")  # the State's notes; the record is FederalAuthorization
    federal_prohibition: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # s.7(a)
    # s.5(c): a bundle of districts, each independently qualifying, with an independent feasibility finding
    bundle_districts: Mapped[str] = mapped_column(Text, default="", server_default="")  # one "District: why it qualifies" per line
    bundle_finding: Mapped[str] = mapped_column(Text, default="", server_default="")
    bundle_finder: Mapped[str] = mapped_column(String(300), default="", server_default="")
    max_founding_units: Mapped[int] = mapped_column(Integer, default=2500)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    objection_ends: Mapped[date | None] = mapped_column(Date, nullable=True)
    verification_notes: Mapped[str] = mapped_column(Text, default="")
    round_id: Mapped[int | None] = mapped_column(ForeignKey("cs_rounds.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    objections: Mapped[list["OfferObjection"]] = relationship(cascade="all, delete-orphan")


class OfferObjection(Base):
    __tablename__ = "cs_offer_objections"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("cs_offers.id", ondelete="CASCADE"), index=True)
    author_name: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_demo: Mapped[bool] = demo_col()


class Round(Base):
    __tablename__ = "cs_rounds"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    rankings_close: Mapped[date] = mapped_column(Date)
    random_seed: Mapped[str] = mapped_column(String(64))  # the publicly witnessed draw, fixed when the round opens
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | matched
    # s.5(c): the readiness conditions on which any match in this round may have one extension of up to 90 days
    extension_conditions: Mapped[str] = mapped_column(Text, default="", server_default="")
    match_log: Mapped[list | None] = mapped_column(JSON, nullable=True)  # published step-by-step matching record
    matched_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    offers: Mapped[list[Offer]] = relationship(foreign_keys=[Offer.round_id])


class Ranking(Base):
    """An Association's sealed ranking. Officials and competitors cannot see it before the deadline."""

    __tablename__ = "cs_rankings"
    __table_args__ = (UniqueConstraint("round_id", "association_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("cs_rounds.id", ondelete="CASCADE"), index=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"))
    offer_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending_vote")  # pending_vote | submitted | rejected
    ballot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    association: Mapped[Association] = relationship()


CONSORTIUM_STATUSES = ("pending_votes", "approved", "rejected", "excluded", "matched", "failed")


class Consortium(Base):
    """s.5(b): Associations may enter a round together only with each one's approval by a majority of all its
    founding units (s.4(c)), unchanged founding rolls, and compliance with the published offer. Each constituent
    keeps its own governance; the consortium's ranking is published with the others after the round."""

    __tablename__ = "cs_consortia"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(300))
    round_id: Mapped[int] = mapped_column(ForeignKey("cs_rounds.id", ondelete="CASCADE"), index=True)
    association_ids: Mapped[list] = mapped_column(JSON)  # the proposing association first
    offer_ids: Mapped[list] = mapped_column(JSON)  # the joint ranking, most preferred first
    units_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)  # {association id: founding units} at proposal
    ballot_ids: Mapped[list] = mapped_column(JSON, default=list)  # one approval ballot per constituent
    compact_ballot_ids: Mapped[list] = mapped_column(JSON, default=list)  # one compact ballot per constituent
    status: Mapped[str] = mapped_column(String(20), default="pending_votes")
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_on: Mapped[date] = mapped_column(Date, default=date.today)
    is_demo: Mapped[bool] = demo_col()


class Match(Base):
    __tablename__ = "cs_matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("cs_rounds.id", ondelete="CASCADE"), index=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("cs_offers.id"))
    priority_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="negotiating")  # negotiating | compacted | failed
    negotiation_deadline: Mapped[date] = mapped_column(Date)
    extension_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # s.5(c): once, up to 90
    extension_reasons: Mapped[str] = mapped_column(Text, default="", server_default="")
    successor_of: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the failed match this one follows
    consortium_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # when a consortium holds the match
    is_demo: Mapped[bool] = demo_col()

    association: Mapped[Association] = relationship()
    offer: Mapped[Offer] = relationship()


FEDERAL_STATUSES = ("pending", "issued", "denied")


class FederalAuthorization(Base):
    """s.7(a): before a federally prohibited activity may be ranked, an Act of Congress must expressly authorize
    it, name each displaced federal prohibition and the administering regulator, and prescribe the activity,
    persons, territory, duration and safeguards; the regulator issues or denies on those criteria with public
    findings, and Commerce publishes the authorization and its limits."""

    __tablename__ = "cs_federal_authorizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("cs_offers.id", ondelete="CASCADE"), unique=True)
    act_citation: Mapped[str] = mapped_column(String(300), default="")
    displaced_prohibitions: Mapped[str] = mapped_column(Text, default="")
    regulator: Mapped[str] = mapped_column(String(300), default="")
    activity: Mapped[str] = mapped_column(Text, default="")
    persons: Mapped[str] = mapped_column(Text, default="")
    territory: Mapped[str] = mapped_column(Text, default="")
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    safeguards: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    findings: Mapped[str] = mapped_column(Text, default="")  # the regulator's public findings
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    offer: Mapped[Offer] = relationship()


# --------------------------------------------------------------------------
# Section 6(c)-(e): Compacts and public supervision
# --------------------------------------------------------------------------
class Compact(Base):
    __tablename__ = "cs_compacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("cs_matches.id"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("cs_offers.id"))
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id"))
    status: Mapped[str] = mapped_column(String(20), default="drafting")  # drafting | member_vote | executed | revoked
    services: Mapped[str] = mapped_column(Text, default="")
    launch_milestones: Mapped[str] = mapped_column(Text, default="")
    public_protections: Mapped[str] = mapped_column(Text, default="")
    selected_terms: Mapped[str] = mapped_column(Text, default="")
    operative_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    initial_term_years: Mapped[int] = mapped_column(Integer, default=30)
    renewal_years: Mapped[int] = mapped_column(Integer, default=10)
    surplus_rate_pct: Mapped[int] = mapped_column(Integer, default=3)
    fund_rate_bp: Mapped[int] = mapped_column(Integer, default=0)  # basis points of quarterly operating receipts
    fund_return_bp: Mapped[int] = mapped_column(Integer, default=0)  # fixed public return on principal
    fund_max_obligation: Mapped[int] = mapped_column(Integer, default=0)  # dollars
    projected_annual_receipts: Mapped[int] = mapped_column(Integer, default=0)  # from the feasibility review
    ballot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    offer: Mapped[Offer] = relationship()
    association: Mapped[Association] = relationship()
    match: Mapped[Match] = relationship()


class SupervisorDecision(Base):
    __tablename__ = "cs_supervisor_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    compact_id: Mapped[int] = mapped_column(ForeignKey("cs_compacts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # pricing | access | service | safety | exclusion
    request: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | modified | rejected
    reasons: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_on: Mapped[date] = mapped_column(Date, default=date.today)
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    compact: Mapped[Compact] = relationship()


# --------------------------------------------------------------------------
# Section 8: Revolving Founding Fund, and the State surplus share
# --------------------------------------------------------------------------
FUND_CATEGORIES = {
    "petition_assistance": "Petition assistance",
    "enrollment": "Enrollment",
    "formation": "Formation",
    "feasibility": "Independent feasibility review",
    "legal_accounting": "Legal and accounting preparation",
    "licensing": "Licensing",
    "training": "Member training",
}


class FundAdvance(Base):
    __tablename__ = "cs_fund_advances"
    id: Mapped[int] = mapped_column(primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"), index=True)
    category: Mapped[str] = mapped_column(String(30))
    amount: Mapped[int] = mapped_column(Integer)  # dollars
    provider: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="requested")  # requested | approved | rejected | disbursed
    reasons: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_on: Mapped[date] = mapped_column(Date, default=date.today)
    is_demo: Mapped[bool] = demo_col()

    association: Mapped[Association] = relationship()


class QuarterlyReport(Base):
    """An Enterprise's quarterly figures; the Fund remittance and State share are computed from them."""

    __tablename__ = "cs_quarterly_reports"
    __table_args__ = (UniqueConstraint("compact_id", "quarter"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    compact_id: Mapped[int] = mapped_column(ForeignKey("cs_compacts.id", ondelete="CASCADE"), index=True)
    quarter: Mapped[str] = mapped_column(String(7))  # e.g. 2029-Q3
    operating_receipts: Mapped[int] = mapped_column(Integer)  # dollars, excluding financing, refunds, collected taxes
    shareable_surplus: Mapped[int] = mapped_column(Integer, default=0)  # dollars, under the published rules
    fund_remittance: Mapped[int] = mapped_column(Integer, default=0)
    state_share: Mapped[int] = mapped_column(Integer, default=0)
    submitted_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_on: Mapped[date] = mapped_column(Date, default=date.today)
    is_demo: Mapped[bool] = demo_col()


# --------------------------------------------------------------------------
# Section 9: Delivery and accountability
# --------------------------------------------------------------------------
RESTORATION_CHECKS = {
    "compact": "An enforceable compact is executed",
    "authorizations": "All legally effective authorizations are in place",
    "feasibility": "An independent feasibility review shows a commercially usable opportunity",
    "training": "Member training is funded and accessible",
    "capital": "There is a lawful path to private capital",
}


class Restoration(Base):
    __tablename__ = "cs_restorations"
    id: Mapped[int] = mapped_column(primary_key=True)
    association_id: Mapped[int] = mapped_column(ForeignKey("cs_associations.id", ondelete="CASCADE"), unique=True)
    compact_id: Mapped[int] = mapped_column(ForeignKey("cs_compacts.id"))
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | provisional | remedied | reopened
    findings: Mapped[str] = mapped_column(Text, default="")
    financing_secured: Mapped[bool] = mapped_column(Boolean, default=False)
    operations_began_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_demo: Mapped[bool] = demo_col()

    association: Mapped[Association] = relationship()
    compact: Mapped[Compact] = relationship()
