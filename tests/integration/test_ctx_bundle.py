"""CTX-01 and CTX-02, measured: four legs, their wall clock, their requests, no side effect.

``TALK_BUDGET``, ``MAIL_BUDGET`` and ``MAX_MAIL_ACCOUNTS`` came out of plans 11-04 and 11-05 as
settings, said out loud as settings in their own comments. A setting is a number nobody has
weighed: too small means degradation in the healthy case, too large means a wall clock a client
gives up on. This file weighs them, and it is the only place in the repository where the four
legs of ``prepare_context`` run against a real Nextcloud at once.

Four things are load bearing in here, and the first two are the reason it exists.

**The measurement never uses the route it measures.** ``unread`` and ``unread_mention`` of the
target conversation are read through ``talk_browse(level="conversations")``, before and after
``fetch("message:...")``, and never through the context route that ``fetch`` walks. The rule
comes from plan 10-08: an operation cannot testify about itself, and a read marker moved by the
very call under test would be invisible to a measurement taken with that call. The freedom from
side effects of that route is a property of spreed 24.0.4 (``waitForNewMessages`` with timeout
0, ``markNotificationsAsRead: false``, and ``updateLastReadMessage`` never reached), which is a
sentence read out of somebody else's source code until it is measured here.

**Every id comes from a real search hit.** Not one ``fetch`` in this file is called with an id
this file made up. A guessed message id reads a neighbouring message and a guessed table id
reads a foreign table, and neither failure is loud (threats T-11-01, T-11-39). When the search
of this instance carries no hit of a kind, the measurement is a **skip with a true reason**, and
the reason names the terms that were tried.

**This file creates nothing.** No table, no message, no mail, no conversation: every number
below comes from what ``scripts/bootstrap_exapp.sh`` and the Nextcloud first run already put
there. There is therefore no ``finally`` that cleans up, because a ``finally`` without content
is a promise without cover. The one thing this run could change is exactly what measurement 5
measures, and the last test of the file reads the conversation counters once more so the end
state is measured rather than assumed.

**A foreign provider error is not a failed leg.** The Deck comment provider of this instance
answers 500 every few runs, and the search leg passes that on as its own ``degraded`` entry,
which is what it is built to do. The assertions below are therefore about entries that say a
leg missed its budget, never about an empty ``degraded`` list, because an assertion on
emptiness would turn somebody else's flaky app into a red measurement of ours.

The topology this was measured against (six containers, three apps, one Nextcloud)::

    nc-mcp-exapp-nc  nc_app_mcp_connector  nc-mcp-exapp-harp
    nc-mcp-exapp-caddy  nc-mcp-exapp-registry  nc-mcp-exapp-greenmail
    Nextcloud 34.0.3, mail 5.11.1, spreed 24.0.4, tables 2.2.2

Run it against the running HaRP topology::

    docker compose -p nc-mcp-exapp -f compose.exapp.yml up -d --wait
    bash scripts/bootstrap_exapp.sh
    set -a && . ./.env.exapp && set +a
    uv run pytest tests/integration/test_ctx_bundle.py -m integration -s

The rows it prints with ``-s`` are the deliverable: they go into
``.planning/phases/11-b-ndelung-budget-und-release-0-1-6/11-06-MEASUREMENTS.md`` verbatim, and
the four budget comments in ``src/mcp_connector/tools/context.py`` quote them.
"""

import os
import time
from collections.abc import AsyncIterator
from statistics import median
from typing import Any

import httpx
import pytest
from topology import CONTAINERS

from mcp_connector.config import normalize_base_url
from mcp_connector.nextcloud import NcClients, capabilities
from mcp_connector.nextcloud.credentials import MODE_APPAPI, Credentials
from mcp_connector.tools import chatgpt
from mcp_connector.tools import context as context_tools
from mcp_connector.tools import mail as mail_tools
from mcp_connector.tools import search as search_tools
from mcp_connector.tools import talk as talk_tools

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

#: The six containers of ``compose.exapp.yml``, named rather than resolved through compose:
#: every compose call against that file wants ``HP_SHARED_KEY`` in the environment, which a
#: test process has no business requiring. The same reasoning as in
#: ``test_srv06_degradation.py``, and the list is here because the measurement document names
#: the topology it belongs to.
#: The query every bundle of this file is asked with. Chosen and not arbitrary, for two
#: reasons that pull in opposite directions: it has to reach files, notes and cards so
#: ``detail="full"`` really produces the three excerpts the reference measurement of plan
#: 04-04 had, and it must not reach the Mail search provider, because a mail hit would put a
#: subject into the bundle through the **search** leg and the subject gate of measurement 4
#: would then be measuring the wrong leg.
MEASUREMENT_QUERY = "Abnahme"

#: How often each wall clock is taken. Three is enough for a minimum, a median and a maximum
#: and cheap enough that the file stays a measurement instead of a load test.
RUNS = 3

#: The reference of plan 04-04 (live proof 5, one MCP session over client, proxy, HaRP,
#: container and Nextcloud, two legs), in seconds. Erfolgskriterium 2 of this phase is the
#: comparison against these two numbers, which is why they are constants and not prose.
REFERENCE_SHORT = 0.84
REFERENCE_FULL = 0.99

#: The six test mails of plan 10-01 by subject. None of them may appear anywhere in the
#: serialised bundle: the mail leg answers counters, and a subject is text somebody wrote who
#: needed no account on this Nextcloud to write it (T-11-38).
GREENMAIL_SUBJECTS = (
    "Gruesse aus Hamburg, die Masse stehen unten",
    "Newsletter August",
    "Grosser Newsletter August",
    "Rechnung",
    "Rechnung Mai",
    "Nur ein Anhang",
)

#: The search terms tried per new id kind, in order. Several per kind on purpose: the point is
#: to find a **real** hit, and which word finds one depends on what is on the instance. A kind
#: without a hit is a skip that names these terms, never a guessed id (T-11-39).
KIND_TERMS: dict[str, tuple[str, ...]] = {
    "message": ("moderation", "conversation", "settings", MEASUREMENT_QUERY),
    "table": ("Test", "Berechtigungstreue", "Welcome", "Tables"),
}

#: The wording every timeout of this module produces (``context._reason``). A ``degraded``
#: entry carrying it means a leg missed its budget, and that is the only kind of entry the
#: wall clock measurement is allowed to fail on.
BUDGET_WORDING = "did not answer within"

#: The measurement protocol of one run. A module level memo and not a fixture, because the
#: last test prints it as one block; a fixture would either rebuild it per test or have to
#: live for the whole session.
_protocol: list[str] = []


def note(line: str) -> None:
    """Record one measured line. Printed by the protocol test at the end of the file."""
    _protocol.append(line)


# --------------------------------------------------------------------------------------
# The identity, and a client that counts what it sends
# --------------------------------------------------------------------------------------


def _appapi_clients(
    exapp_env: dict[str, str],
    user: str,
    hooks: dict[str, list[Any]] | None = None,
) -> NcClients:
    """The impersonating clients of one user, ``APP_SECRET`` as the only credential.

    Mirrors ``deps._credentials_from_appapi``, like every other integration file of this
    repository: no Basic scheme is built here and no user password is read, so a green row
    cannot be explained by a credential that happened to sit in the environment.
    """
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False, timeout=120.0, event_hooks=hooks or {}),
        creds=Credentials(
            base_url=normalize_base_url(exapp_env["base_url"]),
            user=user,
            secret=exapp_env["app_secret"],
            mode=MODE_APPAPI,
            app_id=exapp_env["app_id"],
            app_version=exapp_env["app_version"],
            aa_version=exapp_env["aa_version"],
        ),
    )


def leg_of(path: str) -> str:
    """Which leg one request path belongs to, by prefix and never by guess.

    The two mail names are separate because the cost sentence of CTX-02 is about exactly
    those two routes: one account list plus N mailbox lists. The two detection names are
    separate from each other for the same reason: Mail is recognised through the navigation
    of the signed in account and the other two apps through the capabilities document, so a
    single "detection" counter would hide which of the two a cold cache paid for.
    """
    if path.endswith("/cloud/capabilities"):
        return "capabilities"
    if path.endswith("/core/navigation/apps"):
        return "navigation"
    if path.endswith("/search/providers"):
        return "search-providers"
    if "/search/providers/" in path:
        return "search-query"
    if "/apps/mail/account/list" in path:
        return "mail-accounts"
    if "/apps/mail/ocs/mailboxes" in path:
        return "mail-mailboxes"
    if "/apps/mail/" in path:
        return "mail-other"
    if "/apps/spreed/" in path:
        return "talk"
    if path.startswith("/remote.php/dav"):
        return "calendar-dav"
    return "other"


class RequestCounter:
    """An httpx request hook that tallies outgoing requests by leg.

    A **request** hook and not a response hook: the question of measurement 2 is what this
    server sends, and a response hook would miss a request that never got an answer, which is
    precisely the shape of a leg that ran into its budget.
    """

    def __init__(self) -> None:
        self.legs: list[str] = []

    async def __call__(self, request: httpx.Request) -> None:
        self.legs.append(leg_of(request.url.path))

    def reset(self) -> None:
        self.legs.clear()

    def tally(self) -> dict[str, int]:
        counted: dict[str, int] = {}
        for leg in self.legs:
            counted[leg] = counted.get(leg, 0) + 1
        return counted

    @property
    def total(self) -> int:
        return len(self.legs)


@pytest.fixture
async def alice(exapp_env: dict[str, str]) -> AsyncIterator[NcClients]:
    """The account that owns the mail account, the conversations and the tables here."""
    clients = _appapi_clients(exapp_env, exapp_env["alice"])
    async with clients.client:
        capabilities.clear_cache()
        yield clients


@pytest.fixture
async def counted(exapp_env: dict[str, str]) -> AsyncIterator[tuple[NcClients, RequestCounter]]:
    """The same identity, with a counter on the wire."""
    counter = RequestCounter()
    clients = _appapi_clients(exapp_env, exapp_env["alice"], {"request": [counter]})
    async with clients.client:
        yield clients, counter


# --------------------------------------------------------------------------------------
# Shared readers: a real hit, and the counters of one conversation
# --------------------------------------------------------------------------------------


async def first_hit(
    clients: NcClients, kind: str, terms: tuple[str, ...]
) -> tuple[dict[str, Any], str] | None:
    """The first search hit of one kind over a list of terms, plus the term that found it.

    ``unified_search`` and not a client of its own: the ids this returns are the ids a model
    receives, and an id built anywhere else would be an id nobody can get hold of.
    """
    for term in terms:
        answer = await search_tools.unified_search(
            clients, query=term, limit=search_tools.MAX_LIMIT
        )
        for hit in answer["results"]:
            if hit.get("kind") == kind and hit.get("resolvable") is not False:
                return hit, term
    return None


async def room_counters(clients: NcClients, token: str) -> dict[str, Any]:
    """The three unread fields of one conversation, read over the **conversation list**.

    This is the measuring instrument of measurement 5 and it is deliberately a different route
    from the one under test (plan 10-08). ``unread_mention_direct`` travels along because a
    notification acknowledged by a read would show up there first, and "as far as the
    conversation list shows it" is the honest scope of that statement.
    """
    answer = await talk_tools.browse(
        clients, level="conversations", limit=talk_tools.MAX_CONVERSATIONS
    )
    room = next(
        (item for item in answer["results"] if str(item.get("token")) == token),
        None,
    )
    assert room is not None, f"the conversation {token} is not in the list of this account"
    return {
        "unread": int(room["unread"]),
        "unread_mention": bool(room["unread_mention"]),
        "unread_mention_direct": bool(room["unread_mention_direct"]),
    }


def budget_misses(bundle: dict[str, Any]) -> list[dict[str, str]]:
    """The ``degraded`` entries that say a leg ran into its budget, and only those."""
    return [
        entry for entry in bundle.get("degraded", []) if BUDGET_WORDING in entry.get("reason", "")
    ]


# --------------------------------------------------------------------------------------
# Measurement 1: the wall clock of four legs, against the reference of plan 04-04
# --------------------------------------------------------------------------------------


async def test_the_wall_clock_of_four_legs_stays_under_one_budget(alice: NcClients) -> None:
    """Minimum, median and maximum for both detail levels, plus what degraded on the way.

    The upper bound this asserts is :data:`context.CALENDAR_BUDGET`, the largest of the four
    ceilings, and the reason is the rule of the module rather than a round number: the legs run
    in one ``gather``, so the wall clock of a healthy call is the **maximum** of the parts and
    never their sum. A sequential regress of four legs would add three of those ceilings on top
    and blow through this assertion, which is exactly the failure T-11-40 describes. A tighter
    bound taken from the reference itself (0.84 s) would instead go red on a busy laptop and
    measure the machine rather than the code.
    """
    rows: dict[str, dict[str, float]] = {}
    for detail in (context_tools.SHORT, context_tools.FULL):
        timings: list[float] = []
        misses: list[dict[str, str]] = []
        degraded: list[dict[str, str]] = []
        excerpts = 0
        for _ in range(RUNS):
            started = time.perf_counter()
            bundle = await context_tools.prepare_context(
                alice, query=MEASUREMENT_QUERY, detail=detail
            )
            timings.append(time.perf_counter() - started)
            misses.extend(budget_misses(bundle))
            degraded = list(bundle.get("degraded", []))
            excerpts = sum(
                1 for hits in bundle["results"].values() for hit in hits if hit.get("excerpt")
            )
            for key in ("events", "results", "talk", "mail"):
                assert key in bundle, (
                    f"the bundle of this instance has no {key!r}: {sorted(bundle)}"
                )

        rows[detail] = {
            "min": min(timings),
            "median": float(median(timings)),
            "max": max(timings),
        }
        assert not misses, f"a leg ran into its budget at detail={detail!r}: {misses!r}"
        assert max(timings) <= context_tools.CALENDAR_BUDGET, (
            f"detail={detail!r} took {max(timings):.2f} s, above the largest single ceiling of "
            f"{context_tools.CALENDAR_BUDGET:g} s; four legs in one gather cost the maximum of "
            f"the parts, so this is what a sequential regress looks like"
        )
        note(
            f"wall clock detail={detail!r}: min {rows[detail]['min']:.2f} s, "
            f"median {rows[detail]['median']:.2f} s, max {rows[detail]['max']:.2f} s "
            f"({RUNS} runs, {excerpts} excerpts, degraded={degraded or 'empty'})"
        )

    note(
        f"reference plan 04-04 (two legs): short {REFERENCE_SHORT:.2f} s, "
        f"full {REFERENCE_FULL:.2f} s; today with four legs: "
        f"short {rows[context_tools.SHORT]['median']:.2f} s, "
        f"full {rows[context_tools.FULL]['median']:.2f} s (median)"
    )

    # The four budget comments in context.py need a number each, and the wall clock of the
    # whole bundle cannot give it: it is the maximum of the parts and says nothing about which
    # part it came from. So every leg is timed once more on its own, through the very helper
    # the bundle calls, warm, because the capabilities cache is primed by the runs above.
    start, end = context_tools._window()
    legs: dict[str, tuple[float, float | None]] = {}
    for name, budget, call in (
        (
            "search",
            None,
            lambda: search_tools.unified_search(
                alice, query=MEASUREMENT_QUERY, limit=context_tools.SEARCH_LIMIT
            ),
        ),
        (
            "calendar",
            context_tools.CALENDAR_BUDGET,
            lambda: context_tools._events(alice, start, end),
        ),
        ("talk", context_tools.TALK_BUDGET, lambda: context_tools._talk(alice)),
        ("mail", context_tools.MAIL_BUDGET, lambda: context_tools._mail(alice)),
    ):
        taken: list[float] = []
        for _ in range(RUNS):
            started = time.perf_counter()
            await call()
            taken.append(time.perf_counter() - started)
        legs[name] = (float(median(taken)), budget)
        if budget is not None:
            assert max(taken) <= budget, (
                f"the {name} leg took {max(taken):.2f} s and its budget is {budget:g} s; "
                f"the budget bites in the healthy case and is too tight"
            )
        note(
            f"leg {name}: median {legs[name][0]:.2f} s, max {max(taken):.2f} s over {RUNS} runs, "
            + (
                f"budget {budget:g} s, headroom factor {budget / max(taken):.0f}x"
                if budget is not None
                else "no budget of its own (the search carries the per provider ceiling)"
            )
        )


# --------------------------------------------------------------------------------------
# Measurement 2: the requests of one bundle, cold cache and warm cache
# --------------------------------------------------------------------------------------


async def test_the_request_cost_of_one_bundle_cold_and_warm(
    counted: tuple[NcClients, RequestCounter],
) -> None:
    """The 1+N sentence of CTX-02, counted on the wire instead of estimated in a docstring.

    Two runs, because the cost of this bundle is two numbers and not one: on a cold cache the
    mail leg pays up to two detection requests (the capabilities document for Talk, the
    navigation of the signed in account for Mail), and both answers live in one entry for
    ``capabilities.TTL_SECONDS`` seconds, so a second bundle inside a minute pays neither
    again. Those two numbers are what "measured instead of estimated" means here.
    """
    clients, counter = counted

    capabilities.clear_cache()
    counter.reset()
    await context_tools.prepare_context(clients, query=MEASUREMENT_QUERY)
    cold = counter.tally()
    cold_total = counter.total

    counter.reset()
    await context_tools.prepare_context(clients, query=MEASUREMENT_QUERY)
    warm = counter.tally()
    warm_total = counter.total

    accounts = await mail_tools.browse(clients, level="accounts", limit=mail_tools.MAX_LIMIT)
    instance_accounts = len(accounts["results"])
    expected_boxes = min(instance_accounts, context_tools.MAX_MAIL_ACCOUNTS)

    for label, tally in (("cold", cold), ("warm", warm)):
        assert tally.get("mail-accounts") == 1, (
            f"{label}: the mail leg asked the account list {tally.get('mail-accounts')} times, "
            f"and the cost sentence says exactly one"
        )
        assert tally.get("mail-mailboxes", 0) == expected_boxes, (
            f"{label}: {tally.get('mail-mailboxes', 0)} mailbox lists for "
            f"{instance_accounts} accounts, expected {expected_boxes}"
        )
        assert "mail-other" not in tally, (
            f"{label}: the mail leg touched a route beyond the two of the cost sentence: {tally!r}"
        )

    detection_cold = cold.get("capabilities", 0) + cold.get("navigation", 0)
    detection_warm = warm.get("capabilities", 0) + warm.get("navigation", 0)
    assert detection_warm == 0, (
        f"the warm run paid {detection_warm} detection requests; the cache of "
        f"{capabilities.TTL_SECONDS:g} s did not hold: {warm!r}"
    )
    assert detection_cold >= 1, f"the cold run paid no detection request at all: {cold!r}"
    assert detection_cold < cold_total, "the cold run consisted of detection requests alone"

    note(
        f"requests per bundle, cold capabilities cache: {cold_total} "
        f"({', '.join(f'{name}={count}' for name, count in sorted(cold.items()))})"
    )
    note(
        f"requests per bundle, warm capabilities cache: {warm_total} "
        f"({', '.join(f'{name}={count}' for name, count in sorted(warm.items()))})"
    )
    note(
        f"mail cost sentence measured: 1 account list plus {expected_boxes} mailbox list(s) for "
        f"{instance_accounts} account(s), plus {detection_cold} detection request(s) cold and "
        f"{detection_warm} warm (TTL_SECONDS={capabilities.TTL_SECONDS:g})"
    )


# --------------------------------------------------------------------------------------
# Measurement 3: the digest passes a counter and not a message count
# --------------------------------------------------------------------------------------


def _settles_the_question(row: dict[str, Any]) -> bool:
    """Whether one measured conversation settles that ``unread`` is not a message count.

    Below the window is proof on its own (further pages only add). Above the window is proof
    only without a continuation, because the older pages could otherwise close the gap.
    """
    if row["unread"] < row["readable"]:
        return True
    return row["unread"] > row["readable"] and not row["more"]


async def test_the_digest_passes_the_counter_of_the_app_and_not_a_message_count(
    alice: NcClients,
) -> None:
    """``unread`` against the number of readable messages, per conversation of this account.

    The proof is a divergence: a conversation whose ``unread`` cannot be the number of messages
    in it proves that the field is the counter of the app. The plan expected that divergence at
    the documented trap T12 (``unread == 1`` with an empty history). This instance does not
    carry that case right now, and it carries a stronger one instead: a conversation with
    several readable messages and ``unread == 0``. Both say the same thing, so the assertion is
    on the divergence and the trap case is checked additionally, when it is there.

    A divergence is only counted when one window of the history settles it, and the two
    directions settle differently. ``unread`` **below** the messages of the first window is
    proof on its own, because every further page can only add to that number. ``unread``
    **above** it is proof only when the answer carries no continuation, because otherwise the
    older pages could still make up the difference. ``truncated`` on this level does not mean
    "there is more to read": it means the app handed out a continuation id, which it does even
    for an empty window (see ``talk._messages``), so it cannot be used as a stand in for
    completeness. That is what this measurement corrected against its plan.

    ``talk_browse(level="messages")`` is safe to use as the instrument here: its client sends
    ``READ_ONLY_PARAMS``, so ``setReadMarker`` and ``markNotificationsAsRead`` are both 0 and
    the reading cannot be what moves a counter.
    """
    rooms = await talk_tools.browse(
        alice, level="conversations", limit=talk_tools.MAX_CONVERSATIONS
    )
    bundle = await context_tools.prepare_context(alice, query=MEASUREMENT_QUERY)
    digest = bundle["talk"]

    pairs: list[dict[str, Any]] = []
    for room in rooms["results"]:
        history = await talk_tools.browse(
            alice, level="messages", token=str(room["token"]), limit=talk_tools.MAX_LIMIT
        )
        pairs.append(
            {
                "token": str(room["token"]),
                "name": str(room["name"]),
                "unread": int(room["unread"]),
                "readable": int(history["count"]),
                "more": bool(history.get("truncated")),
                "waiting": bool(
                    int(room["unread"]) > 0
                    or room["unread_mention"]
                    or room["unread_mention_direct"]
                ),
            }
        )

    assert len(digest) <= context_tools.MAX_DIGEST, (
        f"the digest carries {len(digest)} entries, above MAX_DIGEST="
        f"{context_tools.MAX_DIGEST}: {digest!r}"
    )
    waiting = {row["token"] for row in pairs if row["waiting"]}
    for entry in digest:
        assert str(entry["token"]) in waiting, (
            f"the digest lists {entry['token']!r}, which has nothing waiting: {pairs!r}"
        )

    diverging = [row for row in pairs if _settles_the_question(row)]
    assert diverging, (
        "no conversation of this account settles the question of whether unread is a message "
        f"count, so this run cannot prove that it is not: {pairs!r}"
    )

    empty_history = [row for row in pairs if row["readable"] == 0 and row["unread"] > 0]
    for row in empty_history:
        entry = next((item for item in digest if str(item["token"]) == row["token"]), None)
        if entry is None:
            continue
        assert not str(entry.get("last_message") or ""), (
            f"the conversation {row['name']!r} reports unread={row['unread']} with an empty "
            f"history and still carries a preview: {entry!r}"
        )

    for row in pairs:
        note(
            f"conversation {row['name']!r}: unread={row['unread']}, "
            f"readable messages in one window={row['readable']}"
            f"{' (a continuation follows)' if row['more'] else ''}, in the digest="
            f"{any(str(item['token']) == row['token'] for item in digest)}"
        )
    note(
        f"unread is not a message count: {len(diverging)} of {len(pairs)} conversations settle "
        f"it, for example {diverging[0]['name']!r} with unread={diverging[0]['unread']} and "
        f"{diverging[0]['readable']} readable messages in one window"
    )
    note(
        f"the trap T12 case (unread > 0 with an empty readable history) is present "
        f"{len(empty_history)} time(s) on this instance"
    )


# --------------------------------------------------------------------------------------
# Measurement 4: the mail counter against the tool, and not one subject anywhere
# --------------------------------------------------------------------------------------


async def test_the_mail_counter_equals_the_mailbox_list_and_carries_no_subject(
    alice: NcClients,
) -> None:
    """``inbox_unread`` against ``mail_browse(level="mailboxes")``, plus the subject gate.

    The cross check runs through the tool and not through a second calculation in this file: a
    number recomputed here would only prove that two copies of the same arithmetic agree.

    The gate is a statement about the **counter** leg, and the query is what makes it one: it
    was chosen so the search leg reaches no mail hit, and that is asserted rather than assumed.
    Without the assertion a subject arriving through the search leg would fail this test for the
    wrong reason, or worse, a query that happened to match nothing would make the gate vacuous.
    """
    bundle = await context_tools.prepare_context(alice, query=MEASUREMENT_QUERY)
    counters = bundle["mail"]
    assert isinstance(counters, list), f"the mail key is not a list: {counters!r}"

    providers = {str(hit.get("provider")) for hits in bundle["results"].values() for hit in hits}
    assert "mail" not in providers, (
        f"the query {MEASUREMENT_QUERY!r} reached the Mail search provider, so the subject gate "
        f"below would be measuring the search leg: {providers!r}"
    )

    accounts = await mail_tools.browse(alice, level="accounts", limit=mail_tools.MAX_LIMIT)
    assert accounts["count"] >= 1, (
        "this account owns no mail account; run bash scripts/bootstrap_exapp.sh against the "
        "running topology"
    )
    expected = min(accounts["count"], context_tools.MAX_MAIL_ACCOUNTS)
    assert len(counters) == expected, (
        f"{len(counters)} counters for {accounts['count']} accounts, expected {expected}"
    )

    for entry in counters:
        boxes = await mail_tools.browse(
            alice,
            level="mailboxes",
            account_id=str(entry["account_id"]),
            limit=mail_tools.MAX_LIMIT,
        )
        inbox = next(
            (box for box in boxes["results"] if box.get("special_role") == "inbox"),
            None,
        )
        if inbox is None:
            assert "inbox_unread" not in entry, (
                f"the account {entry!r} has no inbox and still carries a counter"
            )
            continue
        assert entry["inbox_unread"] == inbox["unread"], (
            f"the bundle says inbox_unread={entry['inbox_unread']} and mail_browse says "
            f"{inbox['unread']} for the account {entry['account_id']}"
        )
        note(
            f"mail counter of account {entry['account_id']} ({entry['email']}): "
            f"inbox_unread={entry['inbox_unread']}, mail_browse(level='mailboxes') says "
            f"{inbox['unread']}"
        )

    blob = repr(bundle)
    for subject in GREENMAIL_SUBJECTS:
        assert subject not in blob, f"the subject {subject!r} reached the bundle: {blob[:400]!r}"
    note(
        f"none of the {len(GREENMAIL_SUBJECTS)} GreenMail subjects appears in the serialised "
        f"bundle ({len(blob)} characters)"
    )


# --------------------------------------------------------------------------------------
# Measurement 5: reading one message in full moves no counter
# --------------------------------------------------------------------------------------


async def test_reading_one_talk_message_in_full_moves_no_counter(alice: NcClients) -> None:
    """The claim of ``talk_client.get_message_context``, measured over the conversation list.

    The target is preferably a conversation with something unread, because a counter that is
    already zero cannot go down: a green row on a conversation with ``unread == 0`` would be
    true and would prove nothing. The printed row therefore always says which of the two cases
    it measured.
    """
    found = await first_hit(alice, "message", KIND_TERMS["message"])
    if found is None:
        pytest.skip(
            f"the search of this instance carries no Talk message hit for any of "
            f"{KIND_TERMS['message']}; a guessed message id would read a neighbouring message"
        )
    hit, term = found

    candidates = [hit]
    answer = await search_tools.unified_search(alice, query=term, limit=search_tools.MAX_LIMIT)
    candidates.extend(
        item
        for item in answer["results"]
        if item.get("kind") == "message" and item["id"] != hit["id"]
    )
    target = hit
    for candidate in candidates:
        token = str(candidate["id"]).split(":")[1]
        if (await room_counters(alice, token))["unread"] > 0:
            target = candidate
            break

    identifier = str(target["id"])
    token = identifier.split(":")[1]
    before = await room_counters(alice, token)

    fetched = await chatgpt.fetch(alice, identifier)
    assert str(fetched["text"]), "the read that is supposed to change nothing returned nothing"
    assert fetched["id"] == identifier

    after = await room_counters(alice, token)
    assert after == before, (
        f"reading {identifier} in full moved a counter of the conversation {token}: "
        f"{before} -> {after}"
    )
    note(
        f"side effect freedom of fetch({identifier!r}), read over the conversation list: "
        f"unread {before['unread']} -> {after['unread']}, "
        f"unread_mention {before['unread_mention']} -> {after['unread_mention']}, "
        f"unread_mention_direct {before['unread_mention_direct']} -> "
        f"{after['unread_mention_direct']}"
        f"{'' if before['unread'] > 0 else ' (the counter was already zero)'}"
    )


# --------------------------------------------------------------------------------------
# Measurement 6: one fetch per new id kind, every id from a real hit
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["message", "table"])
async def test_one_fetch_per_new_id_kind_from_a_real_search_hit(
    alice: NcClients, kind: str
) -> None:
    """TOOL-16 at the far end: a hit of a new kind, resolved by ``fetch``, never a guessed id."""
    found = await first_hit(alice, kind, KIND_TERMS[kind])
    if found is None:
        pytest.skip(
            f"the search of this instance carries no {kind} hit for any of {KIND_TERMS[kind]}; "
            f"a guessed id of that kind would read a foreign object"
        )
    hit, term = found

    fetched = await chatgpt.fetch(alice, str(hit["id"]))
    text = str(fetched["text"])
    metadata = fetched["metadata"] or {}

    assert fetched["id"] == hit["id"], f"fetch answered about another id: {fetched!r}"
    assert text, f"a {kind} hit resolved to an empty text: {fetched!r}"
    assert metadata.get("kind") == kind, f"the metadata does not name the kind: {metadata!r}"
    assert str(fetched["url"]).startswith(str(alice.creds.base_url)), (
        f"the url of a {kind} answer does not come from the configured base url: {fetched!r}"
    )
    note(
        f"fetch of a real {kind} hit (term {term!r}): id={hit['id']}, "
        f"title={str(fetched['title'])[:60]!r}, {len(text.encode('utf-8'))} bytes of text"
    )


# --------------------------------------------------------------------------------------
# The protocol, the end state and the one thing that may never be printed
# --------------------------------------------------------------------------------------


async def test_no_measured_line_carries_the_app_secret(exapp_env: dict[str, str]) -> None:
    """T-08-01, held over the protocol this file prints rather than promised in a comment."""
    if not _protocol:
        pytest.skip("nothing was measured in this run")

    blob = "\n".join(_protocol)
    secret = exapp_env["app_secret"]
    assert secret, "the fixture handed out an empty APP_SECRET"
    assert secret not in blob, "the measurement protocol carries the app secret"
    assert "AUTHORIZATION-APP-API" not in blob, "the protocol carries an auth header name"
    assert os.environ.get("NC_MCP_TEST_APP_PASSWORD", "\0") not in blob, (
        "the protocol carries an app password"
    )


async def test_the_measurement_protocol_of_this_run(alice: NcClients) -> None:
    """Print what was measured, and read the end state instead of assuming it.

    This file creates nothing, so there is nothing to remove. What it can be asked for is the
    state it leaves behind, and the conversation counters are the numbers that would have moved
    if any read of this run had been a write. They are the last lines of the protocol for
    exactly that reason, and the six container names go with them because a measurement without
    its topology is a number without a unit.
    """
    rooms = await talk_tools.browse(
        alice, level="conversations", limit=talk_tools.MAX_CONVERSATIONS
    )
    end_state = {str(item["token"]): int(item["unread"]) for item in rooms["results"]}
    note(f"end state, unread per conversation: {end_state}")
    note(f"topology: {', '.join(CONTAINERS)}")

    print("\n--- prepare_context with four legs, measured ---")
    for line in _protocol:
        print(f"  {line}")
    assert _protocol, "nothing was measured"
    assert len(_protocol) >= 6, f"fewer than six measured rows: {len(_protocol)}"
