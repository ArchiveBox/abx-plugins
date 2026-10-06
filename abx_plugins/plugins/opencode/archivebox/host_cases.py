"""Host CLI and navigation contracts for the optional agent plugin."""

import re
from pathlib import Path
from django.urls import reverse
from archivebox.tests.conftest import cli_env, run_archivebox_cmd, ADMIN_TEST_HOST


def test_version_shared_git_stays_installed_when_opencode_disabled(tmp_path):
    env = cli_env(
        PLUGINS="git",
        GIT_ENABLED="True",
        OPENCODE_ENABLED="False",
        COLUMNS="300",
    )
    result = run_archivebox_cmd(["version", "--binaries=git"], cwd=tmp_path, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [re.split(r"\s{2,}", line.strip()) for line in result.stdout.splitlines()]
    git_rows = [row for row in rows if len(row) == 6 and row[0] == "git"]
    assert len(git_rows) == 1, result.stdout
    assert set(git_rows[0][1].split(", ")) == {"git", "opencode"}
    assert git_rows[0][2] == "✅"
    assert Path(git_rows[0][5]).is_file()
    assert "disabled by OPENCODE_ENABLED=False" not in result.stdout


def test_version_names_disabling_config_in_path_column(tmp_path):
    env = cli_env(OPENCODE_ENABLED="False", COLUMNS="200")
    result = run_archivebox_cmd(
        ["version", "--binaries=opencode"],
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [re.split(r"\s{2,}", line.strip()) for line in result.stdout.splitlines()]
    opencode_rows = [row for row in rows if len(row) == 6 and row[1] == "opencode"]
    assert len(opencode_rows) == 4, result.stdout
    assert all(
        row[2] == "disabled" and row[5] == "disabled by OPENCODE_ENABLED=False"
        for row in opencode_rows
    )
    assert "Disabled plugins are dimmed" not in result.stdout


def test_add_view_hides_agent_link_when_opencode_is_disabled(client, admin_user):
    from archivebox.machine.models import Machine

    Machine.from_json({"config": {"OPENCODE_ENABLED": False}})
    client.force_login(admin_user)

    response = client.get(reverse("add"), HTTP_HOST=ADMIN_TEST_HOST)

    assert response.status_code == 200
    assert b"/admin/agent" not in response.content
    assert b"Crawl with AI" not in response.content


def test_admin_navigation_hides_agent_link_when_opencode_is_disabled(
    client,
    admin_user,
):
    from archivebox.machine.models import Machine

    Machine.from_json({"config": {"OPENCODE_ENABLED": False}})
    client.force_login(admin_user)

    response = client.get(
        reverse("admin:index"),
        HTTP_HOST="admin.archivebox.localhost:5797",
    )

    assert response.status_code == 200
    assert b"/admin/agent" not in response.content
    assert b">\xf0\x9f\x92\xac AI<" not in response.content
