import pytest

from project_os.errors import Conflict, NotFound
from project_os.relations import normalize_remote
from tests.conftest import git, make_repo


def events(registry, pid):
    return [e["event_type"] for e in registry.list_events(project_id=pid)]


def test_declared_links(registry):
    a = registry.create_project("Synth")["id"]
    b = registry.create_project("Engine")["id"]
    rel = registry.relations

    link = rel.add_link(a, b, "depends_on", note="  audio core  ")
    assert (link["source_slug"], link["target_slug"], link["note"]) == ("synth", "engine", "audio core")
    assert "link_added" in events(registry, a) and "link_added" in events(registry, b)

    with pytest.raises(Conflict):
        rel.add_link(a, b, "depends_on")
    with pytest.raises(Conflict):
        rel.add_link(a, a, "related")
    with pytest.raises(Conflict):
        rel.add_link(a, b, "bogus")
    with pytest.raises(NotFound):
        rel.add_link(a, 999, "related")

    # A directed kind can go both ways; 'related' cannot be declared twice.
    rel.add_link(b, a, "part_of")
    rel.add_link(a, b, "related")
    with pytest.raises(Conflict):
        rel.add_link(b, a, "related")

    edges = registry.get_project(a)["relations"]
    assert {(e["source"], e["kind"], e["target"]) for e in edges} == {
        ("synth", "depends_on", "engine"), ("engine", "part_of", "synth"), ("synth", "related", "engine"),
    }
    assert all(e["origin"] == "declared" for e in edges)

    rel.remove_link(link["id"])
    assert "link_removed" in events(registry, b)
    with pytest.raises(NotFound):
        rel.remove_link(link["id"])

    # Forgetting a project drops its links.
    registry.delete_project(b)
    assert registry.relations.relations() == []


@pytest.mark.parametrize("url", [
    "https://github.com/me/PizzaLab.git",
    "git@github.com:me/pizzalab.git",
    "ssh://git@github.com/me/pizzalab",
    "https://github.com/me/pizzalab/",
])
def test_normalize_remote(url):
    assert normalize_remote(url) == "github.com/me/pizzalab"


def test_observed_relations(registry, workspace):
    lab = make_repo(workspace / "pizzaLAB")
    lab2 = make_repo(workspace / "pizzaLAB2")
    git(lab, "remote", "add", "origin", "https://github.com/me/pizzaLAB.git")
    git(lab2, "remote", "add", "origin", "git@github.com:me/pizzalab.git")
    make_repo(workspace / "armagetron" / "neon")
    (workspace / "armagetron" / "main.py").write_text("print('outer')\n")
    registry.add_root(str(workspace))
    registry.scan()

    observed = {(e["kind"], e["source"], e["target"]) for e in registry.relation_graph()["relations"]}
    assert ("same_remote", "pizzalab", "pizzalab2") in observed
    assert ("contains", "armagetron", "neon") in observed

    graph = registry.relation_graph()
    assert {n["slug"] for n in graph["nodes"]} >= {"pizzalab", "pizzalab2", "armagetron", "neon"}
    assert all(e["id"] is None for e in graph["relations"])
