import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.skill_store import SkillStore


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str | None = None,
    status: str | None = None,
    risk_level: str = "low",
    category: str = "general",
    requires_exec: bool = False,
    supersedes: list[str] | None = None,
    conflicts_with: list[str] | None = None,
    fallback_to: list[str] | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta_lines = [
        "---",
        f"name: {name}",
        f"description: {description or f'{name} description'}",
        "metadata:",
        "  nanobot:",
        f"    id: {name}-id",
        "    version: 1.0.0",
        f"    category: {category}",
        f"    risk_level: {risk_level}",
        f"    requires_exec: {'true' if requires_exec else 'false'}",
    ]
    if status:
        meta_lines.append(f"    status: {status}")
    if supersedes:
        meta_lines.append("    supersedes:")
        meta_lines.extend(f"      - {item}" for item in supersedes)
    if conflicts_with:
        meta_lines.append("    conflicts_with:")
        meta_lines.extend(f"      - {item}" for item in conflicts_with)
    if fallback_to:
        meta_lines.append("    fallback_to:")
        meta_lines.extend(f"      - {item}" for item in fallback_to)
    meta_lines.extend(["---", "", f"# {name}", "Use this skill."])
    (skill_dir / "SKILL.md").write_text("\n".join(meta_lines), encoding="utf-8")


def test_skill_store_reindex_loads_skills_relations_and_searches(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", description="Analyze alpha requirements")
    _write_skill(skills, "beta", description="Beta coding helper", requires_exec=True)
    _write_skill(skills, "gamma", description="Gamma fallback", fallback_to=["alpha"])
    _write_skill(skills, "delta", description="Delta replaces alpha", supersedes=["alpha"])
    _write_skill(skills, "epsilon", description="Epsilon conflicts beta", conflicts_with=["beta"])

    store = SkillStore(tmp_path)
    result = store.reindex(builtin_dir=tmp_path / "empty_builtin")

    assert result.skills == 5
    assert result.relations == 3
    assert result.db_path.exists()

    rows = store.list_skills()
    assert {row["name"] for row in rows} == {"alpha", "beta", "gamma", "delta", "epsilon"}
    beta = store.get_skill("beta")
    assert beta is not None
    assert beta["requires_exec"] == 1
    assert beta["status"] == "candidate"

    matches = store.search("coding", top_k=3)
    assert [match.name for match in matches] == ["beta"]

    with sqlite3.connect(result.db_path) as conn:
        rel_count = conn.execute("SELECT COUNT(*) FROM skill_relations").fetchone()[0]
    assert rel_count == 3


def test_skill_store_rejects_supersedes_cycles(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", supersedes=["beta"])
    _write_skill(skills, "beta", supersedes=["alpha"])

    store = SkillStore(tmp_path)

    with pytest.raises(ValueError, match="supersedes cycle"):
        store.reindex(builtin_dir=tmp_path / "empty_builtin")


def test_skill_store_protects_system_status(tmp_path: Path) -> None:
    system_dir = tmp_path / "skills-system"
    _write_skill(system_dir, "composite-task", status="system")

    store = SkillStore(tmp_path)
    result = store.reindex(builtin_dir=tmp_path / "empty_builtin", system_dir=system_dir)

    assert result.skills == 1
    row = store.get_skill("composite-task")
    assert row is not None
    assert row["status"] == "system"

    with pytest.raises(ValueError, match="system skill"):
        store.set_status("composite-task", "verified")


def test_skill_store_governance_transitions(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "draft-skill", status="draft")
    _write_skill(skills, "candidate-skill", status="candidate")
    system_dir = tmp_path / "skills-system"
    _write_skill(system_dir, "system-skill", status="system")

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin", system_dir=system_dir)

    store.approve_draft("draft-skill")
    assert store.get_skill("draft-skill")["status"] == "candidate"

    _write_skill(skills, "another-draft", status="draft")
    store.reindex(builtin_dir=tmp_path / "empty_builtin", system_dir=system_dir)
    with pytest.raises(ValueError, match="cannot transition"):
        store.promote("another-draft")

    store.promote("candidate-skill")
    assert store.get_skill("candidate-skill")["status"] == "verified"

    for action in (
        store.approve_draft,
        store.promote,
        store.deprecate_skill,
        store.reject_skill,
    ):
        with pytest.raises(ValueError, match="system skill"):
            action("system-skill")


def test_skill_store_classifies_and_applies_skill_updates(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "editable", status="verified", description="Old description")

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")

    skill_path = skills / "editable" / "SKILL.md"
    old_markdown = skill_path.read_text(encoding="utf-8")
    minor_markdown = old_markdown.replace("Old description", "New description")
    minor = store.classify_skill_update("editable", minor_markdown)
    assert minor.kind == "minor"
    assert minor.next_status == "verified"
    assert "description" in minor.changed_fields

    major_markdown = minor_markdown.replace("Use this skill.", "Use this skill.\n\n## Method\nRun tests.")
    major = store.classify_skill_update("editable", major_markdown)
    assert major.kind == "major"
    assert major.requires_revalidation is True
    assert major.next_status == "candidate"
    assert "method" in major.changed_fields

    result = store.update_skill_markdown("editable", major_markdown)
    assert result.assessment.kind == "major"
    row = store.get_skill("editable")
    assert row is not None
    assert row["status"] == "candidate"
    assert "Run tests." in skill_path.read_text(encoding="utf-8")


def test_skill_cli_reindex_list_and_system_protection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    _write_skill(skills, "alpha", description="Alpha helper")
    _write_skill(skills, "systemish", description="System helper", status="system")

    runner = CliRunner()
    result = runner.invoke(app, ["skill", "reindex", "--workspace", str(workspace)])

    assert result.exit_code == 0
    assert "Indexed" in result.stdout
    assert (workspace / ".skillstore" / "skillstore.db").exists()

    result = runner.invoke(app, ["skill", "list", "--workspace", str(workspace)])
    assert result.exit_code == 0
    assert "alpha" in result.stdout
    assert "systemish" in result.stdout

    result = runner.invoke(app, ["skill", "approve", "alpha", "--workspace", str(workspace)])
    assert result.exit_code == 0
    assert "Approved skill" in result.stdout
    assert SkillStore(workspace).get_skill("alpha")["status"] == "candidate"

    result = runner.invoke(app, ["skill", "promote", "alpha", "--workspace", str(workspace)])
    assert result.exit_code == 0
    assert "Promoted skill" in result.stdout
    assert SkillStore(workspace).get_skill("alpha")["status"] == "verified"

    result = runner.invoke(app, ["skill", "deprecate", "systemish", "--workspace", str(workspace)])
    assert result.exit_code == 1
    assert "system skill" in result.stdout


def test_skill_cli_test_routing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    skills.mkdir(parents=True)
    cases = tmp_path / "routing.yaml"
    cases.write_text(
        "cases:\n"
        "  - query: review this proposal for risks\n"
        "    expected: document-review\n"
        "  - query: debug failing tests and stack trace\n"
        "    expected: code-debugging\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "test-routing", str(cases), "--workspace", str(workspace), "--threshold", "1.0"],
    )

    assert result.exit_code == 0
    assert "Accuracy: 2/2" in result.stdout


def test_skill_cli_reindex_includes_system_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()

    result = runner.invoke(app, ["skill", "reindex", "--workspace", str(workspace)])

    assert result.exit_code == 0
    store = SkillStore(workspace)
    row = store.get_skill("composite-task")
    assert row is not None
    assert row["status"] == "system"


def test_skill_draft_hidden_until_approved(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    _write_skill(
        skills,
        "zxq-calibration",
        description="Handle zxq calibration workflow and audit reports",
        status="draft",
        category="operations.zxq",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["skill", "reindex", "--workspace", str(workspace)])
    assert result.exit_code == 0

    store = SkillStore(workspace)
    row = store.get_skill("zxq-calibration")
    assert row is not None
    assert row["status"] == "draft"
    assert store.search("zxq calibration audit", top_k=5) == []

    result = runner.invoke(app, ["skill", "approve", "zxq-calibration", "--workspace", str(workspace)])
    assert result.exit_code == 0
    assert store.get_skill("zxq-calibration")["status"] == "candidate"

    matches = store.search("zxq calibration audit", top_k=5)
    assert [match.name for match in matches] == ["zxq-calibration"]


def test_skill_store_composed_draft_approve_creates_candidate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = SkillStore(workspace)

    draft = store.create_skill_draft(
        name="web-draft",
        description="Review customer renewal notes",
        trigger="renewal review",
        method="# Web Draft\n\n## Method\nReview renewal notes.",
        category="customer.review",
    )

    assert draft.status == "ready"
    assert [item.name for item in store.list_skill_drafts()] == ["web-draft"]
    assert store.get_skill("web-draft") is None
    assert not (workspace / "skills" / "web-draft" / "SKILL.md").exists()

    loaded = store.get_skill_draft(draft.draft_id)
    assert loaded is not None
    assert loaded.name == "web-draft"

    approved, row = store.approve_composed_draft(
        draft.draft_id,
        system_dir=tmp_path / "empty-system",
    )

    assert approved.status == "approved"
    assert row is not None
    assert row["name"] == "web-draft"
    assert row["status"] == "candidate"
    assert store.list_skill_drafts() == []
    assert (workspace / "skills" / "web-draft" / "SKILL.md").is_file()
    cases = workspace / "skills" / "web-draft" / "routing_cases.json"
    assert "renewal review" in cases.read_text(encoding="utf-8")

    composing = store.start_skill_draft(
        name="async-web-draft",
        description="Async draft",
    )
    assert composing.status == "composing"
    assert [item.name for item in store.list_skill_drafts()] == ["async-web-draft"]


def test_skill_trace_updates_usage_counters(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", description="Alpha helper")
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")

    store.record_trace(
        trace_id="trace-1",
        selected_skill="alpha",
        selection_reason="cold",
        gate_result="ok",
    )
    store.record_trace(
        trace_id="trace-2",
        selected_skill="alpha",
        selection_reason="cold",
        gate_result="error",
        user_feedback="routing_failure",
    )

    row = store.get_skill("alpha")
    assert row is not None
    assert row["usage_count"] == 2
    assert row["success_count"] == 1
    assert row["failure_count"] == 1
    assert row["routing_failure_count"] == 1


def test_skill_hot_path_and_lifecycle_reports(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    _write_skill(skills, "stable-helper", description="Stable helper", status="verified")
    _write_skill(skills, "bad-router", description="Bad router", status="verified")
    store = SkillStore(workspace)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    for idx in range(6):
        store.record_trace(
            trace_id=f"stable-{idx}",
            selected_skill="stable-helper",
            gate_result="ok",
        )
    for idx in range(4):
        store.record_trace(
            trace_id=f"bad-{idx}",
            selected_skill="bad-router",
            gate_result="error",
            user_feedback="routing_failure",
        )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skill", "hot-path-report", "--workspace", str(workspace), "--min-usage", "5"],
    )
    assert result.exit_code == 0
    assert "stable-helper" in result.stdout
    assert "bad-router" not in result.stdout

    result = runner.invoke(
        app,
        [
            "skill",
            "lifecycle-report",
            "--workspace",
            str(workspace),
            "--min-usage",
            "4",
            "--apply-deprecate",
        ],
    )
    assert result.exit_code == 0
    assert "bad-router" in result.stdout
    assert store.get_skill("bad-router")["status"] == "deprecated"
