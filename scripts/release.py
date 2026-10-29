#!/usr/bin/env python3
"""Build the LTS for Spring release train into dist/.

The train ships as a GitHub release zip: a Maven repository with every module of each project on its upstream
coordinates and an -ltslab.N version, and Spring Boot and Spring Cloud BOMs that point at them.

  scripts/release.py build [PROJECT]   apply each v1 series and publish every module to dist/repository
  scripts/release.py boms              write the Spring Boot, Spring Cloud, and other BOMs to dist/repository
  scripts/release.py samples           build samples/maven against dist/repository
  scripts/release.py classes           check that every built jar differs from upstream only in patched classes
  scripts/release.py zip               zip dist/repository as the release asset
  scripts/release.py sign [KEY]        sign the zip (and dist/bundle, if any) with gpg
  scripts/release.py notes [DEPLOYMENT]  print the GitHub release notes for the train

Maven Central stays at the train in manifest.yaml `central`; these add the modules that it lacks, on that train:

  scripts/release.py central-build [PROJECT]  copy the changed modules under the ltslab group to dist/repo
  scripts/release.py central-samples [WHERE]  resolve them from dist/repo (local), the Central Portal's
                                       validated deployments (staging), or Maven Central (central)
  scripts/release.py bundle            collect the modules that Maven Central lacks into dist/bundle
  scripts/release.py upload            upload the signed bundle as USER_MANAGED and wait until Central validates it
  scripts/release.py publish DEPLOYMENT  publish a validated deployment and wait until it is on Maven Central
  scripts/release.py drop DEPLOYMENT   drop a deployment that is not published

Requires PyYAML, git, JDKs 8, 17, and 21, Maven 3.9+, and GnuPG. The Central token comes from CENTRAL_USERNAME and
CENTRAL_PASSWORD, or from the `central` server in ~/.m2/settings.xml; it is never printed.
"""

import base64
import copy
import functools
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import toolchain
import verify

ROOT, WORK = toolchain.ROOT, toolchain.WORK
DIST = ROOT / "dist"
REPO = DIST / "repo"  # the Maven Central train, under GROUP
UPSTREAM = DIST / "repository"  # the GitHub release zip, on the upstream coordinates
MANIFEST = toolchain.MANIFEST
GROUP, TRAIN, CENTRAL_TRAIN = MANIFEST["group"], MANIFEST["train"], MANIFEST["central"]
V1 = {name: p for name, p in verify.PROJECTS.items() if p["scope"] == "v1"}
POM = "http://maven.apache.org/POM/4.0.0"
URL = "https://github.com/ltslab/lts-for-spring"
SCM = {"url": URL, "connection": f"scm:git:{URL}.git",
       "developerConnection": "scm:git:ssh://git@github.com/ltslab/lts-for-spring.git"}
# POM elements that Maven Central requires.
REQUIRED = ["name", "description", "url", "licenses/license", "developers/developer",
            "scm/url", "scm/connection", "scm/developerConnection"]
# Third-party versions chosen by scripts/dependencies.py.
DEPENDENCIES = json.loads((ROOT / "cves" / "dependencies.json").read_text())
ET.register_namespace("", POM)


def q(*tags):
    """The ElementTree path of POM elements, such as q("dependencies", "dependency") or q("scm/url")."""
    return "/".join(f"{{{POM}}}{t}" for tag in tags for t in tag.split("/"))


def ltslab(v, train=TRAIN):
    """An upstream version as the train ships it."""
    return f"{v}-ltslab.{train}"


BOM_VERSION, CENTRAL_VERSION = ltslab(MANIFEST["boot"]), ltslab(MANIFEST["boot"], CENTRAL_TRAIN)


def released():
    """The releases before this train, newest first: (tag, train)."""
    tags = subprocess.run(["git", "tag", "--list", f"{MANIFEST['boot']}-ltslab.*"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout.split()
    return sorted(((t, int(t.rsplit(".", 1)[1])) for t in tags if int(t.rsplit(".", 1)[1]) < TRAIN),
                  key=lambda r: -r[1])


def rebuild_projects(train):
    """The projects that a train rebuilt although their patches had not changed, because their released classes
    did not match the upstream build."""
    return set((MANIFEST.get("rebuild") or {}).get(train) or [])


def rebuilds(project):
    """Whether the project rebuilds at this train although its patches are as released."""
    return project in rebuild_projects(TRAIN)


@functools.cache
def version(project):
    """The project's version: the train of the release that last changed it, by its patches or by a rebuild, or this
    train. A version once released never changes, so a project whose patches and rebuilds are as released keeps that
    release's modules."""
    up = str(V1[project]["tag"]).removeprefix("v")
    if rebuilds(project):
        return ltslab(up, TRAIN)
    tags = released()
    if not tags:
        return ltslab(up, TRAIN)
    if subprocess.run(["git", "diff", "--quiet", tags[0][0], "--", f"patches/{project}"],
                      cwd=ROOT).returncode:  # changes after the newest release are this train's
        return ltslab(up, TRAIN)
    for i, (tag, n) in enumerate(tags):
        if project in rebuild_projects(n):
            return ltslab(up, n)
        older = tags[i + 1][0] if i + 1 < len(tags) else None
        if older and subprocess.run(["git", "diff", "--quiet", older, tag, "--", f"patches/{project}"],
                                    cwd=ROOT).returncode:  # the patches changed at this release
            return ltslab(up, n)
    return ltslab(up, tags[-1][1])


def module_dir(artifact, ver):
    return REPO / GROUP.replace(".", "/") / artifact / ver


@functools.cache
def central_modules():
    """Every module that Maven Central has under GROUP: (upstream group, artifact ID) -> its version, the zip's."""
    return {(p["publish"]["group"], m): version(name) for name, p in V1.items() for m in p["publish"]["modules"]}


def run(cmd, cwd, log, env=None):
    with open(log, "a") as out:
        if subprocess.run(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, env=env).returncode != 0:
            print(*log.read_text().splitlines()[-40:], sep="\n")
            raise SystemExit(f"error: {' '.join(cmd[:2])} failed in {cwd.name}; see {log}")


ORDER = ["modelVersion", "parent", "groupId", "artifactId", "version", "packaging", "name", "description",
         "url", "inceptionYear", "organization", "licenses", "developers", "contributors", "mailingLists",
         "prerequisites", "modules", "scm", "issueManagement", "ciManagement", "distributionManagement",
         "properties", "dependencyManagement", "dependencies", "repositories", "pluginRepositories", "build",
         "reporting", "profiles"]


def place(root, element):
    """Insert a POM element where the model requires it, before the first element that must follow it."""
    later = ORDER[ORDER.index(element.tag.split("}")[1]) + 1:]
    for index, child in enumerate(root):
        if child.tag.split("}")[1] in later:
            root.insert(index, element)
            return
    root.append(element)


def inherit(root):
    """Copy the POM elements that Maven Central requires from the parent chain, which is on Maven Central;
    a module that inherits its licenses, developers, or scm would be rejected without them."""
    missing = {path.split("/")[0] for path in REQUIRED if root.find(q(path)) is None}
    parent = root.find(f"{{{POM}}}parent")
    while missing and parent is not None:
        g, a, v = (parent.findtext(f"{{{POM}}}{t}") for t in ("groupId", "artifactId", "version"))
        try:
            pom = upstream_pom(g, a, v)
        except urllib.error.HTTPError:
            break
        for tag in sorted(missing, key=ORDER.index):
            element = pom.find(f"{{{POM}}}{tag}")
            if element is not None:
                place(root, copy.deepcopy(element))
                missing.remove(tag)
        parent = pom.find(f"{{{POM}}}parent")


def rewrite_pom(path, project):
    """Put a built POM under GROUP for Maven Central: its own group ID, its dependencies on the modules in
    the bundle, and its upstream parent from Maven Central. A dependency on one of the project's other modules,
    which only the zip republishes, goes back to its upstream coordinates."""
    published = {a: v for (_, a), v in central_modules().items()}
    upstream = (V1[project]["publish"]["group"], str(V1[project]["tag"]).removeprefix("v"))
    tree = ET.parse(path)
    root = tree.getroot()
    group_id = root.find(f"{{{POM}}}groupId")
    if group_id is None:  # it inherits the parent's, which stays upstream
        group_id = ET.Element(f"{{{POM}}}groupId")
        parent = root.find(f"{{{POM}}}parent")
        after = parent if parent is not None else root.find(f"{{{POM}}}modelVersion")
        root.insert(list(root).index(after) + 1, group_id)
    group_id.text = GROUP
    root.find(f"{{{POM}}}version").text = path.parent.name  # build gave every module its own version
    relative = root.find(f"{{{POM}}}parent/{{{POM}}}relativePath")
    if relative is not None:
        relative.text = ""  # the parent comes from Maven Central, not the file system
    suffix = f"-ltslab.{CENTRAL_TRAIN}"
    for dep in root.iter(f"{{{POM}}}dependency"):
        g, a, ver = (dep.find(f"{{{POM}}}{t}") for t in ("groupId", "artifactId", "version"))
        ours = g.text == "${project.groupId}"  # this project's own group, so one of its modules
        if ours:
            g.text = GROUP
        if ver is not None and ver.text == "${project.version}":
            ver.text = f"{upstream[1]}{suffix}"
        if (g.text, a.text) in central_modules():
            target = GROUP, central_modules()[(g.text, a.text)]
        elif (ours or (ver is not None and ver.text.endswith(suffix))) and a.text not in published:
            # A module that only the zip republishes: its upstream coordinates, so the upstream parent's
            # dependency management, or the upstream version, applies.
            target = upstream[0], (ver.text[:-len(suffix)] if ver is not None and ver.text.endswith(suffix)
                                   else (ver.text if ver is not None else None))
        else:
            continue
        g.text = target[0]
        if ver is not None:
            ver.text = target[1]
        elif target[1] is not None:
            ET.SubElement(dep, f"{{{POM}}}version").text = target[1]
    inherit(root)
    tree.write(path, xml_declaration=True, encoding="UTF-8")


def project_modules(project):
    """Artifact ID -> upstream version of every module of a project: the ones Spring Boot or Spring Cloud manages in
    its group at its version, or from its repository at their own version, such as reactor-core-micrometer. Spring
    Data and Spring Cloud projects share a group and a version, so a Maven project's must be in its sources."""
    p = V1[project]
    g, up = p["publish"]["group"], str(p["tag"]).removeprefix("v")
    managed = toolchain.managed({}) | toolchain.managed(
        {}, ("org.springframework.cloud", "spring-cloud-dependencies", MANIFEST["cloud"]))
    sources = {ET.parse(f).getroot().findtext(q("artifactId")) for f in (WORK / project).rglob("pom.xml")
               if (WORK / project / "pom.xml").is_file()}
    modules = {a: v for (mg, a), (v, _) in sorted(managed.items())
               if mg == g and (not sources or a in sources) and (v == up or toolchain.exists(g, a, v) and
                                                                repository(g, a, v) == repository(url=p["upstream"]))}
    if up not in modules.values():
        raise SystemExit(f"error: {project}: Spring Boot {MANIFEST['boot']} and Spring Cloud {MANIFEST['cloud']} "
                         f"manage no {g} module at {up}")
    return modules


def repository(g=None, a=None, v=None, url=None):
    """The repository that a URL, or a module's POM <scm>, names, such as github.com/reactor/reactor-core."""
    if url is None:
        found = re.search(r"<scm>.*?<url>([^<]*)</url>", toolchain.get(toolchain.url(g, a, v)).decode(), re.S)
        url = found[1] if found else ""
    return re.sub(r"^(scm:)?git:|^https?://|(\.git)?/?$", "", url.strip()).lower()


def fetch_modules(g, modules, stage):
    """Download each module's POM, Gradle module metadata, jars, and sources jar to stage/<artifact>; javadoc is
    not shipped."""
    def fetch(a):
        names = {f"{a}-{modules[a]}{s}" for s in (".pom", ".module", ".jar", "-sources.jar", "-original.jar",
                                                   "-test-fixtures.jar")}
        for name in sorted(names):
            try:
                body = toolchain.fetch(f"{toolchain.CENTRAL}/{g.replace('.', '/')}/{a}/{modules[a]}/{name}")[1]
            except urllib.error.HTTPError as ex:
                if ex.code != 404:
                    raise
                continue
            (stage / a / name).write_bytes(body)
            if name.endswith(".module"):  # files that the metadata lists, such as other classifiers
                names |= {f["url"] for variant in json.loads(body)["variants"] for f in variant.get("files", [])
                          if not f["url"].endswith("-javadoc.jar")} - names

    shutil.rmtree(stage, ignore_errors=True)
    for a in modules:
        (stage / a).mkdir(parents=True)
    with ThreadPoolExecutor(os.cpu_count() or 2) as pool:
        list(pool.map(fetch, modules))


def dependency_order(patched, stage, up):
    """The patched modules, each after the ones that its POM depends on."""
    order = []
    while len(order) < len(patched):
        for a in sorted(set(patched) - set(order)):
            depends = re.findall(r"<artifactId>([^<]+)</artifactId>", (stage / a / f"{a}-{up}.pom").read_text())
            if not set(depends) & (set(patched) - set(order) - {a}):
                order.append(a)
                break
        else:
            raise SystemExit("error: the patched modules depend on each other in a cycle")
    return order


def compile_classes(jdk, target, flags, classpath, sources, out, ajc=None):
    """Compile sources into out with javac and then, for a project that aspectj-maven-plugin weaves, with ajc over
    javac's classes, as upstream builds them; ajc is (its classpath, the aspect jars, aop.xml). None, or the
    compiler's output on failure."""
    error = toolchain.javac(jdk, target, flags, classpath, sources, out)
    if error or not ajc:
        return error
    tools, aspects, xml = ajc
    done = subprocess.run([str(jdk / "bin" / "java"), toolchain.TOOL_JVM, "-cp", tools, "org.aspectj.tools.ajc.Main",
                           f"-{target}", *[f for f in flags if f == "-parameters"], "-proc:none", "-encoding", "UTF-8",
                           "-xmlConfigured", str(xml), "-aspectpath", aspects, "-cp", classpath, "-d", str(out),
                           *map(str, sources)], capture_output=True, text=True)
    return None if done.returncode == 0 else done.stdout + done.stderr


def reproduce(a, upstream, classpath, sources, out, ajc):
    """Compile the unpatched sources with each JDK and set of debug flags until they reproduce the upstream classes
    byte for byte; return (the JDK's home, its version, class-file release, flags, the classes they produce)."""
    majors = [body[7] for n, body in upstream.items() if not n.startswith("META-INF/versions/")]
    target = max(set(majors), key=majors.count) - 44  # the class-file major version of most classes
    # The attributes that -g and -parameters add show the likely flags, which every JDK tries first.
    joined = b"".join(upstream.values())
    likely = [f for f, attribute in (("-g", b"LocalVariableTable"), ("-parameters", b"MethodParameters"))
              if attribute in joined]
    tried = []
    for flags in [likely] + [f for f in (["-g", "-parameters"], ["-g"], ["-parameters"], []) if f != likely]:
        for major, version, jdk in toolchain.installed():
            if major < target:
                continue
            error = compile_classes(jdk, target, flags, classpath, sources, out, ajc)
            built = toolchain.classes(out) if error is None else {}
            wrong = sorted(n for n, body in built.items() if upstream.get(n) != body)
            if built and not wrong:
                return jdk, version, target, flags, set(built)
            why = error.strip().splitlines()[0] if error else f"{len(wrong)} classes differ, such as {wrong[0]}"
            tried.append(f"JDK {version} {' '.join(flags)}: {why}")
    raise SystemExit(f"error: {a}: no JDK and flags reproduce the upstream classes of the unpatched sources\n  " +
                     "\n  ".join(tried))


def swap_module(project, a, files, classpath, stage, ajc):
    """Swap the classes and resources of the module's patched sources into its jars and sources jar in stage."""
    p, d, work = V1[project], WORK / project, WORK / "swap" / project
    up = str(p["tag"]).removeprefix("v")
    java = sorted(r for r in files if r.startswith("java/") and r.endswith(".java"))
    resources = sorted(r for r in files if r.startswith("resources/"))
    if set(files) - set(java) - set(resources):
        raise SystemExit(f"error: {a}: cannot swap {sorted(set(files) - set(java) - set(resources))} "
                         "without the upstream build")
    jar = stage / a / f"{a}-{up}.jar"
    reference = stage / a / f"{a}-{up}-original.jar"  # before shading, if the build shades
    reference = reference if reference.is_file() else jar
    with zipfile.ZipFile(reference) as z:
        upstream = {n: z.read(n) for n in z.namelist() if n.endswith(".class")}
    originals = work / "original" / a
    shutil.rmtree(originals, ignore_errors=True)
    for r in java:
        shown = subprocess.run(["git", "show", f"{p['commit']}:{files[r]}/src/main/{r}"], cwd=d, capture_output=True)
        if shown.returncode == 0:
            (originals / r).parent.mkdir(parents=True, exist_ok=True)
            (originals / r).write_bytes(shown.stdout)
    sources = sorted(originals.rglob("*.java"))
    if not sources:
        raise SystemExit(f"error: {a}: every patched source is new, so none can reproduce the upstream classes")
    out = work / "classes" / a
    jdk, version, target, flags, stale = reproduce(a, upstream, classpath, sources, out, ajc)
    with zipfile.ZipFile(jar) as z:
        if reference != jar and any(z.read(n) != upstream[n] for n in stale):
            raise SystemExit(f"error: {a}: shading changed the patched classes, so they cannot be swapped in")
    error = compile_classes(jdk, target, flags, classpath, [d / files[r] / "src/main" / r for r in java], out, ajc)
    if error:
        raise SystemExit(f"error: {a}: the patched sources do not compile\n{error}")
    before = toolchain.constants(jdk, {n: upstream[n] for n in stale})
    after = toolchain.constants(jdk, {n: body for n, body in toolchain.classes(out).items() if n in stale})
    if any(set(before[n]) - set(after.get(n, before[n])) for n in before):
        raise SystemExit(f"error: {a}: the patches change a compile-time constant, which unpatched classes may inline")
    new = toolchain.classes(out) | {r.removeprefix("resources/"): (d / files[r] / "src/main" / r).read_bytes()
                                    for r in resources}
    for name in (f"{a}-{up}.jar", f"{a}-{up}-original.jar"):
        if (stage / a / name).is_file():
            swap_jar(stage / a / name, stale, new)
    swap_jar(stage / a / f"{a}-{up}-sources.jar", set(),
             {r.split("/", 1)[1]: (d / files[r] / "src/main" / r).read_bytes() for r in java + resources})
    print(f"{a}: {len(java)} sources compiled with JDK {version} (release {target}{''.join(' ' + f for f in flags)}), "
          f"{len(stale)} upstream classes reproduced, {len(new)} entries swapped in", flush=True)


def swap_jar(jar, stale, new):
    """Rewrite the jar without the stale entries and with the new ones."""
    data = io.BytesIO()
    with zipfile.ZipFile(jar) as old, zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as out:
        for e in old.infolist():
            if e.filename not in stale and e.filename not in new:
                out.writestr(e, old.read(e))
        # The new entries take the upstream jar's time, so that a rebuild writes the same bytes.
        stamp = old.infolist()[0].date_time
        for name, body in sorted(new.items()):
            out.writestr(zipfile.ZipInfo(name, stamp), body, zipfile.ZIP_DEFLATED)
    jar.write_bytes(data.getvalue())


def publish_modules(g, modules, stage):
    """Write every module in stage to dist/repository at -ltslab.N, with its POM and Gradle module metadata
    pointing at the project's other modules there."""
    for a, v in modules.items():
        dest = UPSTREAM / g.replace(".", "/") / a / ltslab(v)
        dest.mkdir(parents=True, exist_ok=True)
        for f in sorted((stage / a).iterdir()):
            body = f.read_bytes()
            if f.suffix == ".pom":
                body = sibling_versions(body.decode(), g, modules).encode()
            elif f.suffix == ".module":
                body = module_metadata(json.loads(body), g, modules, a, stage / a).encode()
            (dest / f.name.replace(f"{a}-{v}", f"{a}-{ltslab(v)}", 1)).write_bytes(body)


def swap(project, log):
    """Publish every module of a project from its upstream release on Maven Central, with the classes of
    the patched sources compiled by javac and swapped in. Each module first compiles the unpatched sources and
    must reproduce the upstream classes byte for byte, which picks the JDK and checks the classpath."""
    p, work = V1[project], WORK / "swap" / project
    g, up = p["publish"]["group"], str(p["tag"]).removeprefix("v")
    modules, stage = project_modules(project), work / "upstream"
    fetch_modules(g, modules, stage)
    patched = verify.patched(project)
    if set(patched) - {a for a, v in modules.items() if v == up}:
        raise SystemExit(f"error: {project}: Spring Boot does not manage {sorted(set(patched) - set(modules))} at {up}")
    order = dependency_order(patched, stage, up)
    # Published POMs leave out optional and compile-only dependencies: manifest.yaml lists them for a Gradle
    # project, and a Maven project's own POMs in its sources have them.
    (work / "pom.xml").write_text(toolchain.pom([f"{g}:{a}:{up}" for a in order] + p.get("compile", [])))
    with open(log, "a") as output:
        resolved = toolchain.resolve(work, output)
        for a in order:
            own = WORK / project / next(iter(patched[a].values()))
            if resolved and (own / "pom.xml").is_file():
                found = toolchain.resolve(own, output, "compile", WORK / project)
                resolved = found and resolved + os.pathsep + found
    if resolved is None:
        print(*log.read_text().splitlines()[-40:], sep="\n")
        raise SystemExit(f"error: {project}: Maven cannot resolve the compile classpath; see {log}")
    # The project's modules come from stage, so a patch may use what another module's patch adds.
    classpath = os.pathsep.join(next((str(stage / a / f"{a}-{v}.jar") for a, v in modules.items()
                                      if e.endswith(f"/{a}/{v}/{a}-{v}.jar")), e) for e in resolved.split(os.pathsep))
    ajc = None
    if p.get("ajc"):
        with open(log, "a") as output:
            (work / "ajc").mkdir(exist_ok=True)
            (work / "ajc" / "pom.xml").write_text(toolchain.pom([f"org.aspectj:aspectjtools:{p['ajc']['version']}"]))
            tools = toolchain.resolve(work / "ajc", output)
        aspects = [e for e in classpath.split(os.pathsep) if Path(e).parent.parent.name in p["ajc"]["aspects"]]
        if not tools or len(aspects) != len(p["ajc"]["aspects"]):
            print(*log.read_text().splitlines()[-40:], sep="\n")
            raise SystemExit(f"error: {project}: Maven cannot resolve ajc or the aspects; see {log}")
        ajc = tools, os.pathsep.join(aspects), WORK / project / p["ajc"]["xml"]
    for a in order:
        swap_module(project, a, patched[a], classpath, stage, ajc)
    publish_modules(g, modules, stage)
    if toolchain.exists(g, f"{project}-bom", up):
        rewrite_bom(g, f"{project}-bom", up, rebuilt())


def sibling_versions(pom, g, modules):
    """The upstream POM with its own version and its dependencies on the project's other modules at -ltslab.N."""
    if ET.fromstring(pom).find(q("version")) is None:  # a Maven project's POM inherits it from the upstream parent
        parent = re.search(r"<parent>.*?</parent>", pom, re.S)
        own = next(m for m in re.finditer(r"<artifactId>([^<]*)</artifactId>", pom) if not parent.start() < m.start()
                   < parent.end())
        pom = f"{pom[:own.end()]}<version>{ltslab(modules[own[1]])}</version>{pom[own.end():]}"
    for a, v in modules.items():
        pom = re.sub(rf"(<groupId>{re.escape(g)}</groupId>\s*<artifactId>{re.escape(a)}</artifactId>\s*<version>)"
                     rf"{re.escape(v)}(</version>)", rf"\g<1>{ltslab(v)}\g<2>", pom)
    return pom


def module_metadata(meta, g, modules, a, files):
    """Gradle module metadata at -ltslab.N: its version, its dependencies on the project's other modules, and its
    files renamed, with the sizes and checksums of the swapped jars."""
    meta["component"]["version"] = ltslab(modules[a])
    for variant in meta["variants"]:
        for dep in variant.get("dependencies", []):
            if dep["group"] == g and dep.get("version", {}).get("requires") == modules.get(dep["module"]):
                dep["version"]["requires"] = ltslab(dep["version"]["requires"])
        for f in variant.get("files", []):
            body = (files / f["url"]).read_bytes() if (files / f["url"]).is_file() else None
            f["name"] = f["url"] = f["url"].replace(f"{a}-{modules[a]}", f"{a}-{ltslab(modules[a])}", 1)
            if body is not None:
                f.update(size=len(body),
                         **{h: hashlib.new(h, body).hexdigest() for h in ("sha512", "sha256", "sha1", "md5")})
    return json.dumps(meta, indent=2) + "\n"


def build(project):
    """Apply the project's series and publish every module with -ltslab.N versions on its upstream
    coordinates to dist/repository, which becomes the zip."""
    d = WORK / project
    verify.apply(project)
    log, ver = d / "release.log", version(project)
    log.write_text("")
    if ver == ltslab(str(V1[project]["tag"]).removeprefix("v")):
        swap(project, log)
    else:
        copy_released(project, ver)
    print(f"{project}: {len(list(UPSTREAM.rglob(f'*-{ver}.pom')))} modules published with version {ver}")
    return True


def copy_released(project, ver):
    """Copy what the last release's zip has of the project at ver, which its patches have not changed since: its
    modules, parents, and BOMs, under its group at its versions."""
    tag = released()[0][0]
    archive = toolchain.CACHE / "releases" / f"lts-for-spring-{tag}.zip"
    if not archive.is_file():
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(toolchain.fetch(f"{URL}/releases/download/{tag}/lts-for-spring-{tag}.zip")[1])
    train, g = ver.rsplit(".", 1)[1], V1[project]["publish"]["group"].replace(".", "/")
    versions = {ltslab(v, train) for v in {*project_modules(project).values(), ver.rsplit("-ltslab.", 1)[0]}}
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if n.startswith(f"repository/{g}/") and n.split("/")[-2] in versions]
        for n in names:
            (UPSTREAM / n.removeprefix("repository/")).parent.mkdir(parents=True, exist_ok=True)
            (UPSTREAM / n.removeprefix("repository/")).write_bytes(z.read(n))
    if not names:
        raise SystemExit(f"error: {project}: {tag} has no module at {ver}")


def central_build(project):
    """Copy the modules that Maven Central lacks from dist/repository to dist/repo under GROUP, with
    their POMs rewritten; the jars are the ones the zip ships."""
    p, missing, ver = V1[project], unpublished(), version(project)
    modules = [m for m in p["publish"]["modules"] if (p["publish"]["group"], m) in missing]
    for m in modules:
        src = UPSTREAM / p["publish"]["group"].replace(".", "/") / m / ver
        if not (src / f"{m}-{ver}.pom").is_file():
            raise SystemExit(f"error: {m}:{ver} is not built; run scripts/release.py build")
        dest = module_dir(m, ver)
        dest.mkdir(parents=True, exist_ok=True)
        for f in src.iterdir():
            if f.name.startswith(f"{m}-{ver}") and f.suffix in (".jar", ".pom") and "-javadoc" not in f.name:
                shutil.copy2(f, dest / f.name)
        rewrite_pom(dest / f"{m}-{ver}.pom", project)
    print(f"{project}: {len(modules)} modules copied to {REPO.relative_to(ROOT)} as {GROUP}:*:{ver}")
    return True


def rebuilt():
    """(group, artifact) -> version of every module that build wrote to dist/repository."""
    return {(pom.parent.parent.parent.relative_to(UPSTREAM).as_posix().replace("/", "."), pom.parent.parent.name):
            pom.parent.name for pom in UPSTREAM.rglob("*-ltslab.*.pom")}


def upstream_jar(g, a, v):
    """The upstream release jar at g:a:v, cached: a swapped jar starts from it."""
    path = toolchain.CACHE / "upstream" / g.replace(".", "/") / a / f"{a}-{v}.jar"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(toolchain.fetch(toolchain.url(g, a, v, "jar"))[1])
    return path


def class_diffs(project):
    """The modules of the project whose built jar differs from the upstream release jar outside the classes that its
    patches change: [(module, [class])]. The swap keeps every other class as upstream built it, so a difference
    there means the project was copied from a release whose classes do not match upstream, or the swap is wrong."""
    p, g = V1[project], V1[project]["publish"]["group"]
    up, ver = str(p["tag"]).removeprefix("v"), version(project)
    patched = verify.patched(project)
    out = []
    for module in p["publish"]["modules"]:
        built = UPSTREAM / g.replace(".", "/") / module / ver / f"{module}-{ver}.jar"
        if not built.is_file():
            raise SystemExit(f"error: {module}:{ver} is missing; run scripts/release.py build")
        with zipfile.ZipFile(built) as z:
            got = {n: z.read(n) for n in z.namelist() if n.endswith(".class")}
        with zipfile.ZipFile(upstream_jar(g, module, up)) as z:
            reference = {n: z.read(n) for n in z.namelist() if n.endswith(".class")}
        bases = {path.removeprefix("java/").removesuffix(".java") for path in patched.get(module, {})}
        compiled = WORK / "swap" / project / "classes" / module
        if ver != ltslab(up) and compiled.is_dir():  # rebuilt in this run, so its compiled classes are current
            bases |= {f.relative_to(compiled).as_posix().removesuffix(".class").split("$", 1)[0]
                      for f in compiled.rglob("*.class")}
        wrong = sorted(n for n in set(got) | set(reference)
                       if (n not in got or n not in reference or got[n] != reference[n])
                       and n.removesuffix(".class").split("$", 1)[0] not in bases)
        if wrong:
            out.append((module, wrong))
    return out


def classes():
    """Check that every built jar is the upstream release jar with only the patched classes changed: what lets build
    copy a project whose patches are as released instead of rebuilding it."""
    bad = {project: diffs for project in V1 if (diffs := class_diffs(project))}
    for project, modules in bad.items():
        for module, names in modules:
            print(f"{project}:{module}: {len(names)} classes differ from upstream and are not patched, "
                  f"such as {names[0]}")
    count = sum(len(p["publish"]["modules"]) for p in V1.values())
    print(f"classes: {count} modules, {len(bad)} with a difference from upstream outside the patches")
    return not bad


def upstream_pom(g, a, v):
    return ET.fromstring(toolchain.get(toolchain.url(g, a, v)))


def rewrite_bom(g, a, v, built, overrides=None):
    """Write g:a:v-ltslab.N, the upstream BOM with its managed versions pointing at the rebuilt modules, at
    rewritten imported BOMs, and at overrides (properties); return that version, or None if nothing changes."""
    root = upstream_pom(g, a, v)
    props = {e.tag.split("}")[1]: e for e in root.findall(q("properties", "*"))}

    def resolve(text):  # ${property} references, which may nest
        return re.sub(r"\$\{([^}]+)\}", lambda m: v if m[1] == "project.version" else
                      resolve(props[m[1]].text) if m[1] in props else m[0], text.strip())

    wanted = []  # (version element, current version, new version)
    for dep in root.findall(q("dependencyManagement", "dependencies", "dependency")):
        element = dep.find(q("version"))
        dg, da, dv = resolve(dep.find(q("groupId")).text), dep.find(q("artifactId")).text, resolve(element.text)
        if (dg, da) in built:
            new = built[(dg, da)]
        elif dep.findtext(q("scope")) == "import":
            new = rewrite_bom(dg, da, dv, built)
        else:
            new = None
        wanted.append((element, dv, new if new and new != dv else None))
    # A property moves only when every version that uses it moves the same way; otherwise each one is set.
    users = {}
    for element, _, new in wanted:
        m = re.fullmatch(r"\$\{([^}]+)\}", element.text.strip())
        if m and m[1] in props:
            users.setdefault(m[1], []).append(new)
    changed = dict(overrides or {})
    for name, news in users.items():
        if news[0] and news.count(news[0]) == len(news):
            changed[name] = news[0]
    for element, dv, new in wanted:
        m = re.fullmatch(r"\$\{([^}]+)\}", element.text.strip())
        if new and not (m and m[1] in changed):
            element.text = new
        elif m and m[1] == "project.version":  # the BOM's own version changes below
            element.text = dv
    if not changed and not any(new for _, _, new in wanted):
        return None
    for name, value in changed.items():
        if name not in props:
            raise SystemExit(f"error: {g}:{a}:{v} has no property {name}")
        props[name].text = value
    ver = ltslab(v)
    set_version(root, ver, "Dependency management with the LTS for Spring security fixes.")
    write_pom(root, g, a, ver)
    return ver


def set_version(root, ver, note):
    """Give the POM its -ltslab.N version and say in its description that it is not an official release."""
    element = root.find(q("version"))
    if element is None:
        element = ET.Element(q("version"))
        root.insert(list(root).index(root.find(q("artifactId"))) + 1, element)
    element.text = ver
    description = root.find(q("description"))
    if description is None:
        description = ET.SubElement(root, q("description"))
    description.text = f"{(description.text or '').strip()} {note} Not an official Spring release; see {URL}.".strip()


def write_pom(root, g, a, ver):
    out = UPSTREAM / g.replace(".", "/") / a / ver
    out.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out / f"{a}-{ver}.pom", xml_declaration=True, encoding="UTF-8")


def boms():
    """Spring Boot's BOM and parent, and the Spring Cloud BOM, pointing at the rebuilt modules (and at Reactor
    and Spring Data BOMs that do), with the fixed third-party versions."""
    boot, built = MANIFEST["boot"], rebuilt()
    if DEPENDENCIES["boot"] != boot:
        raise SystemExit("error: cves/dependencies.json is for another Spring Boot; run scripts/dependencies.py update")
    bom = rewrite_bom("org.springframework.boot", "spring-boot-dependencies", boot, built, DEPENDENCIES["properties"])
    if bom != BOM_VERSION:
        raise SystemExit(f"error: spring-boot-dependencies is not {BOM_VERSION}")
    parent = upstream_pom("org.springframework.boot", "spring-boot-starter-parent", boot)
    parent.find(f"{{{POM}}}parent/{{{POM}}}version").text = BOM_VERSION
    set_version(parent, BOM_VERSION, "With the LTS for Spring security fixes.")
    write_pom(parent, "org.springframework.boot", "spring-boot-starter-parent", BOM_VERSION)
    cloud = rewrite_bom("org.springframework.cloud", "spring-cloud-dependencies", MANIFEST["cloud"], built)
    written = sorted(set(rebuilt().items()) - set(built.items()))
    print(f"{len(written)} BOMs and parents written ({', '.join(f'{a}:{v}' for (_, a), v in written)}); "
          f"{len(DEPENDENCIES['properties'])} third-party versions raised in spring-boot-dependencies")
    return bool(cloud)


def bom():
    """Spring Boot's BOM under GROUP, for Maven Central: every republished module at its ltslab version,
    and the third-party versions that the zip's BOM raises."""
    boot = MANIFEST["boot"]
    root = upstream_pom("org.springframework.boot", "spring-boot-dependencies", boot)
    root.find(q("groupId")).text = GROUP
    root.find(q("version")).text = CENTRAL_VERSION
    root.find(q("description")).text = (f"Spring Boot {boot} dependency management with the LTS for Spring "
                                        "security fixes. Not affiliated with Broadcom or the Spring team.")
    root.find(q("url")).text = URL
    scm = root.find(q("scm"))
    for child in list(scm):
        scm.remove(child)
    for name, value in SCM.items():
        ET.SubElement(scm, q(name)).text = value
    developer = root.find(q("developers", "developer"))
    for child in list(developer):
        developer.remove(child)
    ET.SubElement(developer, q("name")).text = "ltslab contributors"
    ET.SubElement(developer, q("organizationUrl")).text = URL
    props = {e.tag.split("}")[1]: e for e in root.findall(q("properties", "*"))}
    for name, value in DEPENDENCIES["properties"].items():
        if name not in props:
            raise SystemExit(f"error: spring-boot-dependencies {boot} has no property {name}")
        props[name].text = value
    managed = root.find(q("dependencyManagement", "dependencies"))
    for i, ((_, artifact), ver) in enumerate(sorted(central_modules().items())):
        dep = ET.Element(q("dependency"))
        ET.SubElement(dep, q("groupId")).text = GROUP
        ET.SubElement(dep, q("artifactId")).text = artifact
        ET.SubElement(dep, q("version")).text = ver
        managed.insert(i, dep)
    out = module_dir("spring-boot-dependencies", CENTRAL_VERSION)
    out.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(out / f"spring-boot-dependencies-{CENTRAL_VERSION}.pom",
                               xml_declaration=True, encoding="UTF-8")
    print(f"{GROUP}:spring-boot-dependencies:{CENTRAL_VERSION}: {len(central_modules())} modules, "
          f"{len(DEPENDENCIES['properties'])} third-party versions raised")
    return True


PLUGIN_ID = f"{GROUP}.lts-for-spring"
COMPAT = ["lts-for-spring-maven-extension", "lts-for-spring-gradle-plugin"]
# The Gradle plugin marker POM's licenses, developers, and scm, which Maven Central requires.
MARKER_LICENSES = ("<licenses><license><name>Apache License, Version 2.0</name>"
                   "<url>https://www.apache.org/licenses/LICENSE-2.0</url></license></licenses>")
MARKER_DEVELOPERS = (f"<developers><developer><name>ltslab contributors</name><organizationUrl>{URL}</organizationUrl>"
                     "</developer></developers>")
MARKER_SCM = (f"<scm><url>{URL}</url><connection>{SCM['connection']}</connection>"
              f"<developerConnection>{SCM['developerConnection']}</developerConnection></scm>")


def compat():
    """The Maven extension and Gradle plugin, with the coordinate map, plus the Gradle plugin marker. The map
    sends each republished module to GROUP and each raised third-party artifact to its version."""
    coordinates = WORK / "compat" / "lts-for-spring.properties"
    coordinates.parent.mkdir(parents=True, exist_ok=True)
    targets = {f"{g}:{a}": f"{GROUP}:{a}:{v}" for (g, a), v in central_modules().items()}
    targets.update(raised_artifacts())
    escaped = {ga: ga.replace(":", "\\:") for ga in targets}  # ':' separates keys in a .properties file
    coordinates.write_text("".join(f"{escaped[ga]}={target}\n" for ga, target in sorted(targets.items())))
    log = WORK / "compat" / "release.log"
    log.write_text("")
    run(["mvn", "-q", "-B", "-f", str(ROOT / "compat" / "pom.xml"), f"-Drevision={CENTRAL_VERSION}",
         f"-Dcoordinates.dir={coordinates.parent}", "-Dmaven.javadoc.skip=true", "clean", "deploy",
         f"-DaltDeploymentRepository=ltslab::{REPO.as_uri()}"], ROOT, log)
    marker = REPO / PLUGIN_ID.replace(".", "/") / f"{PLUGIN_ID}.gradle.plugin" / CENTRAL_VERSION
    marker.mkdir(parents=True, exist_ok=True)
    (marker / f"{PLUGIN_ID}.gradle.plugin-{CENTRAL_VERSION}.pom").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="{POM}">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{PLUGIN_ID}</groupId>
  <artifactId>{PLUGIN_ID}.gradle.plugin</artifactId>
  <version>{CENTRAL_VERSION}</version>
  <packaging>pom</packaging>
  <name>LTS for Spring Gradle plugin marker</name>
  <description>Gradle plugin marker for {PLUGIN_ID}.</description>
  <url>{URL}</url>
  {MARKER_LICENSES}
  {MARKER_DEVELOPERS}
  {MARKER_SCM}
  <dependencies>
    <dependency><groupId>{GROUP}</groupId><artifactId>lts-for-spring-gradle-plugin</artifactId><version>{CENTRAL_VERSION}</version></dependency>
  </dependencies>
</project>
""")
    print(f"{GROUP}:lts-for-spring-{{maven-extension,gradle-plugin}}:{CENTRAL_VERSION} and the {PLUGIN_ID} marker "
          f"written; {len(central_modules())} modules redirected, {len(targets) - len(central_modules())} third-party "
          "versions raised")
    return True


ZIP = DIST / f"lts-for-spring-{BOM_VERSION}.zip"


def zip_release():
    """Zip dist/repository as a Maven repository, with MD5 and SHA-1 files: every module whose upstream version
    is on Maven Central, without javadoc, which upstream publishes, and without Maven metadata."""
    built = rebuilt()
    with ThreadPoolExecutor(os.cpu_count() or 2) as pool:
        there = pool.map(lambda ga: toolchain.exists(*ga, built[ga].rsplit("-ltslab.", 1)[0]), built)
        keep = {ga for ga, ok in zip(built, there, strict=True) if ok}
    skipped = ("-javadoc.jar", ".md5", ".sha1", ".sha256", ".sha512")
    files = sorted(f for (g, a) in keep for f in (UPSTREAM / g.replace(".", "/") / a / built[(g, a)]).iterdir()
                   if f.name.startswith(f"{a}-") and not f.name.endswith(skipped))
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as out:
        for f in files:
            name, data = f"repository/{f.relative_to(UPSTREAM).as_posix()}", f.read_bytes()
            out.writestr(name, data)
            out.writestr(f"{name}.md5", hashlib.md5(data).hexdigest())
            out.writestr(f"{name}.sha1", hashlib.sha1(data).hexdigest())
    print(f"{len(keep)} modules ({len(built) - len(keep)} not on Maven Central upstream left out), {len(files)} files, "
          f"{ZIP.stat().st_size / 1e6:.1f} MB in {ZIP.relative_to(ROOT)}")
    return True


def zip_samples():
    """Resolve samples/maven against dist/repository; every rebuilt module must resolve to its -ltslab.N version,
    every project must be on the classpath, and no raised third-party artifact may resolve lower."""
    out = WORK / "zip-samples"
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(ROOT / "samples" / "maven" / "target", ignore_errors=True)
    settings = out / "settings.xml"
    settings.write_text(f"""<settings><profiles><profile><id>train</id>
<repositories><repository><id>train</id><url>{UPSTREAM.as_uri()}</url></repository></repositories>
</profile></profiles><activeProfiles><activeProfile>train</activeProfile></activeProfiles></settings>""")
    log = out / "release.log"
    log.write_text("")
    run(["mvn", "-q", "-B", "-s", str(settings), "-f", str(ROOT / "samples" / "maven" / "pom.xml"),
         "dependency:list", f"-DoutputFile={out / 'maven.txt'}", "compile"], ROOT, log)
    built, raised = rebuilt(), raised_artifacts()
    projects = {(p["publish"]["group"], m): name for name, p in V1.items() for m in p["publish"]["modules"]}
    found = re.findall(r"^\s+([^:\s]+):([^:]+):[^:]+:([^:]+):", (out / "maven.txt").read_text(), re.M)
    stale = sorted(f"{g}:{a}:{v} (not {built[(g, a)]})" for g, a, v in found if (g, a) in built and v != built[(g, a)])
    low = sorted(f"{g}:{a}:{v} (< {raised[f'{g}:{a}']})" for g, a, v in found
                 if f"{g}:{a}" in raised and toolchain.key(v) < toolchain.key(raised[f"{g}:{a}"]))
    missing = sorted(set(V1) - {projects[(g, a)] for g, a, _ in found if (g, a) in projects})
    print(f"maven sample: {len(found)} modules, {len([1 for g, a, _ in found if (g, a) in built])} rebuilt, "
          f"{len(stale)} not rebuilt, {len([1 for g, a, _ in found if f'{g}:{a}' in raised])} raised third-party, "
          f"{len(low)} below, projects without a patched module: {missing or 'none'}")
    for line in stale + low:
        print(f"  {line}")
    return not stale and not low and not missing


CENTRAL = "https://central.sonatype.com"
SAMPLE_REPOS = {"local": REPO.as_uri(), "staging": f"{CENTRAL}/api/v1/publisher/deployments/download",
                "central": "https://repo1.maven.org/maven2"}
# The staging repository needs the token; Maven reads it from the environment, so it is never written to a file.
STAGING_AUTH = ("<servers><server><id>train</id><configuration><httpHeaders><property><name>Authorization</name>"
                "<value>${env.LTSLAB_CENTRAL_AUTH}</value></property></httpHeaders></configuration></server></servers>")


@functools.cache
def unpublished():
    """The republished modules that Maven Central does not have yet: (upstream group, artifact ID) -> version."""
    with ThreadPoolExecutor(os.cpu_count() or 2) as pool:
        there = list(pool.map(lambda item: toolchain.exists(GROUP, item[0][1], item[1]), central_modules().items()))
    return {ga: v for (ga, v), ok in zip(central_modules().items(), there, strict=True) if not ok}


def central_samples(where="local"):
    """Resolve every republished module under GROUP, next to the published BOM, as README's Maven Central
    section shows; each must resolve to its version, and none may pull its upstream twin."""
    url, env = SAMPLE_REPOS[where], None
    if where == "staging":
        env = {**os.environ, "LTSLAB_CENTRAL_AUTH": central_auth()}
    out = WORK / "central-sample"
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(Path.home() / ".m2" / "repository" / GROUP.replace(".", "/"), ignore_errors=True)  # stale copies
    dependencies = "".join(f"<dependency><groupId>{GROUP}</groupId><artifactId>{a}</artifactId><version>{v}</version>"
                           "</dependency>" for (_, a), v in sorted(central_modules().items()))
    (out / "pom.xml").write_text(f"""<project xmlns="{POM}"><modelVersion>4.0.0</modelVersion>
<groupId>sample</groupId><artifactId>central-sample</artifactId><version>1</version>
<dependencyManagement><dependencies><dependency><groupId>{GROUP}</groupId><artifactId>spring-boot-dependencies</artifactId>
<version>{CENTRAL_VERSION}</version><type>pom</type><scope>import</scope></dependency>
</dependencies></dependencyManagement><dependencies>{dependencies}</dependencies></project>""")
    settings = out / "settings.xml"  # isolates the run from the user's settings and credentials
    settings.write_text(f"""<settings>{STAGING_AUTH if env else ""}<profiles><profile><id>train</id>
<repositories><repository><id>train</id><url>{url}</url></repository></repositories>
</profile></profiles><activeProfiles><activeProfile>train</activeProfile></activeProfiles></settings>""")
    log = out / "release.log"
    log.write_text("")
    run(["mvn", "-q", "-B", "-s", str(settings), "-f", str(out / "pom.xml"), "dependency:list",
         f"-DoutputFile={out / 'maven.txt'}"], ROOT, log, env)
    found = re.findall(r"^\s+([^:\s]+):([^:]+):[^:]+:([^:]+):", (out / "maven.txt").read_text(), re.M)
    ours = {(a, v) for g, a, v in found if g == GROUP}
    wrong = sorted(f"{GROUP}:{a}:{v} missing" for (_, a), v in central_modules().items() if (a, v) not in ours)
    twins = sorted(f"{g}:{a}:{v}" for g, a, v in found if (g, a) in central_modules())
    # A module that only the zip republishes keeps its upstream coordinates in our POMs, so a transitive
    # dependency can still bring the upstream copy; README's Maven Central section has the exclusions.
    print(f"central sample ({where}): {len(found)} modules, {len(ours)} from {GROUP}, "
          f"{len(twins)} upstream twins to exclude")
    for line in wrong + twins:
        print(f"  {line}")
    return not wrong


@functools.cache
def raised_artifacts():
    """group:artifact -> version of each third-party artifact that the BOM raises; the republished modules are
    not third-party."""
    boot, ours = toolchain.managed({}), toolchain.managed(DEPENDENCIES["properties"])
    return {f"{g}:{a}": ours[(g, a)][0] for (g, a), (v, _) in sorted(boot.items())
            if (g, a) in ours and ours[(g, a)][0] != v and (g, a) not in central_modules()}


def placeholder_javadoc(artifact):
    """A javadoc jar with only a README: sources jars carry the documentation, and Central allows this."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as jar:
        jar.writestr(zipfile.ZipInfo("README.txt", (1980, 1, 1, 0, 0, 0)),
                     f"Javadoc for {artifact} is not republished by LTS for Spring.\n"
                     "Use the sources jar, or the upstream API documentation for the same version.\n")
    return buffer.getvalue()


def bundle():
    """Copy the jar, sources, POM, and a placeholder javadoc of each module that Maven Central does not have yet,
    plus the BOM, the extension, the plugin, and the plugin marker, with MD5 and SHA-1 files, into dist/bundle."""
    out = DIST / "bundle"
    shutil.rmtree(out, ignore_errors=True)
    files = 0
    train = [(GROUP, a, v) for (_, a), v in unpublished().items()]
    train += [(GROUP, a, CENTRAL_VERSION) for a in ["spring-boot-dependencies"] + COMPAT]
    train += [(PLUGIN_ID, f"{PLUGIN_ID}.gradle.plugin", CENTRAL_VERSION)]
    for group, artifact, ver in sorted(train):
        src = REPO / group.replace(".", "/") / artifact / ver
        primary = [f for f in src.glob(f"{artifact}-{ver}*")
                   if f.suffix in (".jar", ".pom") and "-javadoc" not in f.name]
        if not any(f.suffix == ".pom" for f in primary):
            raise SystemExit(f"error: {artifact}:{ver} is not built; run scripts/release.py central-build, "
                             "bom, and compat")
        dest = out / src.relative_to(REPO)
        dest.mkdir(parents=True)
        pom = ET.parse(next(f for f in primary if f.suffix == ".pom")).getroot()
        missing = [r for r in REQUIRED if pom.find("/".join(f"{{{POM}}}{t}" for t in r.split("/"))) is None]
        if missing:
            raise SystemExit(f"error: {artifact}:{ver} lacks {missing} in its POM, which Maven Central requires")
        coordinates = tuple(pom.findtext(f"{{{POM}}}{t}") for t in ("groupId", "artifactId", "version"))
        if coordinates != (group, artifact, ver):  # Central matches the files against these
            raise SystemExit(f"error: {artifact}: the POM says {':'.join(str(c) for c in coordinates)}, "
                             f"not {group}:{artifact}:{ver}")
        contents = {f.name: f.read_bytes() for f in primary}
        if any(name == f"{artifact}-{ver}.jar" for name in contents):
            contents[f"{artifact}-{ver}-javadoc.jar"] = placeholder_javadoc(artifact)
        for name, data in contents.items():
            (dest / name).write_bytes(data)
            (dest / f"{name}.md5").write_text(hashlib.md5(data).hexdigest())
            (dest / f"{name}.sha1").write_text(hashlib.sha1(data).hexdigest())
            files += 1
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    javadoc = sum(f.stat().st_size for f in out.rglob("*-javadoc.jar"))
    print(f"{len(train)} artifacts, {files} files, {files * 4} with checksums and signatures, "
          f"{size / 1e6:.1f} MB ({javadoc / 1e6:.1f} MB javadoc); unsigned in {out.relative_to(ROOT)}")
    return True


SIGNING_KEY = "AF610AB69E410A04C9B5644ADACC0A4FFFCA8F8A"  # ltslab <ltslab@yukkes.com>


def sign(key=SIGNING_KEY):
    """Write a verified, detached .asc for the release zip and for each file in dist/bundle except checksums,
    and zip the bundle for the Central Portal."""
    out = DIST / "bundle"
    if not ZIP.is_file():
        raise SystemExit(f"error: {ZIP.relative_to(ROOT)} is missing; run scripts/release.py zip")
    files = [ZIP] + sorted(f for f in out.rglob("*") if f.is_file() and f.suffix not in (".md5", ".sha1", ".asc"))
    # gpg-agent asks for the passphrase once, through pinentry on this terminal, and caches it for the rest.
    env = {**os.environ, "GPG_TTY": os.ttyname(0)} if sys.stdin.isatty() else None
    for f in files:
        subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--local-user", key, "--armor", "--detach-sign", f],
                       check=True, env=env)
        subprocess.run(["gpg", "--batch", "--quiet", "--verify", f"{f}.asc", f], check=True, capture_output=True)
    print(f"{len(files)} signed")
    if len(files) == 1:
        return True
    archive = DIST / f"central-bundle-{BOM_VERSION}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for f in sorted(f for f in out.rglob("*") if f.is_file()):
            zip_file.write(f, f.relative_to(out).as_posix())
    with zipfile.ZipFile(archive) as zip_file:
        count = len(zip_file.namelist())
    print(f"{count} files, {archive.stat().st_size / 1e6:.1f} MB in {archive.relative_to(ROOT)}")
    return True


def central_auth():
    """The Central Portal's Authorization header value. Never print it."""
    user, password = os.environ.get("CENTRAL_USERNAME"), os.environ.get("CENTRAL_PASSWORD")
    settings = Path.home() / ".m2" / "settings.xml"
    if not (user and password) and settings.is_file():
        for server in ET.parse(settings).getroot().iter():
            fields = {child.tag.split("}")[-1]: child.text or "" for child in server}
            if server.tag.split("}")[-1] == "server" and fields.get("id") == "central":
                user, password = fields.get("username"), fields.get("password")
    if not (user and password):
        raise SystemExit("error: no Central token; set CENTRAL_USERNAME and CENTRAL_PASSWORD, or add a `central` "
                         "server to ~/.m2/settings.xml")
    return "Bearer " + base64.b64encode(f"{user}:{password}".encode()).decode()


def central(method, path, data=None, content_type=None):
    headers = {"Authorization": central_auth(), **({"Content-Type": content_type} if content_type else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(CENTRAL + path, data, headers, method=method),
                                    timeout=600) as response:
            return response.read().decode()
    except urllib.error.HTTPError as ex:
        raise SystemExit(f"error: {method} {path.split('?')[0]}: HTTP {ex.code} "
                         f"{ex.read().decode(errors='replace')[:1000]}") from None


def wait(deployment, goal):
    """Poll the deployment until it reaches goal or fails; print Central's errors."""
    for _ in range(360):  # an hour
        status = json.loads(central("POST", f"/api/v1/publisher/status?id={deployment}"))
        state = status["deploymentState"]
        if state in (goal, "FAILED"):
            for component, errors in (status.get("errors") or {}).items():
                print(f"  {component}: {errors}")
            print(f"deployment {deployment}: {state}")
            return state == goal
        time.sleep(10)
    raise SystemExit(f"error: deployment {deployment} is still {state}")


def upload():
    """Upload the signed zip with USER_MANAGED, which never publishes on its own, and wait for validation."""
    archive = DIST / f"central-bundle-{BOM_VERSION}.zip"
    if not archive.is_file():
        raise SystemExit(f"error: {archive.relative_to(ROOT)} is missing; run scripts/release.py sign")
    boundary = uuid.uuid4().hex
    body = b"".join([f'--{boundary}\r\nContent-Disposition: form-data; name="bundle"; filename="{archive.name}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode(), archive.read_bytes(),
                     f"\r\n--{boundary}--\r\n".encode()])
    upload = f"/api/v1/publisher/upload?publishingType=USER_MANAGED&name=lts-for-spring-{BOM_VERSION}"
    deployment = central("POST", upload, body, f"multipart/form-data; boundary={boundary}").strip()
    (DIST / "deployment.txt").write_text(f"{deployment}\n")
    print(f"uploaded {archive.name} as deployment {deployment}")
    return wait(deployment, "VALIDATED")


def notes(deployment=None):
    """The GitHub release notes: how to use the zip, the CVEs that the train fixes, and, with a deployment,
    the modules that it adds to Maven Central."""
    cloud = ltslab(MANIFEST["cloud"])
    upgrades = DEPENDENCIES["upgrades"]
    rows = [f"| [{i}]({c['advisory']}) | {c['project']} | {c['severity']} | {c['title'].replace('|', '/')} |"
            for i, c in verify.CVES.items() if c["project"] in V1 and c["status"] in ("verified", "released")]
    rebuilt = [name for name in V1 if rebuilds(name)]
    lines = [
        f"LTS for Spring {BOM_VERSION}: security fixes for Spring Boot {MANIFEST['boot']} and Spring Cloud "
        f"{MANIFEST['cloud']}.", "",
        f"`{ZIP.name}` is a Maven repository with Spring's own group and artifact IDs and `-ltslab` versions. "
        f"Put it in your local Maven repository or a private one, and change two versions "
        f"([README]({URL}#install)):", "",
        f"- `org.springframework.boot:spring-boot-starter-parent` or `spring-boot-dependencies`: `{BOM_VERSION}`",
        f"- `org.springframework.cloud:spring-cloud-dependencies`: `{cloud}`", "",
        *([f"This release rebuilds the {len(rebuilt)} projects whose last release's class files did not match "
           "Spring's own build; their source and security fixes are unchanged, and every other project is unchanged "
           "from the last release.", ""] if rebuilt else []),
        "| Project | Version |", "|---|---|",
        *[f"| [{name}]({p['upstream']}) | `{version(name)}` |" for name, p in V1.items()], "",
        f"It fixes {len(rows)} CVEs in Spring and Reactor:", "",
        "| CVE | Project | Severity | Title |", "|---|---|---|---|",
        *rows, "",
        f"It raises {len(upgrades)} third-party libraries from the versions in Spring Boot {MANIFEST['boot']}, "
        f"which fixes {len({c['id'] for c in DEPENDENCIES['cves'] if c['fixed']})} more CVEs (see CVES.md)"
        + (". The versions are in the zip's BOM; the BOM under the Maven Central group pins only the modules that "
           "Central republishes" if TRAIN == CENTRAL_TRAIN else "") + ":", "",
        "| Property | From | To | Public API changes |", "|---|---|---|---|",
        *[f"| `{p}` | {u['from']} | {u['to']}{' (newer line)' if u['line'] else ''} | {u['public_changes']} |"
          for p, u in upgrades.items()], "",
        "Public API changes count the binary-incompatible changes that japicmp reports in the artifacts on a Spring "
        "module's classpath. None touches an API that Spring uses, apart from the ones checked by hand in "
        "`manifest.yaml`. Code that calls those classes directly may need a change.", "",
        f"SHA-256 of `{ZIP.name}`: `{hashlib.sha256(ZIP.read_bytes()).hexdigest()}`; `{ZIP.name}.asc` is its "
        "signature by the ltslab key."]
    if TRAIN == CENTRAL_TRAIN and (deployment or toolchain.exists(GROUP, "spring-boot-dependencies", CENTRAL_VERSION)):
        missing = unpublished()
        added = set(missing) if deployment else set()
        central = sorted([(GROUP, a, v, (g, a) in added) for (g, a), v in central_modules().items()
                          if deployment or (g, a) not in missing] +
                         [(GROUP, a, CENTRAL_VERSION, False) for a in ("spring-boot-dependencies", *COMPAT)])
        lines += ["", f"Maven Central has the patched modules under `{GROUP}`, with the BOM, the Maven extension, "
                  f"and the Gradle plugin ([README]({URL}#maven-central)). A module gets a new version there only when "
                  "its patches change, and the BOM pins each module's latest one.", "",
                  "| Artifact | Version |", "|---|---|",
                  *[f"| [{a}](https://central.sonatype.com/artifact/{g}/{a}/{v}) | `{v}`"
                    f"{' (added by this release)' if new else ''} |" for g, a, v, new in central]]
        if deployment:
            lines += ["", f"Maven Central deployment: `{deployment}`"]
    print("\n".join(lines))
    return True


def publish(deployment):
    """Publish a validated deployment; this cannot be undone."""
    state = json.loads(central("POST", f"/api/v1/publisher/status?id={deployment}"))["deploymentState"]
    if state != "VALIDATED":
        raise SystemExit(f"error: deployment {deployment} is {state}, not VALIDATED")
    central("POST", f"/api/v1/publisher/deployment/{deployment}")
    return wait(deployment, "PUBLISHED")


def drop(deployment):
    central("DELETE", f"/api/v1/publisher/deployment/{deployment}")
    print(f"deployment {deployment}: dropped")
    return True


def unreleased(central=False):
    """Stop before a released train is built again, which would change what its version holds: a published GitHub
    release has a tag, and Maven Central keeps every version."""
    tag = subprocess.run(["git", "ls-remote", "--tags", f"{URL}.git", f"refs/tags/{BOM_VERSION}"],
                         capture_output=True, text=True, check=True).stdout
    if tag:
        raise SystemExit(f"error: {BOM_VERSION} is released; raise train in manifest.yaml")
    if central and toolchain.exists(GROUP, "spring-boot-dependencies", CENTRAL_VERSION):
        raise SystemExit(f"error: {GROUP}:*:{CENTRAL_VERSION} is on Maven Central; raise central in manifest.yaml")


def main(args):
    if args[:1] == ["build"]:
        unreleased()
        if not args[1:]:  # a module that an earlier build left would go into the zip
            shutil.rmtree(UPSTREAM, ignore_errors=True)
        return all(build(p) for p in (args[1:] or V1))
    if args[:1] == ["central-build"]:
        unreleased(central=True)
        missing = unpublished()
        projects = args[1:] or [p for p in V1 if any((V1[p]["publish"]["group"], m) in missing
                                                     for m in V1[p]["publish"]["modules"])]
        return all(central_build(p) for p in projects)
    commands = {"boms": boms, "bom": bom, "compat": compat, "samples": zip_samples, "zip": zip_release,
                "classes": classes, "bundle": bundle, "upload": upload}
    if len(args) == 1 and args[0] in commands:
        return commands[args[0]]()
    if args[:1] == ["central-samples"] and len(args) <= 2 and set(args[1:]) <= set(SAMPLE_REPOS):
        return central_samples(*args[1:])
    if args[:1] in (["sign"], ["notes"]) and len(args) <= 2:
        return {"sign": sign, "notes": notes}[args[0]](*args[1:])
    commands = {"publish": publish, "drop": drop}
    if len(args) == 2 and args[0] in commands and re.fullmatch(r"[0-9a-f-]{36}", args[1]):
        return commands[args[0]](args[1])
    print(__doc__.strip(), file=sys.stderr)
    return False


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)
