"""P3a: the builder grounds on the target's real interface.

Every fixture here is a real document written to disk, because the failures worth
catching are the ones that come from the shapes people actually export: folders inside
folders in a Postman collection, an OpenAPI document in YAML, a `$ref` that has to be
followed before a response path exists, and a file whose extension lies about it.

The adapter and the builder read those documents through the same functions, so a
collection can never mean one thing when a run sends it and another when the builder
reasons about it. That agreement is asserted directly.
"""
from __future__ import annotations

import json

import jsonschema
import pytest
import yaml

from assay.adapters.rest import RestAdapter
from assay.generator.interface import (
    Interface,
    describe_for_prompt,
    detect_format,
    interface_from_target,
    load_document,
    parse_interface,
    resolve_refs,
    sample_response,
    schema_paths,
)

# ── fixtures: documents, not mocks ──────────────────────────────────────────

COLLECTION = {
    "info": {"name": "Compliance Copilot",
             "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "variable": [{"key": "base_url", "value": "https://api.example.com"}],
    "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{api_token}}"}]},
    "item": [
        {"name": "Health",
         "request": {"method": "GET", "url": {"raw": "{{base_url}}/health"}}},
        {"name": "Analysis", "item": [
            {"name": "Deeper", "item": [
                {"name": "Analyse document",
                 "request": {
                     "method": "POST",
                     "url": {"raw": "{{base_url}}/analyse?locale=en"},
                     "header": [{"key": "Content-Type", "value": "application/json"},
                                {"key": "X-Debug", "value": "1", "disabled": True}],
                     "body": {"mode": "raw",
                              "raw": json.dumps({"text": "{{text}}", "depth": 2})},
                 }},
            ]},
            {"name": "Search",
             "request": {"method": "POST",
                         "url": {"protocol": "https", "host": ["api", "example", "com"],
                                 "path": ["search"]},
                         "body": {"mode": "raw", "raw": json.dumps({"query": "x"})}}},
        ]},
    ],
}

OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Compliance Copilot", "version": "1.0"},
    "servers": [{"url": "https://{host}/v1", "variables": {"host": {"default": "api.example.com"}}}],
    "paths": {
        "/health": {"get": {"operationId": "health", "responses": {"200": {"description": "ok"}}}},
        "/analyse": {
            "post": {
                "operationId": "analyseDocument",
                "parameters": [
                    {"name": "locale", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Trace", "in": "header", "schema": {"type": "string"}},
                ],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}, "depth": {"type": "integer"}},
                }}}},
                "responses": {"200": {"description": "ok", "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Analysis"}}}}},
            },
        },
    },
    "components": {
        "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
        "schemas": {
            "Analysis": {
                "type": "object",
                "required": ["findings", "ok"],
                "properties": {
                    "findings": {"type": "array", "items": {"$ref": "#/components/schemas/Finding"}},
                    "ok": {"type": "boolean"},
                    "meta": {"type": "object",
                             "properties": {"elapsed_ms": {"type": "integer"},
                                            "at": {"type": "string", "format": "date-time"}}},
                },
            },
            "Finding": {
                "type": "object",
                "required": ["article"],
                "properties": {"article": {"type": "string"}, "severity": {"type": "integer"},
                               "quotes": {"type": "array", "items": {"type": "string"}}},
            },
        },
    },
    "security": [{"bearerAuth": []}],
}

MCP = {
    "tools": [
        {"name": "search",
         "description": "Search the corpus",
         "inputSchema": {"type": "object", "required": ["query"],
                         "properties": {"query": {"type": "string"},
                                        "limit": {"type": "integer"}}}},
        {"name": "analyse",
         "inputSchema": {"type": "object", "required": ["text"],
                         "properties": {"text": {"type": "string"},
                                        "locale": {"type": "string"}}},
         "outputSchema": {"type": "object", "properties": {
             "findings": {"type": "array", "items": {"type": "object", "properties": {
                 "article": {"type": "string"}}}}}}},
    ],
}


@pytest.fixture
def collection(tmp_path):
    path = tmp_path / "copilot.postman_collection.json"
    path.write_text(json.dumps(COLLECTION))
    return str(path)


@pytest.fixture
def openapi_json(tmp_path):
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(OPENAPI))
    return str(path)


@pytest.fixture
def openapi_yaml(tmp_path):
    path = tmp_path / "openapi.yaml"
    path.write_text(yaml.safe_dump(OPENAPI))
    return str(path)


@pytest.fixture
def mcp(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(json.dumps(MCP))
    return str(path)


# ── Postman ─────────────────────────────────────────────────────────────────

def test_nested_folders_are_walked(collection):
    iface = parse_interface(collection, request="Analyse document")
    assert iface.kind == "postman"
    assert iface.name == "Analyse document"
    assert iface.request_template["method"] == "POST"
    assert iface.request_template["url"] == "{{base_url}}/analyse?locale=en"


def test_request_fields_come_from_body_and_query(collection):
    iface = parse_interface(collection, request="Analyse document")
    # body keys + query key; {{base_url}} is collection config, not a per-case input.
    assert iface.input_fields == ["depth", "locale", "text"]
    assert "base_url" not in iface.input_fields
    assert iface.is_grounded() is True


def test_a_request_can_be_selected_by_folder_path(collection):
    iface = parse_interface(collection, request="Analysis / Deeper / Analyse document")
    assert iface.name == "Analyse document"


def test_url_is_assembled_when_there_is_no_raw(collection):
    iface = parse_interface(collection, request="Search")
    assert iface.request_template["url"] == "https://api.example.com/search"


def test_unnamed_selection_prefers_the_request_that_sends_a_body(collection):
    iface = parse_interface(collection)
    assert iface.name == "Analyse document"


def test_collection_auth_is_extracted_not_invented(collection):
    iface = parse_interface(collection, request="Analyse document")
    assert iface.auth["type"] == "bearer"
    assert iface.auth["params"]["token"] == "{{api_token}}"


def test_disabled_headers_are_dropped(collection):
    template = RestAdapter(import_=collection, request="Analyse document").template
    assert template["headers"]["Content-Type"] == "application/json"
    assert "X-Debug" not in template["headers"]


def test_adapter_and_builder_read_the_same_request(collection):
    """One parser: what the run sends must be what the builder grounded on."""
    adapter = RestAdapter(import_=collection, request="Analyse document")
    iface = parse_interface(collection, request="Analyse document")
    assert adapter.template["url"] == iface.request_template["url"]
    assert adapter.template["method"] == iface.request_template["method"]
    assert json.loads(adapter.template["body"]).keys() == {"text", "depth"}
    assert sorted(json.loads(adapter.template["body"])) == ["depth", "text"]


def test_adapter_reaches_a_request_nested_in_folders(collection):
    adapter = RestAdapter(import_=collection, request="Analyse document")
    assert adapter.template["url"] == "{{base_url}}/analyse?locale=en"


def test_collection_variables_are_defaults_the_spec_can_override(collection):
    adapter = RestAdapter(import_=collection, request="Analyse document")
    assert adapter.variables["base_url"] == "https://api.example.com"
    overridden = RestAdapter(import_=collection, request="Analyse document",
                             variables={"base_url": "https://staging.example.com"})
    assert overridden.variables["base_url"] == "https://staging.example.com"


def test_collection_bearer_auth_becomes_a_substitutable_header(collection):
    adapter = RestAdapter(import_=collection, request="Analyse document")
    assert adapter.template["headers"]["Authorization"] == "Bearer {{api_token}}"


def test_spec_auth_wins_over_the_collection(collection):
    adapter = RestAdapter(import_=collection, request="Analyse document",
                          auth={"type": "bearer", "token_env": "MY_TOKEN"})
    assert "Authorization" not in adapter.template["headers"]


def test_unknown_request_name_is_a_clear_error(collection):
    with pytest.raises(ValueError) as exc:
        RestAdapter(import_=collection, request="No Such Request")
    assert "No Such Request" in str(exc.value)


# ── OpenAPI ─────────────────────────────────────────────────────────────────

def test_openapi_json_yields_request_fields(openapi_json):
    iface = parse_interface(openapi_json)
    assert iface.kind == "openapi"
    assert iface.name == "analyseDocument"          # the POST, not the /health GET
    # body properties + the query parameter; header parameters are not case inputs.
    assert iface.input_fields == ["depth", "locale", "text"]
    assert "X-Trace" not in iface.input_fields


def test_openapi_yaml_parses_identically(openapi_json, openapi_yaml):
    """`pyyaml` is a dependency; an OpenAPI YAML used to die in json.loads."""
    from_json = parse_interface(openapi_json)
    from_yaml = parse_interface(openapi_yaml)
    assert from_yaml.kind == "openapi"
    assert from_yaml.input_fields == from_json.input_fields
    assert from_yaml.response_paths == from_json.response_paths
    assert from_yaml.hash == from_json.hash          # same document, same hash


def test_response_paths_reach_through_a_ref(openapi_json):
    iface = parse_interface(openapi_json)
    assert "$.findings[*].article" in iface.response_paths
    assert "$.findings[*].severity" in iface.response_paths
    assert "$.findings[*].quotes[*]" in iface.response_paths
    assert "$.meta.elapsed_ms" in iface.response_paths
    assert "$.ok" in iface.response_paths


def test_local_refs_are_resolved_into_the_response_schema(openapi_json):
    iface = parse_interface(openapi_json)
    assert "$ref" not in json.dumps(iface.response_schema)
    items = iface.response_schema["properties"]["findings"]["items"]
    assert items["properties"]["article"] == {"type": "string"}


def test_external_refs_are_left_alone_rather_than_fetched(tmp_path):
    doc = json.loads(json.dumps(OPENAPI))
    doc["components"]["schemas"]["Finding"]["properties"]["source"] = {
        "$ref": "https://external.example/schemas.yaml#/Source"}
    path = tmp_path / "external.json"
    path.write_text(json.dumps(doc))

    iface = parse_interface(str(path))
    source = iface.response_schema["properties"]["findings"]["items"]["properties"]["source"]
    assert source == {"$ref": "https://external.example/schemas.yaml#/Source"}
    assert "$.findings[*].source" in iface.response_paths


def test_recursive_refs_terminate(tmp_path):
    doc = json.loads(json.dumps(OPENAPI))
    doc["components"]["schemas"]["Finding"]["properties"]["parent"] = {
        "$ref": "#/components/schemas/Finding"}
    path = tmp_path / "recursive.json"
    path.write_text(json.dumps(doc))

    iface = parse_interface(str(path))       # must not recurse forever
    assert "$.findings[*].article" in iface.response_paths
    assert isinstance(sample_response(iface), dict)


def test_an_operation_can_be_selected_by_id_or_path(openapi_json):
    by_id = parse_interface(openapi_json, request="health")
    assert by_id.name == "health"
    by_path = parse_interface(openapi_json, request="/analyse")
    assert by_path.name == "analyseDocument"


def test_openapi_security_is_reported(openapi_json):
    iface = parse_interface(openapi_json)
    assert iface.auth == {"type": "bearer", "scheme": "bearerAuth"}


def test_adapter_imports_openapi_yaml(openapi_yaml):
    adapter = RestAdapter(import_=openapi_yaml)
    assert adapter.template["method"] == "POST"
    assert adapter.template["url"] == "https://api.example.com/v1/analyse"
    assert adapter.template["headers"]["Content-Type"] == "application/json"
    # No body template: the adapter serialises the case input, which is the body schema.
    assert adapter.template["body"] is None


def test_adapter_and_builder_agree_on_an_openapi_document(openapi_json):
    adapter = RestAdapter(import_=openapi_json)
    iface = parse_interface(openapi_json)
    assert adapter.template["url"] == iface.request_template["url"]
    assert adapter.template["method"] == iface.request_template["method"]


def test_endpoint_supplies_the_server_when_the_document_has_none(tmp_path):
    doc = json.loads(json.dumps(OPENAPI))
    doc.pop("servers")
    path = tmp_path / "no-servers.json"
    path.write_text(json.dumps(doc))

    adapter = RestAdapter(import_=str(path), endpoint="https://elsewhere.example/")
    assert adapter.template["url"] == "https://elsewhere.example/analyse"


# ── MCP ─────────────────────────────────────────────────────────────────────

def test_mcp_tool_schema_becomes_request_fields(mcp):
    iface = parse_interface(mcp, request="analyse")
    assert iface.kind == "mcp"
    assert iface.input_fields == ["locale", "text"]
    assert iface.request_template["method"] == "tools/call"
    assert iface.request_template["tool"] == "analyse"
    assert iface.request_template["arguments"]["text"] == "{{text}}"


def test_mcp_output_schema_grounds_response_paths(mcp):
    iface = parse_interface(mcp, request="analyse")
    assert iface.response_paths == ["$.findings", "$.findings[*].article"]


def test_mcp_without_an_output_schema_is_still_grounded_on_inputs(mcp):
    iface = parse_interface(mcp, request="search")
    assert iface.input_fields == ["limit", "query"]
    assert iface.response_schema is None
    assert iface.is_grounded() is True


def test_a_bare_tool_object_parses(tmp_path):
    path = tmp_path / "tool.json"
    path.write_text(json.dumps(MCP["tools"][0]))
    iface = parse_interface(str(path))
    assert iface.kind == "mcp"
    assert iface.input_fields == ["limit", "query"]


def test_an_mcp_server_config_stays_ungrounded(tmp_path):
    """`mcpServers` says which process to launch, not what a request looks like."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"copilot": {"command": "uvx", "args": ["srv"]}}}))
    iface = parse_interface(str(path))
    assert iface.kind == "unknown"
    assert iface.is_grounded() is False


def test_an_mcp_descriptor_is_not_an_http_target(mcp):
    with pytest.raises(ValueError) as exc:
        RestAdapter(import_=mcp)
    assert "OpenAPI" in str(exc.value)


# ── format detection and failure ────────────────────────────────────────────

def test_format_is_detected_by_content_not_extension(tmp_path):
    path = tmp_path / "definitely.postman_collection.json"
    path.write_text(yaml.safe_dump(OPENAPI))          # YAML OpenAPI, Postman-ish name
    assert detect_format(load_document(path.read_text())) == "openapi"
    assert parse_interface(str(path)).kind == "openapi"


def test_a_postman_collection_named_openapi_still_parses_as_postman(tmp_path):
    path = tmp_path / "openapi.yaml"
    path.write_text(json.dumps(COLLECTION))
    assert parse_interface(str(path)).kind == "postman"


def test_a_malformed_file_is_ungrounded_not_an_exception(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"info": {"name": "x"}, "item": [ , }')
    iface = parse_interface(str(path))
    assert isinstance(iface, Interface)
    assert iface.kind == "unknown"
    assert iface.is_grounded() is False
    assert iface.hash


def test_a_recognised_but_unreadable_document_keeps_its_kind(tmp_path):
    path = tmp_path / "empty-collection.json"
    path.write_text(json.dumps({"info": {"name": "x"}, "item": []}))
    iface = parse_interface(str(path))
    assert iface.kind == "postman"
    assert iface.is_grounded() is False


def test_an_empty_file_is_ungrounded(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    iface = parse_interface(str(path))
    assert iface.kind == "unknown"
    assert iface.is_grounded() is False
    assert describe_for_prompt(iface) == ""


def test_a_missing_file_is_ungrounded(tmp_path):
    iface = parse_interface(str(tmp_path / "nope.json"))
    assert iface.is_grounded() is False


# ── the hash ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fixture", ["collection", "openapi_json", "openapi_yaml", "mcp"])
def test_every_parsed_interface_carries_a_hash(fixture, request):
    """TargetModel.interface_hash has something real to store."""
    iface = parse_interface(request.getfixturevalue(fixture))
    assert len(iface.hash) == 32


def test_the_hash_changes_when_the_interface_does(openapi_json, tmp_path):
    doc = json.loads(json.dumps(OPENAPI))
    doc["paths"]["/analyse"]["post"]["requestBody"]["content"]["application/json"]["schema"][
        "properties"]["extra"] = {"type": "string"}
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(doc))
    assert parse_interface(str(changed)).hash != parse_interface(openapi_json).hash


def test_a_target_spec_resolves_its_own_interface(collection):
    from assay.spec.models import TargetSpec

    spec = TargetSpec(adapter="rest", **{"import": collection}, request="Analyse document")
    assert interface_from_target(spec).input_fields == ["depth", "locale", "text"]
    assert interface_from_target({"import": collection}).kind == "postman"
    assert interface_from_target(None).is_grounded() is False


# ── samples: what codegen dry-runs against ──────────────────────────────────

def test_sample_response_validates_against_the_schema(openapi_json):
    iface = parse_interface(openapi_json)
    sample = sample_response(iface)
    jsonschema.validate(sample, iface.response_schema)     # raises if it does not fit
    assert sample["findings"][0]["article"] == "sample"
    assert sample["findings"][0]["quotes"] == ["sample"]
    assert sample["meta"]["elapsed_ms"] == 1
    assert sample["ok"] is True


def test_sample_honours_formats_examples_and_enums():
    iface = Interface(response_schema={"type": "object", "properties": {
        "at": {"type": "string", "format": "date-time"},
        "status": {"type": "string", "enum": ["ok", "failed"]},
        "score": {"type": "number", "example": 0.42},
        "kind": {"type": ["string", "null"]},
    }})
    sample = sample_response(iface)
    assert sample["at"] == "2024-01-01T00:00:00Z"
    assert sample["status"] == "ok"
    assert sample["score"] == 0.42
    assert sample["kind"] == "sample"


def test_sample_resolves_composed_schemas():
    schema = {"allOf": [
        {"type": "object", "properties": {"a": {"type": "string"}}},
        {"type": "object", "properties": {"b": {"type": "integer"}}},
    ]}
    assert sample_response(Interface(response_schema=schema)) == {"a": "sample", "b": 1}
    assert schema_paths(schema) == ["$.a", "$.b"]


def test_paths_and_sample_handle_an_array_at_the_root():
    schema = {"type": "array", "items": {"type": "object",
                                         "properties": {"article": {"type": "string"}}}}
    assert schema_paths(schema) == ["$[*].article"]
    sample = sample_response(Interface(response_schema=schema))
    assert sample["json"] == [{"article": "sample"}]


def test_resolve_refs_inlines_only_local_pointers():
    root = {"components": {"schemas": {"A": {"type": "object",
                                             "properties": {"x": {"type": "string"}}}}}}
    node = {"local": {"$ref": "#/components/schemas/A"},
            "remote": {"$ref": "other.yaml#/A"},
            "missing": {"$ref": "#/components/schemas/Nope"}}
    resolved = resolve_refs(node, root)
    assert resolved["local"]["properties"]["x"] == {"type": "string"}
    assert resolved["remote"] == {"$ref": "other.yaml#/A"}
    assert resolved["missing"] == {"$ref": "#/components/schemas/Nope"}


def test_describe_for_prompt_carries_the_real_interface(openapi_json):
    described = describe_for_prompt(parse_interface(openapi_json))
    assert "POST https://api.example.com/v1/analyse" in described
    assert "text" in described
    assert "$.findings[*].article" in described
