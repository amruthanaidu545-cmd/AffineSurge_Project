import pytest
from sqlalchemy import select

from app.db import NodeVersion, Version, make_session_factory
from app.versioning import ingest_file
from tests.conftest import V1, V2


@pytest.fixture()
def session():
    s = make_session_factory("sqlite:///:memory:")()
    yield s
    s.close()


def nodes_of(session, ver):
    return {n.number: n for n in session.scalars(select(NodeVersion).where(NodeVersion.version_id == ver.id))}


def test_v2_reuses_logical_nodes_and_flags_exactly_the_changed_ones(session):
    v1, _ = ingest_file(session, V1, "CT-200")
    v2, created = ingest_file(session, V2, "CT-200")
    assert created and v2.number == 2
    a, b = nodes_of(session, v1), nodes_of(session, v2)
    # same logical identity for every node that exists in both
    for num in a:
        assert a[num].logical_node_id == b[num].logical_node_id
    modified = {n for n, nv in b.items() if nv.change_status == "modified"}
    assert modified == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert b["5.3"].change_status == "new" and b["5.3"].logical_node_id not in {x.logical_node_id for x in a.values()}
    # 3.1 heading+body glued differently across pages in v2 but is semantically unchanged
    assert b["3.1"].change_status == "unchanged"


def test_duplicate_headings_do_not_cross_match(session):
    ingest_file(session, V1, "CT-200")
    v2, _ = ingest_file(session, V2, "CT-200")
    b = nodes_of(session, v2)
    assert b["4.2"].logical_node_id != b["7.1"].logical_node_id
    assert b["4.2"].change_status == "modified" and b["7.1"].change_status == "unchanged"


def test_v1_is_preserved_after_reingest(session):
    v1, _ = ingest_file(session, V1, "CT-200")
    ingest_file(session, V2, "CT-200")
    a = nodes_of(session, v1)
    assert "approximately 300 measurement cycles" in a["2.1.1.1"].body
    assert len(a) == 28 and "5.3" not in a
    assert len(list(session.scalars(select(Version)))) == 2


def test_reingesting_identical_file_is_noop(session):
    v1, c1 = ingest_file(session, V1, "CT-200")
    v1b, c2 = ingest_file(session, V1, "CT-200")
    assert c1 and not c2 and v1b.id == v1.id
