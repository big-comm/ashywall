# Development and testing

AshyWall targets GNOME and uses GTK 4 and libadwaita. Preserve the existing
navigation, visual hierarchy and system theme. Other desktop environments
are a future scope; changes here must keep the GNOME backend working.

## Branches

- `dev-xathay`: active development, fixes and local validation.
- `main`: reviewed changes ready for integration.
- Integrate through a pull request from `dev-xathay` to `main` once the
  repository is published and CI passes.

Local work does not require a GitHub repository. Check `git remote -v`
before any future push: a configured URL does not establish that a remote
repository already exists. The intended upstream is `big-comm/ashywall`;
publishing it and configuring distribution are separate release steps.

Commits use English imperative subjects, with a body when the technical
reason needs explanation. Keep changes focused; review the staged diff
and attachments for private data before committing. Do not add automatic
AI coauthor trailers.

## Validation

On Arch/BigCommunity, install the dependencies in `pkgbuild/PKGBUILD` plus
`python-pytest`, `ruff`, `gettext`, `appstream`, `desktop-file-utils` and
`gnome-desktop-4`, then run:

```sh
./scripts/validate.sh
```

This runs pytest, Ruff, compiled translation checks, desktop and AppStream
validation, and a wheel build. Tests use temporary data and in-memory GNOME
settings. HTTP transport tests bind only to loopback; they require local
socket access, not a live provider. Do not skip them to work around a
restricted execution environment.

CI follows the branch and read-only permission conventions of BigSound,
and the Arch package job used by BigIris, adapted for Python. It validates
both branches and pull requests, then builds the checked-out commit as an
unprivileged user. It has no package publishing or release job.

For an isolated GUI session, use a private session bus and temporary
`XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` and `XDG_STATE_HOME`,
with `GSETTINGS_BACKEND=keyfile`. Do not run GUI tests against the real
desktop's settings or library. Check light and dark themes, narrow windows,
wallpaper navigation, Reading and shutdown. Applying wallpapers should
only be exercised with an in-memory settings backend during automation.

## Local packages and distribution testing

After committing and reviewing the source:

```sh
./scripts/build-local-package.sh
```

The helper archives `HEAD`, verifies that archive and runs the package's
tests. Build artifacts stay in ignored `dist/packages/`; the source is
never cloned from an unpublished or outdated remote. Close the running
application before installing a selected package through `pacman -U`.
The application version and the distribution's date-based package version
are distinct; record the source commit alongside each testing package.

When preparing distribution through BigCommunity:

1. Review the tracked files and reachable history before first publication.
   Local planning material, test profiles and screenshots must stay out of
   the publication unless intentionally reviewed.
2. Publish the reviewed source to the intended repository and confirm the
   source revision used by the distribution's build recipe. The current
   upstream PKGBUILD follows the remote default branch; do not assume that
   it builds an unmerged `dev-xathay` commit.
3. Submit a package to BigCommunity's **testing package repository** through
   its established packaging process, keeping the source revision and build
   evidence. A development Git branch does not select a package channel.
4. Validate fresh installation and upgrade on GNOME, desktop application
   discovery, translations, existing data, online sources, Reading, memory
   under extended use and suspend/resume before stable promotion.

The exact testing-repository submission process must be checked in the
distribution's packaging repository before automating publication.

Release validation also requires all 29 BigCommunity language targets,
including English source. Run `./scripts/validate.sh --release` and follow
[locale/README.md](locale/README.md). Missing translations block release
readiness even when development tests pass.

## Online availability

Provider outages are independent of package installation. For example,
Cloudflare documents HTTP 521 as an origin server refusing its connections:
<https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-521/>.
AshyWall keeps the HTTP status, shows the provider as unavailable and offers
a manual retry. An error page is not proof of a client regression, and a
successful unit test is not proof that the remote service is online.

Do not replace official endpoints with unofficial mirrors, disable TLS
validation or safe-for-work filters, or add aggressive retries to hide an
outage. Check provider availability separately from deterministic tests.
