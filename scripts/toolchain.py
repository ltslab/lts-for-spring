"""Maven Central, Maven, and the JDKs, for the other scripts; it imports none of them.

Requires PyYAML, Maven 3.9+, and access to Maven Central.
"""

import functools
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"
MANIFEST = yaml.safe_load((ROOT / "manifest.yaml").read_text())
CENTRAL = "https://repo1.maven.org/maven2"
CACHE = WORK / "dependencies"
# Read first, never written: Maven owns it. Not for our own group, which the samples install from dist/repo.
LOCAL = Path.home() / ".m2" / "repository"
OWN = MANIFEST["group"].replace(".", "/") + "/"
NS = "{http://maven.apache.org/POM/4.0.0}"
# Maven, javac, and javap run briefly, so the C1 compiler alone runs them on half the CPU that C2 needs. The tests
# keep the JVMs that their builds give them: Surefire starts them without MAVEN_OPTS.
TOOL_JVM = "-XX:TieredStopAtLevel=1"
os.environ["MAVEN_OPTS"] = f"{os.environ.get('MAVEN_OPTS', '')} {TOOL_JVM}".strip()


def fetch(request):
    """(headers, body) of a request; Maven Central and OSV may answer 429 Too Many Requests, so retry."""
    for attempt in range(6):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.headers, response.read()
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 502, 503, 504) or attempt == 5:
                raise
            time.sleep(int(ex.headers.get("Retry-After") or 0) or 2 ** attempt)


def get(url):
    """The body at url; released POMs never change, so they come from ~/.m2 or the cache when they can."""
    cache = CACHE / "poms" / url.rsplit("/", 1)[-1] if url.startswith(CENTRAL) and url.endswith(".pom") else None
    local = LOCAL / url[len(CENTRAL) + 1:] if cache and not url[len(CENTRAL) + 1:].startswith(OWN) else None
    if local and local.exists():
        return local.read_bytes()
    if cache and cache.exists():
        return cache.read_bytes()
    body = fetch(url)[1]
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
    return body


def url(g, a, v, ext="pom"):
    return f"{CENTRAL}/{g.replace('.', '/')}/{a}/{v}/{a}-{v}.{ext}"


def key(v):
    """A version's sort key: numbers numerically, other parts as text."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-_]", v)]


def exists(g, a, v):
    """Whether Maven Central has g:a:v."""
    try:
        get(url(g, a, v))
        return True
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return False
        raise


def entries(g, a, v, overrides=None):
    """(group, artifact, version, property, is_import) for each managed dependency of a BOM."""
    root = ET.fromstring(get(url(g, a, v)))
    props = {p.tag.replace(NS, ""): (p.text or "").strip() for p in root.findall(f"{NS}properties/*")}
    props.update(overrides or {}, **{"project.version": v})
    out = []
    for d in root.iterfind(f"{NS}dependencyManagement/{NS}dependencies/{NS}dependency"):
        if d.findtext(f"{NS}classifier"):
            continue
        raw = (d.findtext(f"{NS}version") or "").strip()
        version = raw
        for _ in range(3):
            version = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), version)
        prop = re.fullmatch(r"\$\{([^}]+)\}", raw)
        out.append((d.findtext(f"{NS}groupId"), d.findtext(f"{NS}artifactId"), version, prop and prop.group(1),
                    d.findtext(f"{NS}scope") == "import"))
    return out


def managed(overrides, bom=("org.springframework.boot", "spring-boot-dependencies", MANIFEST["boot"])):
    """Every artifact that a BOM, Boot's by default, manages through its imported BOMs:
    (group, artifact) -> (version, property)."""
    out = {}

    def walk(g, a, v, prop, extra):
        for dg, da, dv, own, imported in entries(g, a, v, extra):
            if "${" in dv:
                continue
            if imported:
                walk(dg, da, dv, prop or own, None)
            else:
                out.setdefault((dg, da), (dv, prop or own))
    walk(*bom, None, overrides)
    return out


def pom(coordinates, repositories=(), build="", artifact="ltslab"):
    """A POM that depends on each group:artifact:version, in order, from Maven Central and the repositories."""
    deps = "".join(f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version></dependency>"
                   for g, a, v in (c.split(":") for c in coordinates))
    repos = "".join(f"<repository><id>r{i}</id><url>{r}</url></repository>" for i, r in enumerate(repositories))
    return (f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
            f"<groupId>ltslab</groupId><artifactId>{artifact}</artifactId><version>1</version>"
            f"<repositories>{repos}</repositories><dependencies>{deps}</dependencies><build>{build}</build></project>")


def reactor(d, modules, threads=None):
    """Write d/pom.xml, which builds the modules (directories relative to d) together, so that one Maven run, on
    threads threads (one per CPU by default), does the work of many; return the Maven command for it."""
    listed = "".join(f"<module>{m}</module>" for m in modules)
    d.mkdir(parents=True, exist_ok=True)
    (d / "pom.xml").write_text(f'<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
                               f"<groupId>ltslab</groupId><artifactId>reactor</artifactId><version>1</version>"
                               f"<packaging>pom</packaging><modules>{listed}</modules></project>")
    return ["mvn", "-q", "-B", "-T", str(threads or os.cpu_count() or 2), "-f", str(d / "pom.xml")]


def maven(root):
    """The Maven command of the sources at root, to run in root: the project's Maven wrapper, if it has one, runs the
    Maven and the extensions that its build uses."""
    return [str(root / "mvnw")] if (root / "mvnw").is_file() else ["mvn"]


def resolve(d, output, scope="", root=None):
    """The classpath that Maven resolves for d/pom.xml, in a scope (every scope by default), with the Maven of the
    sources at root, or None; Maven writes to output."""
    done = subprocess.run([*(maven(root) if root else ["mvn"]), "-q", "-B", "-f", str(d / "pom.xml"),
                           "dependency:build-classpath",
                           f"-Dmdep.includeScope={scope}", f"-Dmdep.outputFile={d / 'classpath.txt'}"],
                          cwd=root, stdout=output, stderr=subprocess.STDOUT)
    return (d / "classpath.txt").read_text().strip() if done.returncode == 0 else None


@functools.cache
def installed():
    """(major version, version, home) of every installed JDK, newest first within a major: setup-java's
    JAVA_HOME_<N>_X64, the homes in LTSLAB_JDKS (separated like PATH), and /usr/lib/jvm's. A project may need an
    older release of a major version, such as a JDK 17 before a change to javac."""
    homes = [Path(v) for k, v in sorted(os.environ.items()) if re.fullmatch(r"JAVA_HOME_\d+_(X64|ARM64)", k)]
    homes += [Path(h) for h in os.environ.get("LTSLAB_JDKS", "").split(os.pathsep) if h]
    found = {}
    for home in homes + sorted(Path("/usr/lib/jvm").glob("*")):
        if (home / "bin" / "javac").is_file():
            shown = subprocess.run([str(home / "bin" / "javac"), "-version"], capture_output=True, text=True)
            m = re.search(r"javac ((?:1\.)?(\d+)\S*)", shown.stdout + shown.stderr)
            if m:
                found.setdefault(m[1], (int(m[2]), m[1], home))
    return sorted(found.values(), key=lambda j: (j[0], [-int(n) for n in re.findall(r"\d+", j[1])]))


def jdks():
    """Major version -> home of the newest installed JDK of each."""
    found = {}
    for major, _, home in installed():
        found.setdefault(major, home)
    return found


def major(home):
    return next(m for m, _, h in installed() if h == home)


def javac(jdk, target, flags, classpath, sources, out, sourcepath=None):
    """Compile sources into out for class-file release target with the JDK at home jdk; None, or the compiler's output
    on failure. With a sourcepath, javac also compiles the classes that the sources use from it."""
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    release = ["--release", str(target)] if major(jdk) >= 9 else ["-source", f"1.{target}", "-target", f"1.{target}"]
    if sourcepath:
        found = ["-sourcepath", os.pathsep.join(map(str, sourcepath)), "-implicit:class"]
    else:
        found = ["-implicit:none"]
    done = subprocess.run([str(jdk / "bin" / "javac"), f"-J{TOOL_JVM}", *release, *flags, "-encoding", "UTF-8",
                           "-proc:none", *found, "-nowarn", "-cp", classpath, "-d", str(out), *map(str, sources)],
                          capture_output=True, text=True)
    return None if done.returncode == 0 else done.stdout + done.stderr


def classes(out):
    return {f.relative_to(out).as_posix(): f.read_bytes() for f in out.rglob("*.class")}


def constants(jdk, bodies):
    """Class file -> the compile-time constants that other classes may inline: its non-private static final
    fields with a value, as javap shows them. One javap shows every class, each ending with a line "}"."""
    if not bodies:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        files = [Path(tmp) / f"{i}.class" for i in range(len(bodies))]
        for f, body in zip(files, bodies.values(), strict=True):
            f.write_bytes(body)
        shown = subprocess.run([str(jdk / "bin" / "javap"), f"-J{TOOL_JVM}", "-p", "-constants", *map(str, files)],
                               capture_output=True, text=True, check=True).stdout
    blocks = shown.split("\n}\n")[:len(bodies)]
    if len(blocks) != len(bodies):
        raise SystemExit("error: javap did not show every class")
    return {name: sorted(line.strip() for line in block.splitlines() if " static final " in line
                         and " = " in line and not line.strip().startswith("private"))
            for name, block in zip(bodies, blocks, strict=True)}
