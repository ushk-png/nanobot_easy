"""Relation-level obligations for selective context (opt-in path only).

An obligation is a relation, not a word: (action, target, necessity, condition, source span).
"파일은 수정하지 말고 답변만 수정해 줘" = edit/prior_answer/required + edit/external/forbidden.

Two producers, merged in code:
* the design model's ``Obligations.relations`` (semantic; trusted only when its span is in the
  request and its polarity agrees with the clause it cites), and
* ``parse_relations`` below: a clause parser (quoted text removed, clauses split at punctuation
  and contrastive connectives, predicate = verb class + request/connective ending, polarity from
  negation, condition from conditional clauses, target from the clause object). It recognises
  verb CLASSES, not single phrasings, so "구글링해서", "웹에서 찾아보고", "look it up" are the same
  lookup relation.

Targets are bound in code: ``prior_answer`` to the real previous assistant event id; deictic
external targets ("아까 만든 문서", "그 설정") to exactly one recent tool artifact or mentioned file,
otherwise unresolved (clarification, never permission).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

NECESSITY_ORDER = {"optional": 0, "conditional": 1, "required": 2}
ACTIONS = ("answer", "edit", "lookup", "inspect", "write", "run", "send", "delete", "deploy")
EXTERNAL_ACTIONS = {"run", "send", "delete", "deploy"}  # change the world whatever the object

_FILE = re.compile(r"[\w./~@-]+\.(?:py|json|md|tsx?|ya?ml|toml|txt|csv|log|ini|cfg|jsx?|docx?|pdf|sh|env)(?![A-Za-z0-9_])")  # "notes.md의": a Korean particle ends the name
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|‘[^’\n]*’|“[^”\n]*”|「[^」\n]*」|`[^`\n]*`")
# Clause boundaries: punctuation and contrastive/sequential connectives that end a predicate.
_SPLIT = re.compile(r"[.!?](?=\s|$)|[;\n]+|,|(?<=말고)\s|(?<=않고)\s|(?<=대신)\s|(?<=그리고)\s|(?<=[고며])\s|\b(?:but|and then|instead)\b",
                    re.I)
# Verb classes (stems). A class is a meaning; members are its surface forms.
_VERBS = {
    "lookup": r"(?:네이버|구글|검색창)(?:에|에서)?\s?(?:쳐\s?보|쳐\s?봐|찾아\s?보|찾아\s?봐)|검색|서치|구글링|구글|네이버|리서치|조회|찾아\s?보|찾아\s?봐|알아\s?봐|알아\s?보|"
              r"(?:웹|인터넷|온라인)(?:에서|으로|을|를)?\s?(?:확인|찾아|뒤져)|"
              r"look\s*(?:it\s*|this\s*|that\s*)?up|search|google|browse|research|check\s+online",
    "inspect": r"조사|검증|점검|확인|분석|investigate|verify|inspect|check|analy[sz]e",
    "edit": r"건드리|건드려|손\s?대|손대|수정|고쳐|고치|바꿔|바꾸|변경|편집|업데이트|손\s?봐|적용|반영|edit|modify|change|update|fix|revise|apply",
    "write": r"구현|저장|기록|작성|생성|만들어|만들|추가|써\s?(?:줘|주|넣)|implement|save|write|create|add",
    "run": r"실행|돌려|돌리|빌드|설치|테스트|커밋|푸시|머지|재시작|run|execute|build|install|test|commit|push|merge|restart",
    "send": r"보내|전송|발송|메일(?:을|로)?\s?보내|send|email|post",
    "delete": r"삭제|지워|지우|제거|delete|remove",
    "deploy": r"배포|릴리스|deploy|release|publish",
}
# Request/connective endings that make the stem the clause's predicate ("테스트하는 방법" is not).
_ENDING = (r"\s*(?:을|를|은|는|도|만)?\s*(?:"
           r"해도\s?(?:돼|된다|됩니다|좋아|괜찮)|해\s?줘|해\s?주세요|해\s?주십시오|해\s?줄래|해\s?봐|해\s?보세요|해서|하여|하고|해라|하라|하세요|합시다|하자|할래|해(?=$|[\s.!?,])|"
           r"하지|지\s?(?:말|마|않)|하면|할\s?(?:필요|것)|은?\s?필요|는\s?필요|이?\s?필요|없이|금지|"
           r"후(?=\s|$)|\s후(?=\s|$)|한\s?(?:뒤|후|다음)|바람|"
           r"줘|주세요|주십시오|봐|보세요|서|고|라|달라|"
           r"(?=\s*(?:it|this|that|the|for|online|$)))")
_NEGATION = re.compile(r"지\s?(?:말|마|않)|하지\s?(?:말|마|않)|말고|마세요|마십시오|하지\s?마|않|금지|하면\s?안|없이|"
                       r"\b(?:don't|do not|never|without|no need|not)\b", re.I)
_UNNEEDED = re.compile(r"필요\s?(?:없|가\s?없|는\s?없)|불필요|안\s?해도|하지\s?않아도|no need|not needed|unnecessary", re.I)
# Permission cues (auxiliary evidence only: never a grant by themselves).
_PERMISSION = re.compile(r"해도\s?(?:돼|된다|됩니다|좋아|괜찮)|허용|허락|\b(?:you\s+may|you\s+can|feel\s+free|allowed\s+to|go\s+ahead)\b", re.I)
_CONDITION = re.compile(r"(필요\s?하면|필요\s?시|필요한\s?경우|필요할\s?때|모르면|모르는\s?건|불확실하면|확실하지\s?않으면|"
                        r"부족하면|if\s+(?:needed|necessary|you\s+need|unsure)|when\s+needed)", re.I)
_PRIOR_ANSWER = re.compile(r"답변|대답|응답|답(?=[을를에은는의\s])|위\s?(?:내용|계획|글|표|목록)|방금\s?(?:계획|답|내용|글|표|목록|설명)|"
                           r"앞(?:의|에서)?\s?(?:답|내용|계획|설명)|그\s?계획|이\s?계획|시간표|요약본|"
                           r"your\s+(?:answer|response|reply)|previous\s+answer|the\s+answer|above", re.I)
_EXTERNAL_NOUN = re.compile(r"파일|문서|설정|config|코드|스크립트|레포|저장소|폴더|디렉터리|노트|보고서|리포트|DB|데이터베이스|"
                            r"서버|브랜치|커밋|시트|스프레드시트|캘린더|일정표 파일|file|document|repo|settings|script|server|branch",
                            re.I)
# Supplied text as the object of an edit ("다음 문장을 고쳐 줘: '...'"): the result is the answer.
_TEXT_OBJECT = re.compile(r"문장|문구|표현|텍스트|문단|단락|글귀|어투|말투|sentence|phrase|wording|text|paragraph", re.I)
_DEICTIC = re.compile(r"(?:아까|방금|조금\s?전에?|전에|이전에|앞서|그|저|위의?|that|the\s+same)\s?"
                      r"(?:(?:만든|작성한|생성한|저장한|수정한|쓴|열었던|말한|created|wrote|saved)\s?)?"
                      r"(?P<noun>파일|문서|설정|코드|스크립트|노트|보고서|리포트|file|document|config|settings|script|note|report)",
                      re.I)
_KIND_EXT = {"문서": ("md", "txt", "doc", "docx", "pdf", "rst"), "노트": ("md", "txt"), "보고서": ("md", "txt", "doc", "docx", "pdf"),
             "리포트": ("md", "txt", "doc", "docx", "pdf"), "document": ("md", "txt", "doc", "docx", "pdf"),
             "report": ("md", "txt", "doc", "docx", "pdf"), "note": ("md", "txt"),
             "설정": ("json", "yaml", "yml", "toml", "ini", "cfg", "env"), "config": ("json", "yaml", "yml", "toml", "ini", "cfg", "env"),
             "settings": ("json", "yaml", "yml", "toml", "ini", "cfg", "env"),
             "코드": ("py", "js", "ts", "tsx", "jsx", "sh"), "스크립트": ("py", "js", "ts", "sh"), "script": ("py", "js", "ts", "sh")}


@dataclass
class Rel:
    action: str
    target: str            # answer | prior_answer | external_resource | web | unknown
    necessity: str         # required | optional | conditional | forbidden
    source_span: str
    target_ref: str | None = None
    condition: str | None = None
    origin: str = "parser"  # parser | model
    bound_id: str | None = None   # prior answer event id or artifact path, bound in code
    resolved: bool = True
    candidates: list | None = None   # identifiable choices when the target is ambiguous
    noun: str | None = None          # t47: generic noun the user used ("파일"); never a path or artifact ID
    new_artifact: bool | None = None  # t47: a new output whose path the executor decides within scope

    def key(self) -> tuple[str, str]:
        return self.action, ("external_resource" if self.target == "web" else self.target)

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _blank_quotes(text: str) -> str:
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), text or "")


def _clauses(text: str) -> list[tuple[int, str]]:
    out, start = [], 0
    for m in _SPLIT.finditer(text):
        if text[start:m.start()].strip():
            out.append((start, text[start:m.start()]))
        start = m.end()
    if text[start:].strip():
        out.append((start, text[start:]))
    return out


def _target(action: str, clause: str, before: str) -> tuple[str, str | None]:
    if action == "lookup":
        return "web", None
    if action == "inspect":
        named = _FILE.search(clause)
        if _PRIOR_ANSWER.search(clause) and not named and not _EXTERNAL_NOUN.search(clause):
            return "prior_answer", None  # "위 답변이 맞는지 확인해" checks the answer itself
        return "external_resource", named.group(0) if named else None
    if action in EXTERNAL_ACTIONS:
        named = _FILE.search(clause)
        return "external_resource", named.group(0) if named else None
    named = _FILE.search(clause) or _FILE.search(before)
    if named:
        return "external_resource", named.group(0)
    deictic = _DEICTIC.search(clause) or _DEICTIC.search(before)
    if deictic and not _PRIOR_ANSWER.search(deictic.group(0)):
        return "external_resource", deictic.group(0).strip()
    if _EXTERNAL_NOUN.search(clause):
        return "external_resource", _EXTERNAL_NOUN.search(clause).group(0)
    if _PRIOR_ANSWER.search(clause) or _PRIOR_ANSWER.search(before):
        return "prior_answer", None
    if action in {"edit", "write"} and _TEXT_OBJECT.search(clause):
        return "answer", None  # "이 문장을 고쳐 줘: '...'" rewrites supplied text in the answer
    if action == "write":
        return "answer", None  # "계획을 작성해 줘" without an external object writes the answer
    obj = re.sub(rf"(?:{_VERBS.get(action, '')})|{_ENDING}|please|can you|could you|만|좀|다시", " ", clause, flags=re.I)
    words = [w for w in re.findall(r"[가-힣A-Za-z]{2,}", obj)
             if not re.fullmatch(r"그거|그것|이거|이것|저거|저것|it|that|this|the|해줘|주세요|줘", w, re.I)]
    if words:
        return "external_resource", None  # an object that is neither an answer nor a deixis
    return "unknown", None


def parse_relations(request: str) -> list[Rel]:
    """Relations stated in the request (quoted text ignored). Deterministic, no model call."""
    text = _blank_quotes(request)
    rels: list[Rel] = []
    clauses = _clauses(text)
    for index, (offset, clause) in enumerate(clauses):
        before = clauses[index - 1][1] if index else ""
        for action, stems in _VERBS.items():
            for m in re.finditer(rf"(?=({stems}))", clause, re.I):
                start, end = m.start(1), m.end(1)
                tail = clause[end:]
                english_imperative = m.group(1).isascii() and not re.match(r"\s*[가-힣]", tail) and re.match(
                    r"^\s*(?:please\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|you\s+(?:may|can)\s+(?:now\s+)?|and\s+|then\s+)?$",
                    clause[:start], re.I)
                accepted = english_imperative if m.group(1).isascii() else re.match(_ENDING, tail, re.I)
                if not accepted:
                    continue
                window = clause[start:end + 24]
                lead = clause[max(0, start - 12):start]
                if _UNNEEDED.search(window):
                    necessity = "optional"
                elif _NEGATION.search(window) or re.search(r"(?:없이|without)\s*$", lead, re.I):
                    necessity = "forbidden"
                elif (_CONDITION.search(clause[:start]) or _CONDITION.search(window)
                      or (_CONDITION.search(before) and len(_CONDITION.sub("", before).strip()) <= 3)):
                    necessity = "conditional"
                else:
                    necessity = "required"
                target, ref = _target(action, clause, before)
                cond = _CONDITION.search(clause) or _CONDITION.search(before)
                rels.append(Rel(action, target, necessity, clause.strip(), ref,
                                cond.group(0) if cond and necessity == "conditional" else None))
                break  # one relation per action class per clause
    # "검색은 하지 말고 ... 저장해": the forbidden lookup and the required write are separate clauses.
    merged: dict[tuple, Rel] = {}
    for rel in rels:
        k = (*rel.key(), rel.target_ref)
        if k not in merged or _stricter(rel, merged[k]):
            merged[k] = rel
    return list(merged.values())


def _stricter(a: Rel, b: Rel) -> bool:
    if a.necessity == "forbidden" or b.necessity == "forbidden":
        return a.necessity == "forbidden" and b.necessity != "forbidden"
    return NECESSITY_ORDER[a.necessity] > NECESSITY_ORDER[b.necessity]


def polarity_conflict(model: Rel, request: str) -> bool:
    """A model relation contradicts the clause it cites (e.g. required edit on "수정하지 말고")."""
    span = _blank_quotes(model.source_span or "")
    if not span.strip():
        return False
    if (model.necessity == "forbidden" and not _NEGATION.search(span) and not _UNNEEDED.search(span)
            and _PERMISSION.search(span)):
        return True   # "You may now edit notes.md" cited as a prohibition: the span grants, not forbids
    parsed = [r for r in parse_relations(span) if r.action == model.action]
    if not parsed:
        # The parser did not recognise the clause (stays unrecognised). Only explicit negation in
        # the cited span is evidence against a required/conditional reading ("Do not edit notes.md").
        return model.necessity in {"required", "conditional"} and bool(_NEGATION.search(span)) \
            and not _UNNEEDED.search(span)
    forbidden = any(r.necessity == "forbidden" for r in parsed)
    if forbidden:
        return model.necessity != "forbidden"   # the user forbade it; the model may not allow it
    # The model forbids: a conflict only if the clause asks for the action. "검색은 필요 없습니다"
    # (parsed optional/unneeded) read as forbidden is the same direction, only stricter.
    return model.necessity == "forbidden" and any(r.necessity in {"required", "conditional"} for r in parsed)


def external_object(span: str) -> str | None:
    """An external object named in the (quote-blanked) span: a file name, a deictic external noun,
    or an external noun. Auxiliary evidence for a model target, never a requirement by itself."""
    text = _blank_quotes(span or "")
    for pattern in (_FILE, _DEICTIC, _EXTERNAL_NOUN):
        m = pattern.search(text)
        if m and not _PRIOR_ANSWER.fullmatch(m.group(0).strip()):
            return m.group(0).strip()
    return None


def quoted_only(span: str, request: str) -> bool:
    """The cited span occurs in the request only inside quoted text (content, not an instruction)."""
    span = (span or "").strip()
    if not span or span not in (request or ""):
        return False
    blanked = _blank_quotes(request)
    start = request.find(span)
    while start >= 0:
        if blanked[start:start + len(span)].strip():
            return False
        start = request.find(span, start + 1)
    return True


def target_conflict(model: Rel) -> dict | None:
    """The model's target versus the code evidence in the span it cites (action-target relation).

    * external_resource with no external object in the cited span (only an answer/reply/dialogue
      noun) is ungrounded: an external change is never allowed from the model's word alone;
    * answer/prior_answer whose cited span names an external object as the object of the action
      ("아까 만든 문서 파일의 내용을 줄여 줘" read as an answer edit) conflicts.
    Returns the exact conflicting item, or None. Forbidden relations never conflict here.
    """
    if model.necessity == "forbidden" or model.action in {"lookup", "answer"}:
        return None
    span = _blank_quotes(model.source_span or "")
    obj = external_object(span)
    if model.target == "external_resource" or (model.action in EXTERNAL_ACTIONS and model.target != "web"):
        ref = (model.target_ref or "").strip()
        if obj is None or (ref and _PRIOR_ANSWER.search(ref) and not external_object(ref)):
            return {"code": "model_target_ungrounded", "action": model.action, "model_target": model.target,
                    "target_ref": model.target_ref, "source_span": model.source_span,
                    "evidence": "no external object in the cited span"}
        return None
    if model.target in {"answer", "prior_answer"} and obj is not None and not _NEGATION.search(span):
        return {"code": "model_target_conflict", "action": model.action, "model_target": model.target,
                "source_span": model.source_span, "evidence": "span names external object: " + obj}
    return None


def merge_relations(model_rels: list[Rel], parsed: list[Rel]) -> tuple[list[Rel], list[str]]:
    """Model relations refine targets; parsed relations are the floor. Forbidden always wins."""
    out: dict[tuple, Rel] = {}
    notes = []
    for rel in parsed:
        have = out.get(rel.key())
        if have is not None and rel.target_ref and have.target_ref and rel.target_ref != have.target_ref:
            # t51: separate clauses naming different files for the same action ("a.md 파일을 만들어 주세요.
            # b.md 파일도 만들어 주세요.") are one requirement per named file, not one collapsed entry.
            out.setdefault((*rel.key(), rel.target_ref), rel)
            continue
        out[rel.key()] = rel
    for rel in model_rels:
        k = rel.key()
        have = out.get(k)
        if have is None:
            out[k] = rel
        elif have.necessity == "forbidden" and rel.necessity != "forbidden":
            notes.append("parser_forbidden_kept:" + rel.action)
        elif rel.necessity == "forbidden" or _stricter(rel, have):
            out[k] = rel
        elif have.target_ref is None and rel.target_ref:
            have.target_ref = rel.target_ref
        elif (rel.target_ref and have.target_ref and rel.target_ref != have.target_ref
              and rel.necessity == have.necessity and rel.target_ref in (have.source_span or "")):
            # t46: one request naming several targets for the same action ("a.md 와 b.md 를 만들어")
            # is one requirement per target. Only a target the user's own cited text names is added.
            out.setdefault((*k, rel.target_ref), rel)
    # same action, same target kind: a parsed "unknown" target is refined by the model's target
    for rel in list(out.values()):
        if rel.target == "unknown":
            better = [r for r in model_rels if r.action == rel.action and r.target != "unknown"]
            if better:
                out.pop(rel.key(), None)
                for r in better:
                    out.setdefault(r.key(), r)
    return list(out.values()), notes


def prior_answer_id(sources: list[dict], request: str) -> str | None:
    """The real previous response: the newest full assistant source before the current request."""
    for source in sorted(sources, key=lambda s: s.get("sequence", 0), reverse=True):
        if (source.get("actor") == "assistant" and source.get("content")
                and not source.get("excerpt_only") and source.get("content") != request):
            return source.get("event_id")
    return None


def artifact_identity(path: str, workspace: str | None = None) -> str:
    """Normalized target identifier of a located path (t36 policy).

    Relative paths are relative to the workspace root; ``~`` is expanded; absolute paths are kept.
    With a known workspace the path is resolved, so an alias or symlink of one file has one
    identifier. Without one, a relative path stays a distinct "rel:" identifier (never guessed to
    equal an absolute spelling). The identifier names the target only: equal identifiers never mean
    equal content or version, and a name alone never locates a file."""
    import os
    from pathlib import Path
    p = Path(os.path.expanduser(path))
    if p.is_absolute() or workspace:
        base = p if p.is_absolute() else Path(workspace) / p
        try:
            return str(base.resolve())
        except OSError:
            return os.path.normpath(str(base))
    return "rel:" + os.path.normpath(str(p))


def _artifact_targets(pool: list[dict], workspace: str | None) -> dict[str, str]:
    """{identifier: path to bind}. Located artifacts are keyed by their normalized identifier, so
    same-named files at different locations stay apart; a mention with no location joins a located
    artifact only when exactly one of that name exists (otherwise it adds nothing and the located
    candidates stay ambiguous), and stands alone when none does."""
    import os
    def where(a: dict) -> str | None:   # a mention spelled with a directory is located too
        return a.get("full_path") or (a["path"] if "/" in a["path"] else None)

    located: dict[str, str] = {}
    for a in pool:
        if where(a):
            located.setdefault(artifact_identity(where(a), workspace), where(a))
    out = dict(located)
    for a in pool:
        if where(a):
            continue
        name = a["path"]
        if not any(os.path.basename(k) == name for k in located):
            out.setdefault("name:" + name, name)
    return out


def _names_existing(words: str, artifacts: list[dict], workspace: str | None) -> bool:
    """t49: the words are an existing path or a known artifact's ID/name (a concrete link)."""
    import os
    if any(words in {a.get("path"), a.get("full_path"), os.path.basename(a.get("path") or "")} for a in artifacts):
        return True
    if "/" in words or "." in words:
        from pathlib import Path
        p = Path(words).expanduser()
        return (p if p.is_absolute() else Path(workspace or ".") / p).exists()
    return False


def bind_targets(rels: list[Rel], sources: list[dict], artifacts: list[dict], request: str,
                 workspace: str | None = None) -> None:
    """Bind targets to real ids/paths; unresolved targets stay unresolved (clarification)."""
    prior = prior_answer_id(sources, request)
    for rel in rels:
        if rel.necessity == "forbidden":
            continue
        if rel.target == "prior_answer":
            rel.bound_id, rel.resolved = prior, prior is not None
        elif rel.target == "external_resource" and rel.action not in {"run"}:
            ref = rel.target_ref or ""
            # t49: a target is linked by a concrete path, an artifact ID, or exactly one object in
            # context; no list of "generic" words decides it. A new output (write) whose words name no
            # existing object is a delegated naming: the executor links its path before the write.
            words = ref.strip()
            if words and not _FILE.fullmatch(words) and not _DEICTIC.search(words) and not _names_existing(
                    words, artifacts, workspace):
                if rel.action == "write":
                    rel.noun, rel.target_ref = words, None
                    rel.new_artifact, rel.resolved = True, True
                    continue
                if rel.action in {"edit", "delete"}:
                    rel.noun, rel.target_ref, ref = words, None, ""
            if _FILE.fullmatch(ref.strip()):
                rel.bound_id, rel.resolved = ref.strip(), True
                continue
            deictic = _DEICTIC.search(ref)
            if deictic or not ref:
                noun = (deictic.group("noun") if deictic else "").lower()
                exts = _KIND_EXT.get(noun)
                created = re.search(r"만든|작성한|생성한|저장한|created|wrote|saved", ref, re.I)
                pool = [a for a in artifacts if (not created or a.get("kind") in {"created", "modified"})
                        and (not exts or a["path"].rsplit(".", 1)[-1].lower() in exts)]
                # One artifact may appear as a tool path and as a dialogue mention ("t24_todo.md",
                # "/ws/t24_todo.md"); identity is the normalized location, never the bare name (t36).
                targets = _artifact_targets(pool, workspace)
                paths = list(targets.values())
                # t50: in a follow-up the design linked to a task, that task's single output is the
                # referent ("그 문서를 줄여줘"); other recent files do not make it ambiguous.
                linked = _artifact_targets([a for a in pool if a.get("linked_task")], workspace)
                if len(paths) > 1 and len(linked) == 1:
                    paths = list(linked.values())
                if len(paths) == 1:
                    rel.bound_id, rel.resolved = paths[0], True
                elif rel.action in EXTERNAL_ACTIONS | {"inspect", "write"} and not ref:
                    rel.resolved = True  # "배포해 줘", "실행해 줘": the workspace itself
                else:
                    rel.resolved = False
                    rel.candidates = sorted(targets.values()) or None   # what the user has to choose between
            else:
                rel.resolved = True  # a generic noun with no deixis ("서버에", "설정 파일을 새로")
        elif rel.target == "unknown":
            rel.resolved = False


def recent_artifacts(events: list[Any], file_clue: re.Pattern = _FILE) -> list[dict]:
    """Files produced/changed by tools in this session, then files mentioned in dialogue."""
    import json
    out = []
    for e in events:
        meta = {}
        try:
            meta = json.loads(getattr(e, "metadata_json", None) or "{}")
        except Exception:
            meta = {}
        if e.event_type in {"TOOL_RESULT", "FILE_EDIT"}:
            args = meta.get("tool_arguments") or {}
            path = args.get("path") or args.get("file_path") or meta.get("resource_id")
            if path and meta.get("status", meta.get("execution_status")) in {None, "ok"}:
                kind = "read" if meta.get("read_only") else (
                    "created" if re.search(r"write|create|save", str(meta.get("name", "")), re.I) else "modified")
                out.append({"path": str(path).rsplit("/", 1)[-1] if "/" in str(path) else str(path),
                            "full_path": str(path), "kind": kind, "event_id": e.event_id})
        elif e.event_type in {"USER_MESSAGE", "ASSISTANT_MESSAGE"} and e.content:
            for name in file_clue.findall(e.content):
                out.append({"path": name, "kind": "created" if e.actor == "assistant" and re.search(
                    r"만들|작성|생성|저장|created|wrote|saved", e.content) else "mentioned", "event_id": e.event_id})
    return out[-20:]


@dataclass
class Effective:
    relations: list[Rel] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Exact items where the model's structure and the code evidence disagree (never merged).
    conflicts: list[dict] = field(default_factory=list)

    def of(self, action: str | None = None, target: str | None = None, necessity: str | None = None):
        return [r for r in self.relations if (action is None or r.action == action)
                and (target is None or r.target == target) and (necessity is None or r.necessity == necessity)]

    @property
    def lookup(self) -> str:
        nec = [r.necessity for r in self.of("lookup")]
        if "forbidden" in nec:
            return "forbidden"
        if "required" in nec:
            return "required"
        if "conditional" in nec:
            return "conditional"
        return "unneeded"

    @property
    def external_change_required(self) -> bool:
        return any(r.necessity == "required" and (r.action in EXTERNAL_ACTIONS or r.target == "external_resource")
                   and r.action not in {"lookup", "inspect"} for r in self.relations)

    @property
    def inspection_required(self) -> bool:
        return any(r.necessity == "required" and r.action == "inspect" and r.target != "prior_answer"
                   for r in self.relations)

    @property
    def requires_work(self) -> bool:
        """Work an answer alone cannot satisfy: required lookup, inspection or external change."""
        return self.lookup == "required" or self.inspection_required or self.external_change_required

    @property
    def external_change_forbidden(self) -> bool:
        return any(r.necessity == "forbidden" and r.action not in {"lookup", "inspect"}
                   and (r.target == "external_resource" or r.action in EXTERNAL_ACTIONS) for r in self.relations)

    @property
    def external_change_grounded(self) -> bool:
        """Some required/conditional relation grounded in the user's text targets an external change."""
        return any(r.necessity in {"required", "conditional"} and r.action not in {"lookup", "inspect", "answer"}
                   and (r.action in EXTERNAL_ACTIONS or r.target == "external_resource") for r in self.relations)

    @property
    def answer_edit_only(self) -> bool:
        return bool(self.of(target="prior_answer", necessity="required")) and not self.external_change_required

    def unresolved_targets(self) -> list[Rel]:
        return [r for r in self.relations if r.necessity in {"required", "conditional"} and not r.resolved]

    def public(self) -> list[dict]:
        return [r.public() for r in self.relations]
