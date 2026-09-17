"""The names of the containers and the compose file one end-to-end run measures against.

Every file in this directory that reaches past HTTP into the topology itself used to spell
those names out, and each spelled them for exactly one topology: the Nextcloud 34 one from
``compose.exapp.yml``. That is fine while there is one, and it stops being fine the moment a
second one exists. On 2026-09-17 the Nextcloud 35 topology was brought up next to it, and the
four files that carry container names kept creating accounts, stopping apps and counting
bruteforce entries in the 34 instance while asserting against the 35 one. Nothing failed
loudly; the cases simply measured two different servers and reported the mismatch as a
product defect.

So the names live here once and are read from the environment, with the 34 topology as the
default: a run that sets nothing behaves exactly as before, and a run against another
topology exports what differs. The defaults are the values those four files carried, so this
module is a move and not a new truth.

Against the Nextcloud 35 topology of ``compose.nc35.yml``::

    export NC_MCP_E2E_COMPOSE_FILE=compose.nc35.yml
    export NC_MCP_E2E_PROJECT=nc-mcp-nc35
    export NC_MCP_E2E_NEXTCLOUD=nc35-nc
    export NC_MCP_E2E_HARP=nc35-harp
    export NC_MCP_E2E_CADDY=nc35-caddy
    export NC_MCP_E2E_CONTAINERS=nc35-nc,nc_app_mcp_connector,nc35-harp,nc35-caddy,nc35-greenmail

``NC_MCP_E2E_EXAPP`` keeps its default there: the container AppAPI deploys carries the app id
and not the project name, so both topologies call it the same thing.
"""

import os

__all__ = [
    "CADDY_CONTAINER",
    "COMPOSE",
    "COMPOSE_HINT",
    "CONTAINERS",
    "EXAPP_CONTAINER",
    "HARP_CONTAINER",
    "NC_CONTAINER",
]


def _name(variable: str, default: str) -> str:
    """The value of ``variable``, or ``default`` when it is unset or empty.

    An empty value is the default and never an empty container name: an exported but blank
    variable is a typo in a shell, and a blank name would make every ``docker exec`` of this
    directory address whatever container comes first.
    """
    return os.environ.get(variable, "").strip() or default


#: The Nextcloud container, the one every ``occ`` of this directory runs in.
NC_CONTAINER = _name("NC_MCP_E2E_NEXTCLOUD", "nc-mcp-exapp-nc")

#: The HaRP container in front of the ExApp.
HARP_CONTAINER = _name("NC_MCP_E2E_HARP", "nc-mcp-exapp-harp")

#: The container AppAPI deploys for this app. Named after the app id in every topology.
EXAPP_CONTAINER = _name("NC_MCP_E2E_EXAPP", "nc_app_mcp_connector")

#: The reverse proxy that publishes the instance on the loopback port.
CADDY_CONTAINER = _name("NC_MCP_E2E_CADDY", "nc-mcp-exapp-caddy")

#: The compose file of the topology, for the calls that go through compose rather than
#: through a container name.
COMPOSE_FILE = _name("NC_MCP_E2E_COMPOSE_FILE", "compose.exapp.yml")

#: The compose project name. Only the printed hint uses it; nothing resolves through it.
PROJECT = _name("NC_MCP_E2E_PROJECT", "nc-mcp-exapp")

#: The argument vector of a compose call against that file.
COMPOSE = ("docker", "compose", "-f", COMPOSE_FILE)

#: The compose spelling of one ``occ`` call, for the reader who reproduces it by hand.
COMPOSE_HINT = (
    f"docker compose -p {PROJECT} -f {COMPOSE_FILE} exec -T --user www-data nextcloud php occ"
)

#: Every container of the topology, for the measurement document that names what it ran on.
#: A comma separated override replaces the whole list, because a topology may have fewer
#: containers rather than differently named ones: the Nextcloud 35 one ships no registry.
CONTAINERS = tuple(
    part.strip()
    for part in _name(
        "NC_MCP_E2E_CONTAINERS",
        ",".join(
            (
                "nc-mcp-exapp-nc",
                "nc_app_mcp_connector",
                "nc-mcp-exapp-harp",
                "nc-mcp-exapp-caddy",
                "nc-mcp-exapp-registry",
                "nc-mcp-exapp-greenmail",
            )
        ),
    ).split(",")
    if part.strip()
)
