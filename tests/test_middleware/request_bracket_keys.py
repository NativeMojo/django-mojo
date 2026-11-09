"""
Regression tests for #9499 — bracketed form and query keys must nest.

RequestDataParser._normalize_key used key.rstrip('[]'), which strips every
trailing '[' and ']' character, not one '[]' list marker. A key like
'device[id]' became 'device[id' before the bracket-to-dot rewrite ran, so the
field landed under a broken flat name instead of nesting under 'device'.

Only whole trailing '[]' pairs are list markers. Indexed keys ('items[0]')
nest as a dict keyed by the index, as the parser's docstring says; they do
not become lists.

The parser only touches method, content_type, GET, POST, FILES and body, so
these build fake requests in-process (same pattern as request_data_merge).
"""
from testit import helpers as th
from objict import objict

TESTIT_TIER = "core"


def _fake_request(query="", form="", files=None, method="POST",
                  content_type="application/x-www-form-urlencoded"):
    """Build the minimal request surface RequestDataParser touches.

    Imports live inside the helper so Django is configured first."""
    from django.http import QueryDict
    from django.utils.datastructures import MultiValueDict
    req = objict()
    req.method = method
    req.content_type = content_type
    req.GET = QueryDict(query)
    req.POST = QueryDict(form)
    req.FILES = files if files is not None else MultiValueDict()
    req.body = b""
    return req


@th.django_unit_test("parser: form keys device[id] and device[name] nest under device")
def test_form_bracket_keys_nest(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(form="device[id]=7&device[name]=x"))

    assert "device[id" not in data, \
        f"the closing bracket must not be stripped into a flat 'device[id' key, got keys {list(data.keys())}"
    assert data.get("device") == {"id": "7", "name": "x"}, \
        f"device[id] and device[name] must nest under 'device', got {dict(data)!r}"


@th.django_unit_test("parser: repeated query key user[name][] nests as a list under user.name")
def test_query_nested_list_key(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(query="user[name][]=a&user[name][]=b", method="GET"))

    assert isinstance(data.get("user"), dict), \
        f"user[name][] must nest under 'user', got {dict(data)!r}"
    assert data.user.get("name") == ["a", "b"], \
        f"repeated user[name][] must give the list ['a', 'b'] at user.name, got {dict(data)!r}"


@th.django_unit_test("parser: items[0][name] nests as a dict keyed by the index")
def test_indexed_key_nests_by_index(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(form="items[0][name]=x"))

    assert isinstance(data.get("items"), dict), \
        f"items[0][name] must nest under 'items' as a dict keyed by index, got {dict(data)!r}"
    assert isinstance(data["items"].get("0"), dict), \
        f"items[0][name] must nest under items['0'], got {dict(data)!r}"
    assert data["items"]["0"].get("name") == "x", \
        f"items[0][name]=x must give items['0'].name == 'x', got {dict(data)!r}"


@th.django_unit_test("parser: a[b][c] nests three levels deep")
def test_deep_bracket_key(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(form="a[b][c]=1"))

    assert isinstance(data.get("a"), dict) and isinstance(data.a.get("b"), dict), \
        f"a[b][c] must nest as a -> b -> c, got {dict(data)!r}"
    assert data.a.b.get("c") == "1", \
        f"a[b][c]=1 must give a.b.c == '1', got {dict(data)!r}"


@th.django_unit_test("parser: uploaded file key doc[front] nests under files.doc.front")
def test_file_bracket_key(opts):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils.datastructures import MultiValueDict
    from mojo.helpers.request_parser import parse_request_data

    upload = SimpleUploadedFile("front.txt", b"front", content_type="text/plain")
    files = MultiValueDict({"doc[front]": [upload]})
    data = parse_request_data(_fake_request(files=files, content_type="multipart/form-data"))

    assert isinstance(data.get("files"), dict), \
        f"an uploaded file must land under 'files', got {dict(data)!r}"
    assert isinstance(data.files.get("doc"), dict), \
        f"file key doc[front] must nest under files.doc, got files keys {list(data.files.keys())}"
    assert data.files.doc.get("front") is upload, \
        f"file key doc[front] must give files.doc.front, got {dict(data.files)!r}"


@th.django_unit_test("parser: tags[] and tags[][] still strip list markers; malformed a[]] is kept verbatim")
def test_list_markers_unchanged(opts):
    from mojo.helpers.request_parser import parse_request_data

    data = parse_request_data(_fake_request(form="tags[]=x&tags[]=y"))
    assert data.get("tags") == ["x", "y"], \
        f"repeated tags[] must still give the list ['x', 'y'] at tags, got {dict(data)!r}"

    data = parse_request_data(_fake_request(form="tags[][]=x"))
    assert data.get("tags") == "x", \
        f"tags[][] must still strip both list markers to 'tags', got {dict(data)!r}"

    data = parse_request_data(_fake_request(form="a[]]=1"))
    assert data.get("a[]]") == "1", \
        f"a malformed key a[]] must be kept verbatim, not stripped to 'a', got {dict(data)!r}"
