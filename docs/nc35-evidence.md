# Evidence for the Nextcloud 35 version window

**Date:** 2026-09-16
**Subject:** why `appinfo/info.xml` declares `max-version="35"` since this date
**Status:** four of five checks done against a running instance, one named as open

Nextcloud 35 ("Hub 26 Summer") was released on 2026-09-15. Until the window was raised,
this app declared `max-version="34"` and Nextcloud 35 refused to install it. That refusal
is not a warning, it blocks the app outright, including for people who already run it and
upgrade their server. This file records what was actually run before the window moved, so
the claim can be checked rather than believed.

## The instance the evidence comes from

At the time of the test the official Docker image for Nextcloud 35 did not exist yet:
`latest`, `stable` and `production` on Docker Hub all still resolved to 34.0.4 on
2026-09-16. The instance was therefore built from the released source package:

- `nextcloud-35.0.0.zip` from `download.nextcloud.com`
- SHA-256 `552b13b3ee32ba8892aa02578c2ff10ea46cbd83d3bfcfbc4317a26d359aa39a`,
  verified against the published checksum file
- laid over the official `nextcloud:34-apache` image, which carries PHP 8.5.9.
  Nextcloud 35 requires PHP 8.3 or newer and refuses 8.6 or newer
  (`lib/versioncheck.php`), so 8.5.9 is inside the supported range.

`occ status` on that instance reports:

```
  - installed: true
  - version: 35.0.0.10
  - versionstring: 35.0.0
```

The instance was isolated: its own container, its own volume, no published port. It did
not touch the throwaway topology from `compose.exapp.yml`.

## Check 1: AppAPI exists for Nextcloud 35

`app_api` version `35.0.0` ships with the release and is enabled. Upstream declares it for
this server generation only (`<nextcloud min-version="35" max-version="35"/>` on the
`stable35` branch). Without AppAPI no ExApp can run at all, so this is the precondition for
everything below.

## Check 2: the old window is refused (the user-facing blocker)

With the manifest as it stood, `max-version="34"`:

```
$ occ app:enable mcp_connector
App "MCP Connector" cannot be installed because it is not compatible with this version of the server.
```

## Check 3: the new window is accepted

Same instance, same app, the single value changed to `max-version="35"`:

```
$ occ app:enable mcp_connector
mcp_connector 0.1.13 enabled
```

## Check 4: the coupling to AppAPI is unchanged

This app is a manifest plus a container: it contains no PHP at all (zero `.php` files in
the repository). It therefore cannot break on a changed server PHP API. What it does depend
on is AppAPI, at exactly five points, and all five were read out of the running 35.0.0
instance rather than out of a changelog:

| What the app uses | Where it is in this repo | Present in AppAPI 35.0.0 |
|---|---|---|
| header `authorization-app-api` | `src/mcp_connector/exapp/auth.py` | yes, `lib/Middleware/AppAPIAuthMiddleware.php` |
| header `ex-app-id` | `src/mcp_connector/exapp/auth.py` | yes, `lib/Service/AppAPIService.php` |
| header `ex-app-version` | `src/mcp_connector/exapp/auth.py` | yes, same file |
| route `/ocs/v2.php/apps/app_api/ex-app/status` | `src/mcp_connector/exapp/status.py` | yes, `appinfo/routes.php` |
| route `/ocs/v2.php/apps/app_api/api/v1/ex-app/config` | `src/mcp_connector/oauth/crypto.py` | yes, same file |

## What is still open, and why it does not block the window

The end-to-end run is not part of this evidence: registering the app through a HaRP deploy
daemon on a 35 instance and calling one tool through it. That needs the full topology from
`compose.exapp.yml`, and the one on this machine is occupied.

The window was still raised, for three reasons. The refusal in check 2 is a hard block that
affects real installations today, while a possible HaRP defect would be a fixable bug on an
otherwise reachable app. The app has no PHP, so the usual source of server-upgrade breakage
does not apply to it. And the five coupling points in check 4 are the whole interface
between this app and the server, verified as present.

Two consequences follow, and they are deliberate:

- **The store release is a separate decision.** Raising the window in the repository is
  reversible; moving a tag in the store is not. The end-to-end run belongs before a release,
  not before a commit.
- **`compose.exapp.yml` and `compose.staging.yml` still pin a 34 image.** They stay that way
  until an official 35 image is published, because a topology that points at a locally built
  image is not reproducible for anyone else. Raising them is the first step of the
  end-to-end run.
