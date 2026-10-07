from app.projects import PROJECT_REGISTRY, all_projects, get_project


def test_registry_has_ten_projects():
    assert len(PROJECT_REGISTRY) == 10


def test_all_project_ids_are_unique():
    ids = [p.id for p in all_projects()]
    assert len(ids) == len(set(ids))


def test_get_project_found_and_not_found():
    assert get_project("push_chain") is not None
    assert get_project("inexistant") is None


def test_project_to_project_row_has_required_fields():
    project = get_project("canopy")
    row = project.to_project_row()
    for key in ("id", "name", "official_url", "type", "status", "requires_kyc", "requires_capital"):
        assert key in row


def test_default_tasks_reference_project_id():
    project = get_project("push_chain")
    tasks = project.default_tasks()
    assert len(tasks) > 0
    assert all(t["project_id"] == "push_chain" for t in tasks)
