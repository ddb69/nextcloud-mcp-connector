"""A1 measured: what ``GET /ocs/v2.php/apps/app_api/api/v1/users`` really answers.

This is the most dangerous assumption of phase 18 (18-RESEARCH.md §7, assumption A1). D-12
lets a missing identifier in that list remove a whole audit chain, and the list comes from
``IUserManager::searchDisplayName('')``, which is a search over display names and not a
listing of a table. Whether it answers completely on every user backend, and whether a
disabled account is in it, was written down as unmeasured. This file is where it stops being
unmeasured for the topology this project ships.

**The fail-safe does not depend on the outcome.** ``audit/accounts.py`` answers ``None`` on
every failure, on every unreadable shape and on an empty list, and ``None`` removes nothing.
So a red case here means "D-12 does not take effect in this topology", never "a chain was
deleted by mistake". That asymmetry is the point of the design, and it is why this
measurement may live in a test that not every run executes.

What is asserted, in the order the plan asks for it:

1.  the call answers 200 with a list, in the app context and with no impersonation;
2.  the list contains the known accounts of the test topology;
3.  an account that does not exist is simply absent, with no status of its own and no 404,
    which is what makes "missing from the list" the only possible signal;
4.  an account that is deleted with ``occ user:delete`` is gone from the list afterwards.

Step 4 needs a way to reach ``occ``, and that way is the compose topology itself. Where it is
not reachable the case skips and says so, and then A1 is measured for the first three
statements and unmeasured for the fourth.

Run it against the running HaRP topology::

    export HP_SHARED_KEY="$(openssl rand -hex 32)"
    docker compose -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run pytest tests/integration/test_appapi_users_list.py -m integration -q

Without ``.env.exapp`` the ``exapp_env`` fixture skips every case and names the variable that
is missing, so the default suite stays green on a host without Docker.
"""

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from topology import COMPOSE

from mcp_connector import config
from mcp_connector.audit import accounts
from mcp_connector.nextcloud.clients.ocs import OCS_HEADERS
from mcp_connector.nextcloud.credentials import appapi_auth_headers

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: The repository root, from which the compose file of the topology is reachable.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The password of the throwaway account of case four. It never leaves the topology, and the
#: account it belongs to exists for a few seconds inside a container that is thrown away.
THROWAWAY_PASSWORD = "a-password-of-a-throwaway-account-0000"


def deploy_env(exapp_env: dict[str, str]) -> dict[str, str]:
    """The deploy environment ``audit/accounts.py`` reads, built from the fixture.

    The production function takes a mapping and not a settings object, because the recorder
    carries the mapping the application was built with (plan 18-07). Handing it the same shape
    here is what makes this file a measurement of the production path and not of a copy of it.
    """
    return {
        config.ENV_APP_ID: exapp_env["app_id"],
        config.ENV_APP_SECRET: exapp_env["app_secret"],
        config.ENV_APP_VERSION: exapp_env["app_version"],
        config.ENV_AA_VERSION: exapp_env["aa_version"],
        config.ENV_NEXTCLOUD_URL: exapp_env["base_url"],
    }


def app_context_headers(exapp_env: dict[str, str]) -> dict[str, str]:
    """The four AppAPI headers with an empty user id, plus the two OCS ones.

    An empty user id is the app context: the ExApp asks about the instance, and no person is
    impersonated. That is the whole authentication of this route, because it carries
    ``#[AppAPIAuth]`` and ``#[PublicPage]`` (app_api v34.0.3).
    """
    headers = dict(OCS_HEADERS)
    headers.update(
        appapi_auth_headers(
            "",
            app_id=exapp_env["app_id"],
            app_version=exapp_env["app_version"],
            aa_version=exapp_env["aa_version"],
            app_secret=exapp_env["app_secret"],
        )
    )
    return headers


def occ(
    *arguments: str, expect: bool = True, passing: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one ``occ`` command inside the Nextcloud container of the topology.

    The one place in this file that needs more than HTTP, and it is needed for exactly one
    statement: that a *deleted* account disappears from the list. Nothing else here shells
    out, and the arguments are constants of this file plus one generated account name.

    ``passing`` are the variables the command needs **inside** the container. The environment
    of this process does not travel through ``docker compose exec``, which is why
    ``--password-from-env`` needs an explicit ``-e`` and not a ``monkeypatch.setenv``.
    """
    handed = [f"-e{name}={value}" for name, value in sorted((passing or {}).items())]
    completed = subprocess.run(  # noqa: S603 - a fixed command line of this file, no shell
        [
            *COMPOSE,
            "exec",
            "-T",
            *handed,
            "--user",
            "www-data",
            "nextcloud",
            "php",
            "occ",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        cwd=REPO_ROOT,
    )
    if expect and completed.returncode != 0:
        pytest.skip(f"occ {arguments[0]} is not reachable in this topology")
    return completed


async def users_list(exapp_env: dict[str, str]) -> httpx.Response:
    """The raw answer of the endpoint, so a case can assert the status as well as the body."""
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        return await client.get(
            f"{config.normalize_base_url(exapp_env['base_url'])}{accounts.USERS_PATH}",
            headers=app_context_headers(exapp_env),
        )


@pytest.fixture
def throwaway_account(exapp_env: dict[str, str]) -> Iterator[str]:
    """One account that exists for the length of one case, and is deleted inside it.

    It is created here rather than reusing ``alice``, because the measurement is what happens
    when an account is *removed*, and removing an account the other integration files depend
    on would make this file the reason they fail.
    """
    if shutil.which("docker") is None:
        pytest.skip("no docker on this host: the deletion half of A1 stays unmeasured")
    name = f"a1-{uuid.uuid4().hex[:12]}"
    created = occ(
        "user:add",
        "--password-from-env",
        name,
        expect=False,
        passing={"OC_PASS": THROWAWAY_PASSWORD},
    )
    if created.returncode != 0:
        pytest.skip("the topology does not allow creating an account: A1 stays unmeasured")
    try:
        yield name
    finally:
        occ("user:delete", name, expect=False)


async def test_the_account_list_answers_two_hundred_with_a_list(
    exapp_env: dict[str, str],
) -> None:
    """Statement one: the route exists, the app context is enough, and the body is a list."""
    response = await users_list(exapp_env)

    assert response.status_code == 200, response.text
    data = response.json()["ocs"]["data"]
    assert isinstance(data, list), f"the answer was not a list but {type(data).__name__}"
    assert all(isinstance(name, str) for name in data)


async def test_the_list_carries_the_known_accounts_of_this_topology(
    exapp_env: dict[str, str],
) -> None:
    """Statement two, and the one that would fail first on a backend with a search limit."""
    known = await accounts.existing_users(deploy_env(exapp_env))

    assert known is not None, "the production reader could not read the list of this instance"
    assert exapp_env["alice"] in known
    assert exapp_env["bob"] in known


async def test_an_account_that_never_existed_is_simply_absent(
    exapp_env: dict[str, str],
) -> None:
    """Statement three: there is no status of its own for a name nobody ever had.

    This is why the check of D-12 can only ever read "missing from the list": Nextcloud makes
    no difference between an account that was deleted and one that never was, so the absence
    is the entire signal and the fail-safe around it is the entire protection.
    """
    invented = f"a1-nobody-{uuid.uuid4().hex[:12]}"

    response = await users_list(exapp_env)

    assert response.status_code == 200
    assert invented not in response.json()["ocs"]["data"]


async def test_a_deleted_account_is_gone_from_the_list(
    exapp_env: dict[str, str], throwaway_account: str
) -> None:
    """Statement four, the measurement D-12 stands on: deletion is visible in this list."""
    before = await accounts.existing_users(deploy_env(exapp_env))
    assert before is not None
    assert throwaway_account in before, (
        "a freshly created account is not in the list: D-12 would treat every account of this "
        "backend as gone, and only the fail-safe of accounts.py keeps it from deleting"
    )

    occ("user:delete", throwaway_account)

    after = await accounts.existing_users(deploy_env(exapp_env))
    assert after is not None
    assert throwaway_account not in after
    assert exapp_account_still_there(after, exapp_env)


def exapp_account_still_there(known: frozenset[str], exapp_env: dict[str, str]) -> bool:
    """The control of case four: one account went, and the others are still there.

    Without it a green case would also pass over a list that suddenly answers with nothing,
    which is the one answer that would make D-12 delete every chain of the instance.
    """
    return exapp_env["alice"] in known and exapp_env["bob"] in known
