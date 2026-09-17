<!-- GSD:project-start source:PROJECT.md -->
## Project

**MCP Connector für Nextcloud (Arbeitstitel)**

Ein schlankes MCP-only-ExApp für Nextcloud: Nutzer installieren es per Klick aus dem Nextcloud App Store und verbinden damit ihre Nextcloud (Dateien, Kalender, Notizen, Aufgaben, Kontakte) als Werkzeug mit KI-Assistenten wie Claude, MUCGPT, Cursor oder eigenen Agenten. Zielgruppe v1: Entwickler und Selfhoster; ab Phase 2 deutsche Behörden und Unternehmen mit Datenschutz-Anforderungen (souveräner Arbeitsplatz, openDesk).

**Core Value:** Die zugänglichste und sauberste MCP-Anbindung für Nextcloud: per Klick installierbar, spec-konformes OAuth statt App-Passwort-Gebastel, und der Assistent sieht niemals mehr als der angemeldete Nutzer.

### Constraints

- **Timeline**: v1 lauffähig + App-Store-Einreichung vor der Nextcloud Conference September 2026 - harte Deadline, notfalls Scope kürzen, nie den Termin
- **Tech stack**: Python 3.13 + offizielles MCP-SDK (mcp>=2.0,<3, Fallback-Pin >=1.29,<2), uv als Toolchain (lokales System-Python ist defekt), Docker/WSL2 für lokale Test-Nextcloud
- **Lizenz**: AGPL-3.0 - passt zur Nextcloud-Ökosystem-Kultur und maximiert die Chance offizieller Übernahme
- **Repo**: public auf GitHub street1983nk (privates Konto, NICHT Akara-GitLab) - Konto-Trennungs-Regel
- **Solo-Betrieb**: Ein Entwickler; Wartungsaufwand pro Feature zählt, kuratiert schlank schlägt breit
- **Sprache**: Code/README Englisch (internationales Nextcloud-Publikum), Projektkommunikation Deutsch; keine Em-Dashes, echte Umlaute in deutschen Texten
- **Security**: Der MCP darf nie mehr sehen als der angemeldete Nutzer (Berechtigungs-Durchgriff); keine destruktiven Writes in v1
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Wichtigster Befund zuerst: Die SDK-Lage hat sich geaendert
- **mcp 2.0.0 ist seit 28.07.2026 stabil (GA)**, released zeitgleich mit der MCP-Spec 2026-07-28 (verifiziert: PyPI + GitHub-Release-Notes)
- **Die 1.x-Linie ist offiziell im Maintenance-Mode**: nur noch Security-Fixes, letzte Version 1.29.0 (28.07.2026)
- v2 bedient **beide Protokoll-Aeren gleichzeitig** aus demselben `MCPServer`: 2026-07-28 (stateless, kein Handshake) und alle 2025er-Clients (Claude.ai, Cursor etc.), ohne Konfiguration
- Die Decorator-API ist unveraendert; `FastMCP` heisst jetzt `MCPServer`; offizielle Migrationsanleitung existiert
## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.13 | Runtime | Projekt-Constraint; von allen gewaehlten Libs unterstuetzt (mcp >=3.10, nc_py_api 3.10-3.13) |
| uv | latest | Toolchain (venv, lock, run) | Projekt-Constraint (System-Python defekt); auch im Docker-Image als Installer nutzen |
| mcp[cli] | >=2.0,<3 (aktuell 2.0.0) | Offizielles MCP-SDK: Server, stdio + Streamable HTTP, Auth-RS-Seite | GA seit 28.07.2026; v1 nur noch Security-Fixes; bedient 2025er- und 2026er-Clients gleichzeitig; stateless HTTP nativ; TokenVerifier + RFC-9728-PRM eingebaut (HIGH) |
| nc_py_api[app] | >=0.30,<1 (aktuell 0.30.3) | ExApp-Framework: AppAPI-Handshake, Auth-Header, Settings-UI, User-Kontext | Offiziell empfohlene Python-Basis fuer ExApps (cloud-py-api, Nextcloud-nah); async-first; Declarative-Settings-API (`ex_app.ui.settings`) deckt "Per-User-Verwaltung in NC-Settings" direkt ab (HIGH) |
| FastAPI | >=0.133 (aktuell 0.141.1) | ASGI-App-Rahmen fuer den ExApp-Teil | nc_py_api-Runner ist FastAPI-basiert (harte Dependency); MCP-App wird per `streamable_http_app()` als Sub-App gemountet (HIGH) |
| httpx | 0.28.x | Async-HTTP-Client fuer alle Nextcloud-User-APIs (WebDAV, CalDAV, CardDAV, OCS, Notes/Deck-REST) | Ein Client fuer alles, voll async, testbar (MockTransport); DAV ist nur HTTP mit XML-Bodies, keine Spezial-Lib noetig (HIGH) |
| Uvicorn | >=0.31 (aktuell 0.52.3) | ASGI-Server im Container | Standard; bereits transitive Dependency von mcp und nc_py_api[app] (HIGH) |
### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| lxml | aktuell | DAV-XML bauen/parsen (PROPFIND, REPORT, SEARCH) | Fuer WebDAV-SEARCH und CalDAV/CardDAV-REPORT-Bodies |
| icalendar | 7.x (aktuell 7.2.2) | RFC-5545-Parsing/Erzeugung (Termine, Aufgaben) | CalDAV-Antworten parsen, VEVENT/VTODO fuer Writes bauen |
| recurring-ical-events | >=2.0 | Recurrence-Expansion (RRULE) | Wenn Kalender-Tools Termine in Zeitfenstern aufloesen sollen (prepare_context) |
| vobject | 0.9.9 | vCard-Parsing (Kontakte) | CardDAV-Antworten (VCF) lesen |
| PyJWT[crypto] | >=2.10 (aktuell 2.13.0) | Access-Token signieren/validieren (RS256/ES256) | Fuer selbst ausgestellte OAuth-Tokens; bereits transitive Dependency von mcp 2.0, kostet nichts extra |
| pydantic | >=2.12 | Modelle, Tool-Schemas | Von mcp 2.0 erzwungen (>=2.12); ohnehin Standard |
### Development Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| pytest + pytest-asyncio | Test-Runner | Tool-Funktionen freistehend testen (InfraNode-Pattern) |
| In-Memory `Client(mcp)` | MCP-Tests ohne Netzwerk | v2-Feature: Client direkt gegen das Server-Objekt, ideal fuer Tool-Contract-Tests |
| ruff | Lint + Format | Pflicht laut globalen Regeln; `ruff check .` + `ruff format --check .` vor Push |
| Docker/WSL2 + juliusknorr/nextcloud-docker-dev | Lokale Wegwerf-Nextcloud | Mehrere NC-Versionen parallel, Keycloak, LDAP; aktiv gepflegt |
| Official `nextcloud:apache` Image | CI-Integrationstests | Ein Container + `occ app:enable app_api` + manual-install-Registrierung, schlanker als docker-dev |
| docker buildx | Multi-Arch-Image (amd64+arm64) | ExApp-Image nach ghcr.io; ARM-Selfhoster sind relevante Zielgruppe |
## Die 5 Entscheidungsfelder im Detail
### 1. MCP-SDK: mcp 2.x, nicht FastMCP, nicht 1.27
- **mcp 2.0.0 (GA)**: siehe Befund oben. Auth-RS-Seite eingebaut: `TokenVerifier`-Protocol (eine async-Methode), `AuthSettings(issuer_url, resource_server_url, required_scopes)`, automatischer RFC-9728-Endpoint `/.well-known/oauth-protected-resource/mcp`, 401 mit `WWW-Authenticate`-Pointer, `get_access_token()` in jedem Handler. Genau der Spec-Flow, den Claude.ai-Connectoren erwarten.
- **stdio**: im selben SDK; Achtung: stdio hat konstruktionsbedingt keine Authorization (kein Header), Security-Grenze ist der startende Prozess. stdio-Modus daher mit App-Passwort/Login-Flow-v2 aus Env/Config.
- **FastMCP standalone (3.4.7 stabil, 4.0.0b2 beta)**: NICHT nehmen. Drittanbieter-Fork mit eigener Auth-Schicht und grosser Dependency-Flaeche; "FastMCP 4.x" ist Beta, nicht stabil. Fuer die Uebernahme-Story beim Nextcloud-Team (context_agent nutzt das offizielle SDK) ist das offizielle SDK strategisch richtig.
- **InfraNode-Wiederverwendung**: Patterns (Schema-Diaet, Annotationen, Graceful Degradation) portieren sich 1:1; nur Import-Umbenennung `FastMCP` -> `MCPServer` und Transport-Args von Konstruktor nach `run()`.
### 2. ExApp-Packaging: AppAPI + nc_py_api + HaRP
- **AppAPI** ist die Server-Seite (Nextcloud-App), das eigene Artefakt ist die ExApp: Docker-Image + `appinfo/info.xml` mit `<external-app><docker-install><registry>ghcr.io</registry><image>...</image><image-tag>...</image-tag>`.
- **Deploy Daemon**: HaRP (High-performance AppAPI Reverse Proxy) ist der empfohlene Weg ab NC 32 und routet Requests direkt zur ExApp (am PHP-Prozess vorbei, wichtig fuer Streamable-HTTP-Performance). Docker Socket Proxy (DSP) ist deprecated, Entfernung geplant in NC 35. Nichts DSP-Spezifisches bauen.
- **nc_py_api[app]** uebernimmt: Registrierungs-Handshake (`/init`, `/heartbeat`, `/enabled`), AppAPI-Auth-Header-Validierung, `NextcloudApp` mit User-Kontext (Impersonation fuer berechtigungstreue Calls), Declarative Settings (`ex_app.ui.settings` mit Text/Password/Checkbox/Select-Feldern) fuer die geforderte Per-User-Verwaltung.
- **Dev-Modus**: AppAPI `manual-install`-Daemon registriert die ExApp als lokalen Prozess (uvicorn), kein Container-Rebuild pro Iteration. Container nur fuer Release.
- **Zielversionen**: Das Fenster steht seit 0.1.14 (17.09.2026) auf min-version 32, max-version 35, gemessen gegen eine laufende 35.0.0 (docs/nc35-evidence.md). Die alte Empfehlung "max 34" ist damit erledigt; beim naechsten Hauptrelease von Nextcloud genauso verfahren: erst messen, dann das Fenster heben, Store-Release als eigener Entscheid danach.
### 3. Nextcloud-Client-APIs: httpx roh statt DAV-Libraries
- **Primaer httpx** fuer alles: WebDAV (`remote.php/dav/files/`, SEARCH), CalDAV/CardDAV (REPORT via lxml-XML), Notes-REST (`/apps/notes/api/v1`), Deck-REST (`/apps/deck/api/v1.x`), OCS Unified Search (Header `OCS-APIRequest: true`, `Accept: application/json`). Der Community-Platzhirsch (cbcoutinho, 110+ Tools) faehrt denselben Roh-HTTP-Ansatz, das ist der bewaehrte Weg.
- **Warum keine DAV-Lib als Kern**: `caldav` 3.2.1 ist sync (niquests-basiert), muesste per Thread-Offload in den async-Server; `aiodav` 0.1.14 ist verwaist und kaum verbreitet. Fuer ~6 kuratierte DAV-Tools lohnt keine zweite HTTP-Stack-Abhaengigkeit.
- **Parsing ausgelagert**: icalendar 7.x (ICS), vobject (VCF), recurring-ical-events (RRULE). Das ist der schwierige Teil von CalDAV, nicht der Transport.
- **nc_py_api als Zweitweg**: `nc.files` (WebDAV-Wrapper) und `nc.cal` (Extra `[calendar]`, nutzt intern caldav) existieren und funktionieren im AppAPI-User-Kontext. Pragmatik: nc_py_api dort nutzen, wo der Call ueber AppAPI-Impersonation laeuft, httpx dort, wo mit User-Credentials (App-Passwort) direkt gegen `remote.php` gesprochen wird.
### 4. OAuth 2.1 nach MCP-Authorization-Spec
- **RS-Haelfte (geschenkt)**: mcp 2.0 liefert `TokenVerifier`, `AuthSettings`, RFC-9728-PRM-Endpoint und 401-Discovery-Flow komplett. Nichts selbst bauen.
- **AS-Haelfte (selbst bauen, klein)**: Die SDK-Doku sagt explizit, das SDK stellt keinen Authorization Server (das aeltere `auth_server_provider=`-Argument wird fuer neue Server ausdruecklich abgeraten). Benoetigt werden 4 Endpoints als FastAPI-Routen: RFC-8414-AS-Metadata (`/.well-known/oauth-authorization-server`), `/authorize` (PKCE S256, leitet in die Nextcloud-Login-Session bzw. Login Flow v2), `/token` (Code-Exchange + Refresh), `/register` (RFC 7591 Dynamic Client Registration, noetig fuer Plug-and-play mit Claude.ai/ChatGPT).
- **Token-Format**: Selbst signierte JWTs via PyJWT[crypto] (bereits mcp-Dependency), Mapping Token -> Nextcloud-User-Id + Scopes in eigener Storage; der `TokenVerifier` prueft Signatur + Ablauf + Scopes lokal, kein Introspection-Roundtrip.
- **Warum nicht Authlib als AS-Framework**: Authlib 1.7.2 ist maechtig, aber die AS-Integrationen sind Flask/Django-first; fuer 4 massgeschneiderte Endpoints (Bridge zu Nextcloud-Identitaeten, kein generischer IdP) ist Handarbeit + PyJWT schlanker und auditierbarer. joserfc 1.7.4 nur nachruesten, falls JWE/JWKS-Rotation gebraucht wird.
- **Warum nicht Nextclouds oauth2-App als AS**: kein DCR, keine Scopes, Tokens mit Vollzugriff; genau die Luecke, die das Projekt fuellt. Die Community-OIDC-Provider-App (h2ck/oidc) waere eine optionale Fremd-App und bricht "per Klick installierbar" (LOW confidence als spaetere Integrationsoption).
### 5. Test-Setup: Wegwerf-Nextcloud
- **Lokal (WSL2)**: `juliusknorr/nextcloud-docker-dev` (ehem. julius-haertl). Aktiv gepflegt, mehrere NC-Versionen parallel (wichtig fuer min/max-version-Matrix 31-34), Keycloak/LDAP inklusive (nuetzlich fuer spaetere Behoerden-Szenarien). AppAPI dort per `occ app:enable app_api` + manual-install-Daemon aktivieren.
- **CI (GitHub Actions)**: schlanker: offizielles `nextcloud:apache`-Image als Service-Container, `occ`-Bootstrap (Admin-User, `app:enable app_api`, Notes/Deck/Calendar installieren), ExApp als Prozess via manual-install registrieren. Kein HaRP in CI noetig; HaRP-Pfad einmalig manuell vor Release testen.
- **Test-Pyramide**: (1) Tool-Funktionen pure-python mit httpx-MockTransport, (2) MCP-Contract via In-Memory `Client(mcp)`, (3) Integration gegen Docker-NC, (4) OAuth-Flow-E2E mit MCP-SDK-`Client` + OAuth-Provider gegen die laufende ExApp. Alle Pfade testen (Happy/Fehler/Edge/no_data, globale Regel).
## Installation
# Projekt-Setup (uv, Python 3.13)
# Dev
# Lokale Test-Nextcloud (WSL2)
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| mcp 2.x | mcp>=1.29,<2 | Nur wenn v2 bis Mitte September einen konkreten Blocker zeigt (Regression, Client-Inkompatibilitaet); Architektur bleibt portierbar |
| httpx roh fuer DAV | caldav 3.2.1 (sync, via anyio.to_thread) | Wenn Kalender-Logik komplex wird (Timezones, Free/Busy, Scheduling); caldav bundelt icalendar + recurring-ical-events |
| Eigener Mini-AS + PyJWT | Authlib 1.7.2 | Wenn der AS generisch werden soll (mehrere Grant-Types, Introspection, Revocation als Produkt-Feature) |
| nextcloud-docker-dev lokal | Offizielles nextcloud-Image ueberall | Wenn nur eine NC-Version getestet wird und Multi-Version-Matrix egal ist |
| nc_py_api-Impersonation | Nur App-Passwoerter pro User | Wenn AppAPI-User-Kontext Luecken hat (z.B. bestimmte DAV-Reports); App-Passwort via Login Flow v2 ist der robuste Fallback und ohnehin fuer stdio noetig |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| FastMCP standalone (3.x/4.0-beta) | Drittanbieter, 4.x ist Beta, eigene Auth-Abstraktionen kollidieren mit dem Spec-genauen SDK-Flow; schlechtere Upstream-Story bei Nextcloud | mcp 2.x (offiziell) |
| mcp 1.27 festnageln | 1.x ist Maintenance-only (nur Security-Fixes); Neustart auf toter Linie | mcp>=2.0,<3 |
| aiodav | 0.1.x, praktisch verwaist, kein CalDAV/CardDAV-REPORT | httpx + lxml |
| SDK-`auth_server_provider=` | Von der offiziellen Doku fuer neue Server explizit abgeraten (Vor-AS/RS-Trennungs-Design) | Eigene AS-Routen + `token_verifier=` |
| Docker Socket Proxy als Deploy-Ziel | Deprecated, Entfernung in NC 35 geplant | HaRP (NC 32+); ExApp selbst bleibt daemon-agnostisch |
| Nextcloud-oauth2-App als MCP-AS | Kein DCR, keine Scopes, Vollzugriffs-Tokens | Eigener AS im ExApp, Bridge zu NC-Login |
| requests/niquests direkt im eigenen Code | Sync im async-Server; niquests kommt ohnehin nur transitiv via nc_py_api | httpx (async) |
## Stack Patterns by Variant
- FastAPI-App (nc_py_api-Runner) mit gemountetem `mcp.streamable_http_app()` + AS-Routen
- `token_verifier` prueft eigene JWTs; `get_access_token()` liefert User-Id fuer den Nextcloud-Call
- Stateless: kein Session-State in Tools, Pagination ueber Handles (upgradefaehig, InfraNode-Pattern)
- Gleiches Server-Objekt, `mcp.run(transport="stdio")`
- Auth via App-Passwort/Login Flow v2 aus Env; kein OAuth (stdio hat keine Header)
- CI: offizielles nextcloud-Image, eine Version, manual-install
- Lokal: nextcloud-docker-dev, Versions-Matrix 31-34, HaRP-Smoke-Test vor Release
## Version Compatibility
| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| mcp 2.0.0 | pydantic>=2.12, starlette>=0.27, uvicorn>=0.31, pyjwt[crypto]>=2.10 | Verifiziert via PyPI requires_dist |
| mcp 2.0.0 | httpx2>=2.5 (Fork!) | SDK nutzt intern httpx2; koexistiert mit normalem httpx im selben Env (anderer Paketname). Eigenen Code auf httpx lassen |
| nc_py_api 0.30.3 | fastapi>=0.133, starlette>=1.0.1, niquests>=3.4 | starlette 1.6.0 aktuell; fastapi 0.141.1 verlangt starlette>=0.46, passt |
| nc_py_api[calendar] | caldav>=3.1,<4 | Nur relevant, wenn `nc.cal` genutzt wird |
| nc_py_api 0.30.3 | Nextcloud 31-33 (lt. Doku), Python 3.10-3.13 | NC-34-Support vor Phase 1 verifizieren (NC 34 stable seit 16.06.2026) |
| AppAPI HaRP | Nextcloud 32+ | DSP deprecated, Entfernung NC 35 |
| mcp 2.x Server | 2025er-Clients (Claude.ai, Cursor) + 2026er-Clients | Beide Aeren aus einem Endpoint, ohne Konfiguration (offizielle Release-Notes) |
## Confidence je Empfehlung
| Empfehlung | Confidence | Begruendung |
|------------|------------|-------------|
| mcp>=2.0,<3 | HIGH (Faktenlage) / MEDIUM (Frische) | GA + Maintenance-Status offiziell verifiziert; Restrisiko: 2.0.0 ist 2,5 Wochen alt, daher Fallback-Pin dokumentiert |
| nc_py_api + AppAPI/HaRP | HIGH | Offizielle NC-Doku + Repo verifiziert; NC-34-Badge-Lag als offener Punkt |
| httpx + Parsing-Libs | MEDIUM-HIGH | Vom Community-Platzhirsch praktisch validiert; DAV-XML-Aufwand ist der bekannte Preis |
| Eigener Mini-AS + PyJWT | MEDIUM | SDK-RS-Seite HIGH (Doku verifiziert); AS-Eigenbau ist Design-Entscheidung, gegen Claude.ai-Connector real zu validieren (frueher E2E-Test einplanen) |
| Test-Setup | MEDIUM | nextcloud-docker-dev aktiv verifiziert; CI-Rezept ist Standard-Praxis, aber nicht projektspezifisch erprobt |
## Sources
- https://pypi.org/pypi/mcp/json - Versionen/Release-Daten (2.0.0 GA 2026-07-28, 1.29.0 gleicher Tag), requires_dist (HIGH)
- https://github.com/modelcontextprotocol/python-sdk/releases - v2.0.0-Release-Notes: v1 Maintenance-Mode, beide Protokoll-Aeren, MCPServer-Rename, httpx2 (HIGH)
- https://py.sdk.modelcontextprotocol.io/run/authorization/ - TokenVerifier, AuthSettings, RFC-9728-PRM, 401-Discovery, Abraten von auth_server_provider (HIGH)
- https://py.sdk.modelcontextprotocol.io/whats-new/ - Stateless Streamable HTTP, ASGI-Mounting (HIGH)
- https://pypi.org/pypi/fastmcp/json - FastMCP 3.4.7 stabil, 4.0.0b2 Beta (HIGH)
- https://github.com/cloud-py-api/nc_py_api + PyPI - 0.30.3, NC 31-33, Python 3.10-3.13, Extras app/calendar, Declarative-Settings-Quellcode geprueft (HIGH)
- https://github.com/nextcloud/app_api + https://docs.nextcloud.com/server/stable/developer_manual/exapp_development/ - HaRP empfohlen, DSP deprecated (Removal NC 35), info.xml external-app/docker-install, manual-install fuer Dev (HIGH)
- https://github.com/cbcoutinho/nextcloud-mcp-server - Roh-HTTP-Ansatz, Auth-Modi des Platzhirschs (MEDIUM)
- https://github.com/juliusknorr/nextcloud-docker-dev - aktiv, Multi-Version, Keycloak (MEDIUM)
- https://nextcloud.com/changelog/ (via Suche) - NC 34 (Hub 26 Spring) stable 16.06.2026 (MEDIUM)
- PyPI-JSON fuer caldav 3.2.1 (sync/niquests), aiodav 0.1.14, httpx 0.28.1, authlib 1.7.2, joserfc 1.7.4, pyjwt 2.13.0, icalendar 7.2.2, vobject 0.9.9, fastapi 0.141.1, starlette 1.6.0, uvicorn 0.52.3 (HIGH)
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

Conventions not yet established. Will populate as patterns emerge during development.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

<!-- GSD:skills-start source:skills/ -->
## Project Skills

No project skills found. Add skills to any of: `.claude/skills/`, `.agents/skills/`, `.cursor/skills/`, `.github/skills/`, or `.codex/skills/` with a `SKILL.md` index file.
<!-- GSD:skills-end -->

<!-- GSD:workflow-start source:GSD defaults -->
## GSD Workflow Enforcement

Before using Edit, Write, or other file-changing tools, start work through a GSD command so planning artifacts and execution context stay in sync.

Use these entry points:
- `/gsd-quick` for small fixes, doc updates, and ad-hoc tasks
- `/gsd-debug` for investigation and bug fixing
- `/gsd-execute-phase` for planned phase work

Do not make direct repo edits outside a GSD workflow unless the user explicitly asks to bypass it.
<!-- GSD:workflow-end -->



<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd-profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->
