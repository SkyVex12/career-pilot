"""Prepared pack policy is versioned provenance, never a semantic approval stamp."""

from uuid import uuid4

from copilot_core.packs import NodeRef
from copilot_worker.packs import _keep_known_citations
from copilot_worker.prompts import pack_request


def test_unknown_source_drops_the_whole_prepared_point():
    refs = {"n:1234": NodeRef(node_id=uuid4(), label="Known project")}
    assert _keep_known_citations("I shipped the service [n:5678]", refs) == ""
    assert _keep_known_citations("I shipped X [n:1234] and Y [n:5678]", refs) == ""
    assert _keep_known_citations("I evaluated X [n:1234]", refs) == "I evaluated X [n:1234]"
    # A recognized citation does not make the assertion true; no lexical validator is claimed.
    assert _keep_known_citations("An unverified assertion [n:1234]", refs)
    assert _keep_known_citations("I'd measure delivery lag", refs) == "I'd measure delivery lag"


def test_new_pack_generation_preserves_unknown_personal_facts_and_source_provenance():
    request = pack_request("Alex uses Python.", "Acme", "Engineer", "technical", "Use Epic", 4)
    rules = request.system[0].text
    assert "An absent fact does not establish its opposite" in rules
    assert "Omit a prepared question whose direct" in rules
    assert "answer requires an unknown personal fact" in rules
    assert "Never transfer another task's completion, testing or ownership" in rules
    assert "never as experience the digest does not state" in rules
