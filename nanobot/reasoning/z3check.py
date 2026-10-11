"""Optional Z3 entailment check for small Boolean rule cases.

SAT only means the encoded premises are consistent. It does not prove that a
natural-language claim is true.
"""

from __future__ import annotations

from nanobot.reasoning.models import Entailment

try:
    import z3
except ImportError:  # pragma: no cover - exercised when the optional solver exists
    z3 = None


def check_entailment(premises: dict[str, bool], claim: tuple[str, bool]) -> Entailment:
    """Check whether ``claim`` follows from Boolean assignments."""

    if z3 is None:
        return _fallback_entailment(premises, claim)
    return _z3_entailment(premises, claim)


def _z3_entailment(premises: dict[str, bool], claim: tuple[str, bool]) -> Entailment:
    variables: dict[str, z3.BoolRef] = {}

    def variable(name: str) -> z3.BoolRef:
        if name not in variables:
            variables[name] = z3.Bool(name)
        return variables[name]

    solver = z3.Solver()
    solver.set(timeout=1000)
    for raw_name, value in premises.items():
        negated = raw_name.startswith("!")
        name = raw_name[1:] if negated else raw_name
        literal = variable(name)
        if negated:
            literal = z3.Not(literal)
        solver.add(literal if value else z3.Not(literal))

    if _status(solver.check()) != "sat":
        status = _status(solver.check())
        if status == "unsat":
            return Entailment.INCONSISTENT
        return Entailment.SOLVER_UNKNOWN

    name, expected = claim
    claim_literal = variable(name) if expected else z3.Not(variable(name))
    solver.push()
    solver.add(z3.Not(claim_literal))
    negated_status = _status(solver.check())
    solver.pop()
    if negated_status == "unknown":
        return Entailment.SOLVER_UNKNOWN
    if negated_status == "unsat":
        return Entailment.ENTAILED

    solver.add(claim_literal)
    claim_status = _status(solver.check())
    if claim_status == "unknown":
        return Entailment.SOLVER_UNKNOWN
    if claim_status == "unsat":
        return Entailment.REFUTED
    return Entailment.UNDETERMINED


def _status(result: z3.CheckSatResult) -> str:
    text = str(result)
    if text not in {"sat", "unsat", "unknown"}:
        return "unknown"
    return text


def _fallback_entailment(premises: dict[str, bool], claim: tuple[str, bool]) -> Entailment:
    seen: dict[str, bool] = {}
    for raw_name, value in premises.items():
        negated = raw_name.startswith("!")
        name = raw_name[1:] if negated else raw_name
        actual = (not value) if negated else value
        if name in seen and seen[name] is not actual:
            return Entailment.INCONSISTENT
        seen[name] = actual
    name, expected = claim
    if name not in seen:
        return Entailment.UNDETERMINED
    return Entailment.ENTAILED if seen[name] is expected else Entailment.REFUTED
