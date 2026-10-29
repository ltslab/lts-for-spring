# Contributing

## Repository

| Path | Contents |
|---|---|
| [`manifest.yaml`](manifest.yaml) | each upstream project's repository, tag, and commit, and the BOM rules |
| [`patches/`](patches/) | the patches, applied in the order of each `series` file |
| [`cves/`](cves/) | one file per CVE: status, patches, and Red/Green results; `dependencies.json`: the third-party versions and their CVEs |
| [`CVES.md`](CVES.md) | the CVE list, generated from `cves/` |
| [`scripts/verify.py`](scripts/verify.py) | checks the data, applies the patches, and proves Red/Green |
| [`scripts/dependencies.py`](scripts/dependencies.py) | chooses the third-party versions of the BOM |
| [`scripts/release.py`](scripts/release.py) | builds, signs, uploads, and publishes a release |
| [`samples/`](samples/) | a Maven and a Gradle application that use the release zip; `scripts/release.py samples` checks the Maven one |
| [`compat/`](compat/) | the Maven extension and the Gradle plugin on Maven Central, published with each train, with samples |
| [`.github/`](.github/) | `ci.yml` proves Red/Green; `release.yml` publishes a release through a GitHub release; `actions/setup` installs Python and the JDK for both |

## Setup

- Python 3.10+ with PyYAML.
- JDKs 8, 17, 17.0.13, and 21 (the default), and Maven 3.9+. The patched classes compile with the JDK
  that reproduces upstream's classes, and Spring Cloud Config's need javac 17.0.13, before 17.0.14 changed
  how string concatenation converts its operands. Name the homes of JDKs that `/usr/lib/jvm` lacks in
  `LTSLAB_JDKS`, separated like `PATH`.

## Commands

```bash
scripts/verify.py                        # check that manifest.yaml, cves/, and patches/ agree
scripts/verify.py index                  # regenerate the CVE list, CVES.md, after editing cves/
scripts/verify.py apply spring-framework # apply the patches to the pinned commit in work/
scripts/verify.py cve CVE-2026-47883     # prove Red and Green, print the result to record
scripts/dependencies.py update           # choose third-party versions; see "Dependency upgrades"
ruff check --line-length 120 --select E,W,F,B,UP,I scripts  # lint the scripts, as CI does
```

Without a project or CVE, `apply` and `cve` run for all of them. On a pull request, CI checks the
data, applies the patches if they changed, and proves the CVEs whose files changed; a change to
`scripts/`, `.github/`, or `manifest.yaml`, every push to `main`, and a weekly run prove
all of them.

## Dependency upgrades

Spring and Reactor modules are patched (see "Add a CVE"). Every other library keeps Spring Boot's
version line, and the BOM raises it by these rules:

1. **Patch releases, always.** Each version property of Spring Boot's BOM moves to the newest
   release on its line: the same major and minor version, and the same qualifier, such as `.Final`.
   Spring Boot did the same in its own patch releases.
2. **A newer line, only to fix a CVE.** When a known CVE is fixed only on a newer minor line, list
   the property under `bom.lines` in `manifest.yaml`. Never take a new major version.
3. **Nothing that Spring uses may break.** japicmp compares the changed artifacts on the classpath
   of a Spring module: the dependencies that its POM declares, and their compile and runtime
   dependencies. A patch release may change public classes, as Tomcat and Netty do, but not a
   class, method, or field that a Spring jar of the BOM references. Check such a change by hand and record it under
   `bom.reviewed` if Spring still works, such as a constructor that became protected and is
   called only from subclasses.
4. **A newer line may break nothing public.** For a `bom.lines` property, any removed or less
   accessible public class, method, or field, or a changed signature or group ID, blocks it; only
   test-fixture artifacts (`*-test`, `*-tests`) are exempt. Move a blocked property to
   `bom.rejected` with the reason.
5. **Never hide an unfixed CVE.** CVEs that no compatible version fixes stay in `CVES.md` as not
   fixed, with the reason from `bom.rejected`, or "no fixed release".

`scripts/dependencies.py update` applies the rules with Maven Central, OSV, and japicmp, fails when
a rule needs a decision, and writes the chosen versions, the number of public API changes, and the CVEs to
`cves/dependencies.json`; then run `scripts/verify.py index`. The weekly CI run fails when newer
releases or advisories make the file out of date. The release notes list every raised library.

## Build a release train

```bash
scripts/release.py build     # patch and build every module of the v1 projects into dist/repository
scripts/release.py boms      # write the Spring Boot, Spring Cloud, Reactor, and Spring Data BOMs there
scripts/release.py samples   # resolve samples/maven; every rebuilt module must resolve to its -ltslab version
scripts/release.py classes   # check that each jar differs from upstream only in the classes its patches change
scripts/release.py zip       # zip dist/repository as lts-for-spring-<version>.zip
scripts/release.py sign      # sign the zip with the ltslab key
```

`build` and `central-build` stop when the train is released, on GitHub or on Maven Central, because a
released version never changes; raise `train` (and `central`) first.

No project is built: `build` swaps the patched classes, compiled by javac, into its upstream release jars,
after its unpatched sources reproduce the upstream classes byte for byte. For a project whose build weaves its
classes, `ajc` in `manifest.yaml` runs ajc after javac, as upstream's build does.

Modules keep their upstream group and artifact IDs; only the version changes, to
`<upstream version>-ltslab.<N>`, where N is the train of the release that last changed the project, by its
`patches/` directory or by a rebuild. A project whose patches are as released and that is not in `rebuild` is
not built again: `build` copies its modules, parents, and BOMs from the last release's zip, so a released
version never changes. The BOMs take the train's version. The zip holds the modules whose upstream version is
on Maven Central, without javadoc. A BOM is rewritten when it manages a rebuilt module or imports a
rewritten BOM, and the Spring Boot BOM also takes the third-party versions from
`cves/dependencies.json`.

A build change can leave a released project's classes different from the upstream build. Add that project
under the coming train's `rebuild` entry in `manifest.yaml`: `build` rebuilds it at that train although its
patches are as released, and copies every other project as before. `classes` compares every built jar with the
upstream release jar and fails when a class outside the patches differs, so a project is copied only when a
rebuild would change nothing. Keep the entry after the release: it is why the project stays at that train
until its patches or a later rebuild change it.

### Maven Central

Maven Central gets the train `central` in `manifest.yaml`, under the `io.github.ltslab` group, because a
released version can never change there. Every release publishes its BOM, Maven extension, and Gradle
plugin again at the release version, so `train` and `central` are raised together when a release goes
to both. Central has each module in `publish.modules` at the zip's version, so a release adds only the
modules of the projects whose patches changed or that it rebuilds, which keeps two releases a month within
Central's monthly publishing limits. The release workflow, run with `central`, adds the modules that Central
lacks:

```bash
scripts/release.py central-build            # copy those modules from the zip build into dist/repo as io.github.ltslab
scripts/release.py bom                      # write spring-boot-dependencies under io.github.ltslab
scripts/release.py compat                   # build the Maven extension, the Gradle plugin, and its marker
scripts/release.py central-samples          # resolve every republished module next to the published BOM
scripts/release.py bundle                   # collect what Central lacks in dist/bundle, check the POMs
```

Dependencies between republished modules are rewritten to the `io.github.ltslab` coordinates.
Each module ships a placeholder javadoc jar, and its sources jar carries the documentation.
The `central-check` workflow resolves the modules from Central after a successful release; it never blocks a release.

## Release

The workflow uploads a `release` artifact with the signed zip, its `.asc`, and the deployment ID. Create the
GitHub release with `gh release create` and the maintainer's token: the organization keeps `GITHUB_TOKEN`
read-only. `sign-release` signs an existing train artifact with the CI signing subkey when a release has to be
recovered.

1. Raise `train` in `manifest.yaml` (and `central`, for a release that goes to both) and the versions in
   the README and `samples/` in a pull request.
2. Run the `release` workflow from `main` and approve the `release` environment. It builds the train and the
   samples, signs the zip with the CI signing subkey, and uploads the signed release as the `release`
   artifact. With `central`, it also uploads the modules that Maven Central lacks to the Central Portal as
   `USER_MANAGED`, waits for validation, and publishes the deployment, which cannot be undone.
3. Download the `release` artifact and create the GitHub release with `gh`:
   ```bash
   gh run download <run> -n release -D dist
   version=$(python -c 'import sys; sys.path.insert(0, "scripts"); import release; print(release.BOM_VERSION)')
   python scripts/release.py notes "$(cat dist/deployment.txt)" > dist/notes.md
   gh release create "$version" --target <the run's head SHA> --title "$version" --notes-file dist/notes.md \
     "dist/lts-for-spring-$version.zip" "dist/lts-for-spring-$version.zip.asc"
   ```
   The `central-check` workflow resolves the modules from Central separately.
4. In a pull request, set the released CVEs to `status: released` and run `scripts/verify.py index`.

To abandon a release, do not create it; with `central`, its modules stay on Maven Central. `drop` drops a
deployment that never published, such as one whose upload failed validation.
`scripts/release.py upload`, `publish`, and `drop` also run locally with the token in `~/.m2/settings.xml`.

## Add a CVE

1. Check that the advisory affects the pinned base and that the base lacks the upstream fix.
2. In the `apply` checkout, cherry-pick the public upstream fix with `git cherry-pick -x` and
   split it into a test commit and a fix commit that contain only the security change.
3. `git format-patch` both into `patches/<project>/<tag without v>/` as `NN-<CVE>-test.patch`
   and `NN-<CVE>-fix.patch`, and append them to `series`.
4. Add `cves/<CVE>.yaml`, modelled on an existing one: `verify.tests` lists `<module>:<test class>`. If the fix
   changes another module, add it to the project's `publish.modules` in `manifest.yaml`;
   `scripts/verify.py` checks that the list matches the patches.
5. Run `scripts/verify.py cve <CVE>`, copy the printed `red` and `green` lines into the file, and
   set `status: verified`. Run `scripts/verify.py index` to update the CVE list.
6. Open one pull request per advisory batch and state any deviation from upstream.

Red means the new test fails with an assertion; a compile error is not Red. Green means the same
tests pass after the fix. The tests run with Maven, on the upstream release for Red and on the train for
Green: javac compiles a Gradle project's tests for Surefire, and a Maven project's module runs them with its
own POM, on the module's jar and the other patched modules at that version.

## Policy

- **Provenance:** patches come only from public upstream commits, with the upstream author and
  the `cherry picked from` trailer kept. Never use commercial or private code.
- **Pinning:** a series applies only to the commit in `manifest.yaml`. For a new base, add a new
  patch directory.
- **Security fixes only:** no features, refactoring, or dependency bumps. Never weaken, skip, or
  delete an existing test.
- **Immutability:** never rewrite a released patch; add a new one.
- **Scope:** only `scope: v1` projects are built. Changing a scope needs its own pull request.
- **State:** CVE state lives only in `cves/`, not in issues, labels, or status documents.
- **Secrets:** never commit tokens, keys, or `settings.xml`. Release secrets live only in the
  `release` environment.
- **Privacy:** no local paths or personal mail addresses; write `@author ltslab` in new tests.
  `scripts/verify.py` checks the paths and addresses.
- **English:** all repository content, except upstream test data inside patches.
