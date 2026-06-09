"""Tests for the VoiceLLM Claude API wrapper.

The Anthropic client is mocked — tests don't make real API calls. We verify:
- Prompts contain the anti-leak directives and the audience/doc_type tag set.
- JSON parsing handles markdown code fences and prose-wrapped outputs.
- Invalid classifier outputs (unknown audience, unknown doc_type, missing
  keys) raise loud errors instead of being silently accepted.
- Each method returns the expected dataclass shape.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

import llm as llm_mod
from llm import (
    CLASSIFY_SYSTEM,
    DESCRIBE_SYSTEM,
    EXEMPLIFY_SYSTEM,
    CandidateExemplar,
    Classification,
    VoiceLLM,
    parse_json_response,
)


@dataclass
class FakeBlock:
    text: str


@dataclass
class FakeResponse:
    content: list[FakeBlock]


def _fake_client(text_payload: str) -> MagicMock:
    """Build a mock anthropic.Anthropic-like client whose
    `client.messages.create(...)` returns a response with the given payload."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = MagicMock(return_value=FakeResponse(content=[FakeBlock(text=text_payload)]))
    return client


# ---------- Prompt content checks --------------------------------------------

def test_classify_system_lists_all_seven_audiences():
    for tag in [
        "technical_peer", "leadership", "direct_report", "cross_functional",
        "external_public", "casual", "self_notes",
    ]:
        assert tag in CLASSIFY_SYSTEM, f"classifier prompt missing audience tag {tag}"


def test_classify_system_lists_all_doc_types():
    for dt in ["polished", "draft", "outline", "chat"]:
        assert dt in CLASSIFY_SYSTEM


def test_describe_system_has_anti_leak_rules():
    # Several distinct anti-leak directives must be present
    assert "ANTI-LEAK" in DESCRIBE_SYSTEM
    assert "proper nouns" in DESCRIBE_SYSTEM.lower()
    assert "5 words" in DESCRIBE_SYSTEM
    assert "placeholder" in DESCRIBE_SYSTEM.lower()


def test_exemplify_system_has_anti_leak_rules():
    assert "ANTI-LEAK" in EXEMPLIFY_SYSTEM
    assert "placeholder" in EXEMPLIFY_SYSTEM.lower()
    assert "5 words" in EXEMPLIFY_SYSTEM


def test_describe_system_documents_all_descriptor_fields():
    # The schema requires these keys; the system prompt should specify them.
    for field in [
        "voice_summary", "rhetorical_moves", "tics", "structural_habits",
        "openings_inventory", "closings_inventory", "transition_style",
        "humor_register", "self_reference_behavior", "what_to_avoid",
    ]:
        assert field in DESCRIBE_SYSTEM, f"descriptor prompt missing field {field}"


# ---------- JSON parsing ------------------------------------------------------

def test_parse_json_response_plain():
    data = parse_json_response('{"audience": "leadership"}')
    assert data == {"audience": "leadership"}


def test_parse_json_response_strips_markdown_fence():
    raw = '```json\n{"audience": "leadership"}\n```'
    assert parse_json_response(raw) == {"audience": "leadership"}


def test_parse_json_response_strips_bare_fence():
    raw = '```\n{"x": 1}\n```'
    assert parse_json_response(raw) == {"x": 1}


def test_parse_json_response_strips_whitespace():
    assert parse_json_response('   {"x": 1}\n  ') == {"x": 1}


def test_parse_json_response_raises_on_malformed():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_json_response("not json at all")


# ---------- Classifier --------------------------------------------------------

def _valid_classifier_payload(**overrides: Any) -> str:
    base = {
        "audience": "technical_peer",
        "audience_confidence": 0.92,
        "audience_alternates": ["cross_functional"],
        "doc_type": "polished",
        "doc_type_confidence": 0.98,
        "axis_estimates": {
            "formality": 0.6,
            "technical_density": 0.85,
            "brevity": 0.4,
            "warmth": 0.3,
        },
        "reasoning": "Heavy use of technical jargon, explicit recommendation.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_classify_happy_path():
    llm = VoiceLLM(client=_fake_client(_valid_classifier_payload()))
    result = llm.classify("some doc text")
    assert isinstance(result, Classification)
    assert result.audience == "technical_peer"
    assert result.audience_confidence == pytest.approx(0.92)
    assert result.doc_type == "polished"
    assert result.axis_estimates["technical_density"] == pytest.approx(0.85)


def test_classify_passes_text_in_user_message():
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client)
    llm.classify("the doc content")
    call = client.messages.create.call_args
    # User message must contain the doc
    user_msg = call.kwargs["messages"][0]
    assert user_msg["role"] == "user"
    assert "the doc content" in user_msg["content"]


def test_classify_uses_cached_system_prompt():
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client)
    llm.classify("text")
    call = client.messages.create.call_args
    system = call.kwargs["system"]
    # System is a list of blocks with cache_control set
    assert isinstance(system, list)
    assert system[0]["text"] == CLASSIFY_SYSTEM
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_classify_rejects_unknown_audience():
    payload = _valid_classifier_payload(audience="random_made_up_tag")
    llm = VoiceLLM(client=_fake_client(payload))
    with pytest.raises(ValueError, match="invalid audience"):
        llm.classify("text")


def test_classify_rejects_unknown_doc_type():
    payload = _valid_classifier_payload(doc_type="manuscript")
    llm = VoiceLLM(client=_fake_client(payload))
    with pytest.raises(ValueError, match="invalid doc_type"):
        llm.classify("text")


def test_classify_rejects_missing_axis_keys():
    payload = json.dumps({
        "audience": "leadership",
        "audience_confidence": 0.9,
        "audience_alternates": [],
        "doc_type": "polished",
        "doc_type_confidence": 0.9,
        "axis_estimates": {"formality": 0.5, "technical_density": 0.5, "brevity": 0.5},  # missing "warmth"
        "reasoning": "x",
    })
    llm = VoiceLLM(client=_fake_client(payload))
    with pytest.raises(ValueError, match="missing required keys"):
        llm.classify("text")


def test_classify_rejects_missing_top_level_key():
    payload = json.dumps({"audience": "leadership"})  # missing nearly everything
    llm = VoiceLLM(client=_fake_client(payload))
    with pytest.raises(ValueError, match="missing required keys"):
        llm.classify("text")


# ---------- Descriptor extraction ---------------------------------------------

def _valid_descriptors_payload(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "voice_summary": "Direct, conversational, leans on em-dashes for asides.",
        "rhetorical_moves": ["Names the tradeoff before the recommendation"],
        "tics": ["Uses 'actually' as a soft correction marker"],
        "structural_habits": ["Three-part shape: claim, evidence, hedge"],
        "openings_inventory": ["Tradeoff-first opener"],
        "closings_inventory": ["Open-question redirect"],
        "transition_style": "Short pivot sentences between sections.",
        "humor_register": "none observed",
        "self_reference_behavior": "Uses 'we' for team work, 'I' for opinions.",
        "what_to_avoid": ["Avoid bullet lists with parallel verb forms"],
    }
    base.update(overrides)
    return json.dumps(base)


def test_extract_descriptors_happy_path():
    llm = VoiceLLM(client=_fake_client(_valid_descriptors_payload()))
    result = llm.extract_descriptors("doc text")
    assert result["voice_summary"].startswith("Direct")
    assert "rhetorical_moves" in result
    assert isinstance(result["rhetorical_moves"], list)


def test_extract_descriptors_rejects_missing_field():
    payload = _valid_descriptors_payload()
    bad = json.loads(payload)
    del bad["tics"]
    llm = VoiceLLM(client=_fake_client(json.dumps(bad)))
    with pytest.raises(ValueError, match="missing required keys"):
        llm.extract_descriptors("text")


# ---------- Exemplar generation -----------------------------------------------

def _valid_exemplars_payload(n: int = 2) -> str:
    return json.dumps({
        "exemplars": [
            {
                "pattern_id": f"pat_{i}",
                "pattern": f"Pattern {i}",
                "synthetic": "Tradeoff is X vs Y — X gives A but B, Y the reverse.",
                "when_to_use": f"When you need pattern {i}.",
            }
            for i in range(n)
        ],
    })


def test_generate_exemplars_happy_path():
    llm = VoiceLLM(client=_fake_client(_valid_exemplars_payload(n=3)))
    result = llm.generate_exemplars("text", patterns=["pattern A", "pattern B", "pattern C"])
    assert len(result) == 3
    assert all(isinstance(e, CandidateExemplar) for e in result)
    assert result[0].pattern_id == "pat_0"


def test_generate_exemplars_empty_pattern_list_skips_llm():
    client = _fake_client("{}")
    llm = VoiceLLM(client=client)
    result = llm.generate_exemplars("text", patterns=[])
    assert result == []
    # API should not have been called
    client.messages.create.assert_not_called()


def test_generate_exemplars_user_content_includes_patterns():
    client = _fake_client(_valid_exemplars_payload(n=1))
    llm = VoiceLLM(client=client)
    llm.generate_exemplars("doc text", patterns=["X-first opener", "Y-list closer"])
    call = client.messages.create.call_args
    user_content = call.kwargs["messages"][0]["content"]
    assert "X-first opener" in user_content
    assert "Y-list closer" in user_content
    # Source text also passed for rhythm reference
    assert "doc text" in user_content


def test_generate_exemplars_rejects_missing_exemplar_keys():
    bad = json.dumps({
        "exemplars": [{"pattern_id": "x", "pattern": "y"}]  # missing synthetic + when_to_use
    })
    llm = VoiceLLM(client=_fake_client(bad))
    with pytest.raises(ValueError, match="missing required keys"):
        llm.generate_exemplars("text", patterns=["a"])


# ---------- Model + max_tokens defaults --------------------------------------

def test_voicellm_defaults_to_sonnet_4_6():
    llm = VoiceLLM(client=_fake_client(_valid_classifier_payload()))
    assert llm.model == "claude-sonnet-4-6"


def test_classify_metadata_happy_path():
    llm = VoiceLLM(client=_fake_client(_valid_classifier_payload()))
    result = llm.classify_metadata(
        title="Design Note: VS Code extension architecture",
        location="confluence",
        space="DTX",
    )
    assert result.audience == "technical_peer"
    assert result.doc_type == "polished"


def test_classify_metadata_user_content_format():
    """The classifier should see title/location/space in a structured way."""
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client)
    llm.classify_metadata(title="Design Note: X", location="confluence", space="DTX")
    call = client.messages.create.call_args
    user_content = call.kwargs["messages"][0]["content"]
    assert "Title: Design Note: X" in user_content
    assert "Location: confluence" in user_content
    assert "Space: DTX" in user_content


def test_classify_metadata_uses_metadata_prompt():
    """classify_metadata must NOT use the prose-classifier system prompt."""
    from llm import CLASSIFY_METADATA_SYSTEM, CLASSIFY_SYSTEM
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client)
    llm.classify_metadata(title="X", location="confluence")
    call = client.messages.create.call_args
    system_text = call.kwargs["system"][0]["text"]
    assert system_text == CLASSIFY_METADATA_SYSTEM
    assert system_text != CLASSIFY_SYSTEM


def test_classify_metadata_omits_optional_fields():
    """Without space/snippet, those fields shouldn't appear in user content."""
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client)
    llm.classify_metadata(title="X", location="confluence")
    user_content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Space:" not in user_content
    assert "Snippet:" not in user_content


def test_classify_metadata_validates_audience():
    """Same validation as full classify — invalid audience must raise."""
    payload = _valid_classifier_payload(audience="random_tag")
    llm = VoiceLLM(client=_fake_client(payload))
    with pytest.raises(ValueError, match="invalid audience"):
        llm.classify_metadata(title="X", location="confluence")


def test_voicellm_custom_model_passed_through():
    client = _fake_client(_valid_classifier_payload())
    llm = VoiceLLM(client=client, model="claude-opus-4-7")
    llm.classify("text")
    assert client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"
