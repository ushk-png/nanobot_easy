import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.skill_store import SkillStore, parse_skill_markdown
from nanobot.webui.skill_manage_api import (
    installed_tools_payload,
    skill_manage_approve_draft_payload,
    skill_manage_list_payload,
)


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
    install_sources: list[str] | None = None,
    body: str | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "name": name,
        "description": description or f"{name} description",
        "metadata": {
            "nanobot": {
                "id": f"{name}-id",
                "version": "1.0.0",
                "category": category,
                "risk_level": risk_level,
                "requires_exec": requires_exec,
            }
        },
    }
    meta = frontmatter["metadata"]["nanobot"]
    if status:
        meta["status"] = status
    if supersedes:
        meta["supersedes"] = supersedes
    if conflicts_with:
        meta["conflicts_with"] = conflicts_with
    if fallback_to:
        meta["fallback_to"] = fallback_to
    if install_sources:
        meta["install_sources"] = install_sources
    markdown = (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + (body or f"# {name}\nUse this skill.")
    )
    (skill_dir / "SKILL.md").write_text(markdown, encoding="utf-8")


def _approve(store: SkillStore, *names: str, promote: bool = False) -> None:
    """Run the human approval transition; files always index as draft."""
    for name in names:
        store.approve_draft(name)
        if promote:
            store.promote(name)


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
    # Workspace files enter as draft and stay out of search until approved.
    assert beta["status"] == "draft"
    assert store.search("coding", top_k=3) == []

    _approve(store, "beta")
    matches = store.search("coding", top_k=3)
    assert [match.name for match in matches] == ["beta"]

    with sqlite3.connect(result.db_path) as conn:
        rel_count = conn.execute("SELECT COUNT(*) FROM skill_relations").fetchone()[0]
    assert rel_count == 3


def test_skill_store_reindex_rejects_malformed_frontmatter(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "bad"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: bad\n"
        "description: Bad trigger: unquoted colon\n"
        "---\n\n"
        "# Bad\n",
        encoding="utf-8",
    )

    store = SkillStore(tmp_path)
    with pytest.raises(ValueError, match="malformed YAML frontmatter"):
        store.reindex(builtin_dir=tmp_path / "empty_builtin")


def test_parse_skill_markdown_preserves_method_and_normalizes_frontmatter() -> None:
    markdown = """---
name: imported-review
description: Review imported content.
metadata:
  nanobot:
    category: document.review
    risk_level: low
    requires_exec: false
    triggers:
      - review imported doc
---

## When to use
Use for imported document review.

## Method
Keep this exact procedure.

## Failure rules
Ask for the document if missing.
"""

    parsed = parse_skill_markdown(markdown)

    assert parsed["fields"]["name"] == "imported-review"
    assert parsed["fields"]["category"] == "document.review"
    assert parsed["fields"]["method"].find("Keep this exact procedure.") != -1
    assert parsed["preserved_method"] is True
    assert parsed["validation"]["errors"] == []
    assert "```" not in parsed["normalized_markdown"]


def test_parse_skill_markdown_reports_external_setup_shape_errors() -> None:
    markdown = """---
name: demo-setup
description: Install demo.
metadata:
  nanobot:
    category: external.demo
    risk_level: low
    requires_exec: false
---

## Install
Clone the repo.

## Verify
Run demo --version.
"""

    parsed = parse_skill_markdown(markdown)

    assert parsed["fields"]["name"] == "demo-setup"
    assert any("setup skills must declare risk_level=high" in item for item in parsed["validation"]["errors"])


def test_skill_store_hybrid_search_uses_optional_query_vector(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", description="Alpha task")
    _write_skill(skills, "beta", description="Beta task")

    store = SkillStore(tmp_path)

    def embed(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vectors.append([1.0, 0.0] if "Alpha" in text else [0.0, 1.0])
        return vectors

    store.reindex(
        builtin_dir=tmp_path / "empty_builtin",
        embedding_fn=embed,
        embedding_model="test-embedding",
        embedding_dimensions=2,
    )
    _approve(store, "alpha", "beta")

    matches = store.search("unrelated wording", top_k=2, query_vector=[0.0, 1.0])

    assert matches[0].name == "beta"
    assert matches[0].score > matches[1].score


def test_skill_store_query_vector_cache(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)

    assert store.get_cached_query_vector("abc", embedding_model="embed") is None

    store.set_cached_query_vector("abc", embedding_model="embed", vector=[0.1, 0.2])

    assert store.get_cached_query_vector("abc", embedding_model="embed") == [0.1, 0.2]
    assert store.get_cached_query_vector("abc", embedding_model="other") is None


def test_skill_cards_extract_when_sections_from_body(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "sectioned",
        body=(
            "# Sectioned\n\n"
            "## When to use (trigger phrases)\n\n"
            "- summarize this report\n\n"
            "## When not to use\n\n"
            "- combining multiple sources\n\n"
            "## Method\n\n1. Do it.\n"
        ),
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    row = store.get_skill("sectioned")
    assert row is not None
    assert "summarize this report" in row["when_to_use"]
    assert "combining multiple sources" in row["when_not_to_use"]
    assert "Method" not in row["when_to_use"]


def test_routing_score_respects_card_polarity(tmp_path: Path) -> None:
    """when_not_to_use is negative evidence and a lone word is not a phrase.

    Guards against reintroducing hardcoded per-skill boosts: ranking must be
    derivable from each skill's own card (triggers, when_to_use polarity),
    so a broad single-verb name cannot capture queries its card disclaims.
    """
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "summarize",
        description="Summarize URLs, podcasts, and videos with a CLI.",
        body=(
            "# Summarize\n\n"
            "## When not to use\n\n"
            "- Summarizing a pasted report, memo, or article — use summarize-document.\n"
        ),
    )
    _write_skill(
        skills,
        "summarize-document",
        description="Summarize one supplied document. Triggers: summarize this report.",
        body=(
            "# Summarize Document\n\n"
            "## When to use\n\n"
            "- summarize this report\n"
            "- summarize this article\n"
        ),
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    _approve(store, "summarize", "summarize-document")

    matches = store.search("Summarize this report for me", top_k=2)
    assert matches and matches[0].name == "summarize-document"


def test_skill_store_reindex_loads_scoped_workspace_packages(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills / "@steipete", "obsidian", description="Work with Obsidian vaults")

    store = SkillStore(tmp_path)
    result = store.reindex(builtin_dir=tmp_path / "empty_builtin")

    assert result.skills == 1
    row = store.get_skill("obsidian")
    assert row is not None
    assert row["name"] == "obsidian"
    assert row["source"] == "workspace"
    assert row["path"].endswith("@steipete/obsidian/SKILL.md")


def test_skill_store_audit_reports_advisory_findings(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "answer-comparison", category="answer.compare")
    _write_skill(skills, "compare-options", category="decision.compare")
    _write_skill(skills, "ready", category="document.review")
    incomplete_dir = skills / "my"
    incomplete_dir.mkdir(parents=True)
    (incomplete_dir / "SKILL.md").write_text("---\nname: my\n---\n\n# My\n", encoding="utf-8")

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    _approve(store, "answer-comparison", "compare-options", "ready", "my")
    report = store.audit_catalog()

    assert Path(report.report_path).is_file()
    assert report.summary["skills"] == 4
    assert any(item["code"] == "missing_frontmatter_fields" and item["skill_names"] == ["my"] for item in report.attention)
    assert any(item["code"] == "unwired_similarity_cluster" for item in report.attention)
    assert any(item["code"] == "missing_routing_cases" and "ready" in item["skill_names"] for item in report.reference)


def test_skill_store_rejects_supersedes_cycles(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(skills, "alpha", supersedes=["beta"])
    _write_skill(skills, "beta", supersedes=["alpha"])

    store = SkillStore(tmp_path)

    with pytest.raises(ValueError, match="supersedes cycle"):
        store.reindex(builtin_dir=tmp_path / "empty_builtin")


def test_skill_store_validates_external_tool_setup_skills(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    setup_body = """# Demo Setup

## Install

Clone into `workspace/tools/demo` and create a local venv.

## Verify

Run `workspace/tools/demo/bin/demo --version`.

## Uninstall

Delete `workspace/tools/demo` and remove its row from `workspace/tools/installed.md`.
"""
    _write_skill(
        skills,
        "demo-setup",
        risk_level="high",
        category="external.tool",
        requires_exec=True,
        install_sources=["https://github.com/example/demo"],
        body=setup_body,
    )
    _write_skill(
        skills,
        "demo-usage",
        risk_level="medium",
        category="external.tool",
        requires_exec=True,
        fallback_to=["demo-setup"],
        body="""# Demo Usage

## Method

1. Check installation with `which demo` or `demo --version`; if missing, tell the user `demo-setup` is required.
2. Run `demo input.txt --json`.
""",
    )

    store = SkillStore(tmp_path)
    result = store.reindex(builtin_dir=tmp_path / "empty_builtin")

    assert result.skills == 2
    assert store.get_skill("demo-setup")["install_sources_json"] == '["https://github.com/example/demo"]'


def test_installed_tools_payload_reads_workspace_ledger(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "installed.md").write_text(
        "| name | description | installed_at | version | status | last_checked_at | path | source |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| yq | YAML query tool | 2026-07-10 | 3.4.3 | running | 2026-07-10T12:00:00Z | tools/yq/.venv/bin/yq | https://pypi.org/project/yq/ |\n",
        encoding="utf-8",
    )

    payload = installed_tools_payload(tmp_path)

    assert payload == [
        {
            "name": "yq",
            "description": "YAML query tool",
            "installed_at": "2026-07-10",
            "version": "3.4.3",
            "status": "running",
            "last_checked_at": "2026-07-10T12:00:00Z",
            "path": "tools/yq/.venv/bin/yq",
            "source": "https://pypi.org/project/yq/",
        }
    ]


def test_skill_manage_list_includes_installed_tools(tmp_path: Path) -> None:
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "installed.md").write_text(
        "| yq | 3.4.3 | tools/yq/.venv/bin/yq | 2026-07-10 | https://pypi.org/project/yq/ |\n",
        encoding="utf-8",
    )

    payload = skill_manage_list_payload(tmp_path)

    assert payload["installed_tools"][0]["name"] == "yq"
    assert payload["installed_tools"][0]["version"] == "3.4.3"
    assert payload["installed_tools"][0]["installed_at"] == "2026-07-10"


def test_skill_store_rejects_invalid_external_tool_setup_skills(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "bad-setup",
        category="external.tool",
        risk_level="medium",
        requires_exec=True,
        body="""# Bad Setup

## Install
Do something.

## Verify
Check it.
""",
    )

    store = SkillStore(tmp_path)

    with pytest.raises(ValueError, match="risk_level=high"):
        store.reindex(builtin_dir=tmp_path / "empty_builtin")


def test_workspace_skill_cannot_self_declare_status(tmp_path: Path) -> None:
    """P1 governance: runtime-written files never enter search without approval."""
    skills = tmp_path / "skills"
    _write_skill(skills, "self-verified", description="Sneaky skill", status="verified")
    _write_skill(skills, "self-system", description="Sneaky system", status="system")

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")

    assert store.get_skill("self-verified")["status"] == "draft"
    assert store.get_skill("self-system")["status"] == "draft"
    assert store.search("sneaky", top_k=5) == []

    # The registry status survives a rewrite of the file with a new claim.
    _approve(store, "self-verified")
    _write_skill(skills, "self-verified", description="Sneaky skill v2", status="system")
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    assert store.get_skill("self-verified")["status"] == "candidate"


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

    _approve(store, "candidate-skill")

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
    _approve(store, "editable", promote=True)

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

    # A workspace file cannot self-declare system status; it indexes as draft.
    assert SkillStore(workspace).get_skill("systemish")["status"] == "draft"

    result = runner.invoke(app, ["skill", "deprecate", "composite-task", "--workspace", str(workspace)])
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


def test_skill_cli_test_routing_discovers_catalog_cases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["skill", "test-routing", "--workspace", str(workspace), "--threshold", "0.0"],
    )

    assert result.exit_code == 0
    assert "Skill Routing Test" in result.stdout
    assert "file(s)" in result.stdout
    assert "case(s)" in result.stdout
    assert "Accuracy:" in result.stdout


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


def test_skill_store_draft_records_duplicate_review_and_requires_differentiation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    _write_skill(
        skills,
        "answer-comparison",
        description=(
            "Explain differences between concepts, tools, approaches, or terms in a "
            "concise comparison for A vs B, compare X and Y, and which is better requests."
        ),
        status="verified",
        category="answer.compare",
    )
    store = SkillStore(workspace)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    _approve(store, "answer-comparison", promote=True)

    draft = store.create_skill_draft(
        name="postgres-mysql-comparison",
        description=(
            "Compare PostgreSQL and MySQL and recommend which database to choose "
            "based on project requirements."
        ),
        trigger="PostgreSQL vs MySQL\nPostgreSQL하고 MySQL 중 뭐가 나아",
        method="# PostgreSQL MySQL Comparison\n\n## Method\nCompare and recommend.",
        category="answer.compare",
    )

    duplicate = draft.review_json["duplicate"]
    assert duplicate["score"] >= 0.8
    assert duplicate["nearest"]["name"] == "answer-comparison"
    assert duplicate["differentiation_required"] is True

    with pytest.raises(ValueError, match="duplicate draft requires trigger differentiation"):
        skill_manage_approve_draft_payload(
            workspace,
            draft.draft_id,
            approval={"reason": "needed"},
        )


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
    _approve(store, "stable-helper", "bad-router", promote=True)
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
