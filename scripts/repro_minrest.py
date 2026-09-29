"""Repro: min-rest violation for a MULTI-ROLE nurse.

Nurse has 2 roles (RN, SR). Late 14:00-22:00, Early 07:00-15:00.
Rest Late->Early = 9h < min_rest 11h, so scheduling Late(D) then Early(D+1)
must be forbidden. Skill mix demands 1 RN per shift per day (2 days).
Single nurse can only satisfy demand if she works Late then Early...
To make it feasible-without-violation, add a second single-role helper nurse
so a clean schedule exists; check whether the multi-role nurse gets the
forbidden Late->Early pair anyway (proof the constraint is too weak).
"""
import datetime
import sys

sys.path.insert(0, ".")

from app.scheduling.domain import (
    ContractData,
    DomainData,
    NurseData,
    RoleData,
    ShiftTemplateData,
    SkillMixRequirementData,
    SkillMixRuleData,
    make_uuid,
)
from app.scheduling.engine import RosterCPModel, SolverConfig

role_rn = RoleData(id=make_uuid(), code="RN", name="RN")
role_sr = RoleData(id=make_uuid(), code="SR", name="SR")
late = ShiftTemplateData(id=make_uuid(), code="L", name="Late",
                         start_time=datetime.time(14, 0), end_time=datetime.time(22, 0),
                         duration_hours=8.0, day_numbers=frozenset(range(1, 8)))
early = ShiftTemplateData(id=make_uuid(), code="E", name="Early",
                          start_time=datetime.time(7, 0), end_time=datetime.time(15, 0),
                          duration_hours=8.0, day_numbers=frozenset(range(1, 8)))
period_start = datetime.date(2026, 9, 1)
period_days = 2

def contract(nid, spp=2):
    return ContractData(nurse_id=nid, shifts_per_period=spp, max_shifts_per_period=None,
                        min_rest_hours=11, max_consecutive_days=5,
                        enforce_balanced=False, enforce_shifts_per_period=True,
                        enforce_one_shift_per_day=True)

# Multi-role nurse (RN + SR) and one single-role helper (RN only)
n1 = NurseData(id=make_uuid(), employee_id="M1", first_name="Multi", last_name="Role",
               is_available=True, role_ids=[role_rn.id, role_sr.id], skill_ids=[],
               contract=None)
n1.contract = contract(n1.id)
n2 = NurseData(id=make_uuid(), employee_id="H1", first_name="Helper", last_name="RN",
               is_available=True, role_ids=[role_rn.id], skill_ids=[],
               contract=None)
n2.contract = contract(n2.id)

sm = [SkillMixRuleData(id=make_uuid(), name=f"SM-{s.code}", shift_template_id=s.id,
                       priority=0,
                       requirements=(SkillMixRequirementData(role_id=role_rn.id, count=1),))
      for s in (late, early)]

domain = DomainData(
    tenant_id="t1", period_start=period_start, period_days=period_days,
    nurses=[n1, n2],
    roles={r.id: r for r in (role_rn, role_sr)},
    shift_templates=[late, early],
    skill_mix_rules=sm, shift_sequence_rules=[], preferences=[], previous_assignments=[],
    dates=[period_start + datetime.timedelta(days=i) for i in range(period_days)],
    extended_dates=[period_start - datetime.timedelta(days=1), period_start,
                    period_start + datetime.timedelta(days=1)],
)

engine = RosterCPModel(domain, SolverConfig(timeout_seconds=10, num_workers=4))
result = engine.solve()
print("outcome:", result.outcome)
for a in sorted(result.assignments, key=lambda x: (x.date, x.nurse_id)):
    who = "MULTI" if a.nurse_id == n1.id else "helper"
    print(a.date, who, a.shift_template_id == late.id and "Late" or "Early", "role=", a.role_id == role_rn.id and "RN" or "SR")

# Check the forbidden pair on the multi-role nurse
pairs = sorted((a.date, "Late" if a.shift_template_id == late.id else "Early")
               for a in result.assignments if a.nurse_id == n1.id)
violated = (len(pairs) == 2 and pairs[0][1] == "Late" and pairs[1][1] == "Early"
            and (pairs[1][0] - pairs[0][0]).days == 1)
print("MULTI-ROLE NURSE LATE->EARLY (9h rest < 11h):", "VIOLATION PRESENT" if violated else "not scheduled as forbidden pair")
