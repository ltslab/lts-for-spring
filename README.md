# LTS for Spring

[![ci](https://github.com/ltslab/lts-for-spring/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ltslab/lts-for-spring/actions/workflows/ci.yml?query=branch%3Amain)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Spring Boot](https://img.shields.io/badge/Spring%20Boot-3.5.16-6db33f)](manifest.yaml)

Security fixes for applications that stay on Spring Boot 3.5.x.

> **Latest release: `3.5.16-ltslab.2`**, a Maven repository zip on the [releases page](https://github.com/ltslab/lts-for-spring/releases).
> The release notes list the fixed CVEs.

Spring Boot 3.5.x no longer receives open-source updates.
LTS for Spring, maintained by ltslab, keeps the libraries that Spring Boot 3.5.16 and Spring Cloud 2025.0.3 use free of known CVEs where a compatible fix exists.
It ships them with Spring's own group and artifact IDs, so an application changes only the versions of its Spring Boot and Spring Cloud BOMs.

## How CVEs are handled

**Spring and Reactor are patched.**
The security fixes that the Spring and Reactor teams published for newer versions are applied to the versions that Spring Boot 3.5.16 and Spring Cloud 2025.0.3 use.

- **Public fixes only.** Every change comes from a public upstream commit, never from a commercial release.
- **Proven fixes.** Each CVE has a test that fails before the fix (Red) and passes after it (Green), and CI checks both.
- **Reproducible.** Patches apply to exact upstream commits, so anyone can rebuild from public sources and this repository.
- **Same coordinates.** Every module of a patched project keeps its upstream group and artifact ID, and its version becomes `<upstream version>-ltslab.<release>`, so the modules of a project never mix versions.

| Project | Version | Release version |
|---|---|---|
| Spring Framework | 6.2.19 | `6.2.19-ltslab.1` |
| Spring Security | 6.5.11 | `6.5.11-ltslab.2` |
| Spring Authorization Server | 1.5.8 | `1.5.8-ltslab.2` |
| Spring Batch | 5.2.6 | `5.2.6-ltslab.2` |
| Spring Data JPA | 3.5.13 | `3.5.13-ltslab.2` |
| Spring Data REST | 4.5.13 | `4.5.13-ltslab.2` |
| Spring Integration | 6.5.10 | `6.5.10-ltslab.1` |
| Spring AMQP | 3.2.12 | `3.2.12-ltslab.1` |
| Spring for Apache Kafka | 3.3.16 | `3.3.16-ltslab.1` |
| Spring Cloud Config | 4.3.4 | `4.3.4-ltslab.2` |
| Spring Cloud Gateway | 4.3.5 | `4.3.5-ltslab.2` |
| Reactor Core | 3.7.19 | `3.7.19-ltslab.1` |
| Reactor Netty | 1.2.18 | `1.2.18-ltslab.1` |

The BOMs point at these versions: `spring-boot-dependencies` and `spring-boot-starter-parent` `3.5.16-ltslab.2`, `spring-cloud-dependencies` `2025.0.3-ltslab.2`, and the Reactor and Spring Data BOMs that they import.

**Other libraries are upgraded, without breaking Spring.**
Tomcat, Netty, Jackson, the JDBC drivers, and every other library in Spring Boot's BOM move to the newest patch release of the line that Spring Boot uses.
A newer minor line is taken only to fix a CVE, and only when it removes or changes no public API.
japicmp checks every upgrade against the APIs that Spring uses; [`CONTRIBUTING.md`](CONTRIBUTING.md#dependency-upgrades) has the rules.

**Unfixed CVEs are listed, not hidden.**
The [CVE list](CVES.md) shows every known CVE, patched or not, with the reason when no compatible fix exists.

## Install

### 1. Download and verify

Download `lts-for-spring-3.5.16-ltslab.2.zip` and its `.asc` signature from the [release](https://github.com/ltslab/lts-for-spring/releases/tag/3.5.16-ltslab.2).
Its SHA-256 is in the release notes.
Files are signed with the ltslab key, fingerprint `AF61 0AB6 9E41 0A04 C9B5  644A DACC 0A4F FFCA 8F8A`, published on keyserver.ubuntu.com and keys.openpgp.org:

```bash
gpg --keyserver keys.openpgp.org --recv-keys AF610AB69E410A04C9B5644ADACC0A4FFFCA8F8A
gpg --verify lts-for-spring-3.5.16-ltslab.2.zip.asc lts-for-spring-3.5.16-ltslab.2.zip
```

The zip holds one directory, `repository/`, in the Maven repository layout.

### 2. Put it in a Maven repository

**Local Maven repository**, for one machine:

```bash
unzip lts-for-spring-3.5.16-ltslab.2.zip -d ~/.m2
```

Maven then finds the artifacts with no other change.
Gradle needs `mavenLocal()` in its repositories:

```kotlin
repositories {
    mavenLocal()
    mavenCentral()
}
```

**Private repository**, for a team or CI, such as Nexus Repository, Artifactory, or Reposilite:
upload every file under `repository/` to a hosted Maven release repository at the same path.
For example, with the credentials in `~/.netrc`:

```bash
unzip lts-for-spring-3.5.16-ltslab.2.zip
cd repository
REPO_URL=https://repo.example.com/repository/maven-releases
find . -type f | while read -r f; do curl -fsS --netrc -T "$f" "$REPO_URL/${f#./}"; done
```

Then add the repository to the build, or to a mirror in `~/.m2/settings.xml`, as for any private repository.
The `-ltslab` versions exist only there, so the order of the repositories does not matter.

### 3. Change the versions

**Maven.** Change the parent version:

```xml
<parent>
  <groupId>org.springframework.boot</groupId>
  <artifactId>spring-boot-starter-parent</artifactId>
  <version>3.5.16-ltslab.2</version>
  <relativePath/>
</parent>
```

or, without the parent, the version of the imported `org.springframework.boot:spring-boot-dependencies` to `3.5.16-ltslab.2`.
With Spring Cloud, also change its BOM:

```xml
<dependency>
  <groupId>org.springframework.cloud</groupId>
  <artifactId>spring-cloud-dependencies</artifactId>
  <version>2025.0.3-ltslab.2</version>
  <type>pom</type>
  <scope>import</scope>
</dependency>
```

**Gradle.** Keep the Spring Boot plugin at `3.5.16` and import the BOMs with the dependency management plugin:

```kotlin
dependencyManagement {
    imports {
        mavenBom("org.springframework.boot:spring-boot-dependencies:3.5.16-ltslab.2")
        mavenBom("org.springframework.cloud:spring-cloud-dependencies:2025.0.3-ltslab.2")  // with Spring Cloud
    }
}
```

Without that plugin, use `enforcedPlatform(...)` rather than `platform(...)`:
Gradle orders `6.2.19-ltslab.1` below `6.2.19`, so a plain platform loses to the upstream versions that other modules ask for.

Remove any version overrides of the patched projects, such as a `spring-framework.version` property, so that the BOMs decide.
[`samples/`](samples/) has a complete Maven and Gradle project.
To upgrade to a later release, put its zip in the repository and change the same versions.

## Maven Central

Maven Central needs a group of our own, so the modules there are under `io.github.ltslab`.
Every release also goes there, at its version; this one is `3.5.16-ltslab.2`.
It has every module that a patch changes or that this release rebuilds, with the zip's version, and:

- `io.github.ltslab:spring-boot-dependencies:3.5.16-ltslab.2`, a BOM,
- `io.github.ltslab:lts-for-spring-maven-extension` and the Gradle plugin `io.github.ltslab.lts-for-spring`, version `3.5.16-ltslab.2`, which redirect references to the Spring Framework, Spring Security, Spring Batch, Reactor Core, and Reactor Netty modules; [`compat/`](compat/) has their source and samples.

The BOM, the extension, and the plugin at `3.5.16-ltslab.2` match this release's modules and third-party
versions.

The [release notes](https://github.com/ltslab/lts-for-spring/releases/tag/3.5.16-ltslab.2) list every module on Maven Central.
The zip is simpler and has this release's raised third-party versions, so prefer it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how the repository works and the rules for patches, dependencies, and releases.

## Security

Report a vulnerability privately through [GitHub](https://github.com/ltslab/lts-for-spring/security/advisories/new).
Do not open a public issue.

## License

Licensed under the [Apache License 2.0](LICENSE).
The patches keep the license and authorship of their upstream projects, whose notices are collected in [`NOTICE`](NOTICE).
The rebuilt modules are modified versions of the upstream projects: their `-ltslab` versions mark them, and they are not official Spring releases.

ltslab is an independent, non-commercial community project.
It is not affiliated with or endorsed by Broadcom or the Spring team.
"Spring" and related names are trademarks of Broadcom and are used here only to describe compatibility.
