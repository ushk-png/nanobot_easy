"""t46: several targets of one action named in the user's text are one requirement each; a model
target the user's text does not name is never added."""
from nanobot.agent.context_relations import Rel, merge_relations

TEXT = "t24_a.md 파일과 t24_b.md 파일을 만들어서 각각 할 일 하나씩 적어 줘"


def rel(ref, span=TEXT, origin="model"):
    return Rel(action="write", target="external_resource", necessity="required", source_span=span,
               target_ref=ref, origin=origin)


def test_user_named_second_target_kept():
    out, _ = merge_relations([rel("t24_a.md"), rel("t24_b.md")], [rel("t24_a.md", origin="parser")])
    assert sorted(r.target_ref for r in out) == ["t24_a.md", "t24_b.md"]


def test_model_invented_target_not_added():
    out, _ = merge_relations([rel("secret.md", span="secret.md")], [rel("t24_a.md", origin="parser")])
    assert [r.target_ref for r in out] == ["t24_a.md"]
