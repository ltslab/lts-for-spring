#!/usr/bin/env python3
"""Choose the third-party versions of the LTS for Spring BOM; CONTRIBUTING.md "Dependency upgrades" has the rules.

  scripts/dependencies.py update   choose and check the versions, and write cves/dependencies.json
  scripts/dependencies.py check    fail if update would change cves/dependencies.json

Requires PyYAML, JDK 21, and access to Maven Central and OSV (api.osv.dev).
"""

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import release
from toolchain import (
    CACHE,
    CENTRAL,
    LOCAL,
    MANIFEST,
    NS,
    ROOT,
    entries,
    fetch,
    get,
    key,
    managed,
    url,
)

POLICY = MANIFEST["bom"]
BOOT = MANIFEST["boot"]
OUT = ROOT / "cves" / "dependencies.json"
JAPICMP = f"{CENTRAL}/com/github/siom79/japicmp/japicmp/0.26.5/japicmp-0.26.5-jar-with-dependencies.jar"
UNSTABLE = re.compile(r"(?i)alpha|beta|-rc|\.rc|rc\d|cr\d|-m\d|\.m\d|milestone|snapshot|preview|-ea")
SPRING = ("org.springframework",)
CORES = os.cpu_count() or 2  # network pools use one worker per core; japicmp JVMs use half, as builds do
BUNDLE = 50_000_000  # bytes; larger jars are distributions, such as Pulsar IO connectors, not libraries


def cached(g, a, v, ext="jar"):
    """The artifact's file in work/, or None when Maven Central has none (a POM-only module) or it is a bundle."""
    local = LOCAL / g.replace(".", "/") / a / v / f"{a}-{v}.{ext}"
    if local.exists() and g != release.GROUP:
        return local
    path = CACHE / "jars" / f"{g}__{a}__{v}.{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            headers = fetch(urllib.request.Request(url(g, a, v, ext), method="HEAD"))[0]
            if int(headers.get("Content-Length") or 0) > BUNDLE:
                return None
            path.write_bytes(get(url(g, a, v, ext)))
        except urllib.error.HTTPError as ex:
            if ex.code != 404:
                raise
            return None
    return path


def published():
    """Each LTS for Spring release for this Spring Boot, oldest first: version -> managed artifacts. A tag is the
    release, and the BOM in the zip is the one it ships. The BOM under the Maven Central group of the same version
    only pins the modules that Central republishes, without the third-party versions, so it stands in only for a
    release whose tag this clone does not have."""
    tags = subprocess.run(["git", "tag", "--list", f"{BOOT}-ltslab.*"], cwd=ROOT, capture_output=True, text=True).stdout
    out = {}
    for tag in set(tags.split()):
        shown = subprocess.run(["git", "show", f"{tag}:cves/dependencies.json"], cwd=ROOT,
                               capture_output=True, text=True)
        out[tag] = managed(json.loads(shown.stdout)["properties"] if shown.returncode == 0 else {})
    try:
        group = release.GROUP.replace(".", "/")
        metadata = get(f"{CENTRAL}/{group}/spring-boot-dependencies/maven-metadata.xml").decode()
    except urllib.error.HTTPError as ex:
        if ex.code != 404:
            raise
        metadata = ""
    for v in re.findall(r"<version>([^<]+)</version>", metadata):
        if v.startswith(f"{BOOT}-ltslab."):
            out.setdefault(v, managed({}, (release.GROUP, "spring-boot-dependencies", v)))
    return dict(sorted(out.items(), key=lambda item: key(item[0])))


def choose():
    """Boot's version property -> the newest release on its line (or on bom.lines), when that differs."""
    root = ET.fromstring(get(url("org.springframework.boot", "spring-boot-dependencies", BOOT)))
    props = {p.tag.replace(NS, ""): (p.text or "").strip() for p in root.findall(f"{NS}properties/*")}
    probes = {}
    for g, a, _, prop, _ in entries("org.springframework.boot", "spring-boot-dependencies", BOOT):
        if prop:
            probes.setdefault(prop, (g, a))
    with ThreadPoolExecutor(CORES) as pool:
        def listing(ga):  # the versions on Maven Central
            return get(f"{CENTRAL}/{ga[0].replace('.', '/')}/{ga[1]}/maven-metadata.xml")
        metadata = dict(zip(probes, pool.map(listing, probes.values()), strict=True))
    chosen = {}
    for prop in sorted(probes):
        current = props[prop]
        line = str(POLICY.get("lines", {}).get(prop) or ".".join(current.split(".")[:2]))
        suffix = re.sub(r"^\d+(\.\d+)*", "", current)  # .Final, .RELEASE, .jre11: stay on the same flavour
        found = re.findall(r"<version>([^<]+)</version>", metadata[prop].decode())
        candidates = [v for v in found if re.fullmatch(rf"{re.escape(line)}(\.\d+)+{re.escape(suffix)}", v)
                      and not UNSTABLE.search(v)]
        newest = max(candidates + [current], key=key)
        if newest != current:
            chosen[prop] = (current, newest)
    return chosen


def class_refs(data):
    """(class, member) pairs that one class file references; member '' for the class itself."""
    count, pos, pool, i = struct.unpack(">H", data[8:10])[0], 10, {}, 1
    while i < count:
        tag = data[pos]
        if tag == 1:
            size = struct.unpack(">H", data[pos + 1:pos + 3])[0]
            pool[i] = data[pos + 3:pos + 3 + size].decode("utf-8", "replace")
            pos += 3 + size
        elif tag in (7, 8, 16, 19, 20):
            pool[i] = (tag, struct.unpack(">H", data[pos + 1:pos + 3])[0])
            pos += 3
        elif tag in (9, 10, 11, 12, 17, 18):
            pool[i] = (tag, *struct.unpack(">HH", data[pos + 1:pos + 5]))
            pos += 5
        else:
            pos += {15: 4, 3: 5, 4: 5, 5: 9, 6: 9}[tag]
            i += tag in (5, 6)  # longs and doubles take two slots
        i += 1
    refs = set()
    for entry in pool.values():
        if isinstance(entry, str):
            refs |= {(c.replace("/", "."), "") for c in re.findall(r"L([\w/$]+);", entry)}
        elif entry[0] == 7:
            refs.add((pool[entry[1]].replace("/", "."), ""))
        elif entry[0] in (9, 10, 11):
            owner, name = pool[pool[entry[1]][1]].replace("/", "."), pool[pool[entry[2]][1]]
            refs |= {(owner, ""), (owner, owner.rsplit(".", 1)[-1] if name == "<init>" else name)}
    return refs


def jar_refs(jar):
    refs = set()
    with zipfile.ZipFile(jar) as z:
        for name in z.namelist():
            if name.endswith(".class"):
                refs |= class_refs(z.read(name))
    return refs


def spring_refs():
    """What the Spring jars of Boot's BOM use: the classes and members they reference, and the managed
    artifacts on their classpath: the dependencies that their POMs declare, and what those need."""
    boot = managed({})
    spring = [(g, a, v) for (g, a), (v, _) in boot.items() if g.startswith(SPRING)]

    def needs(gav, closure):  # every dependency of a Spring POM; compile and runtime ones of the others
        try:
            root = ET.fromstring(get(url(*gav)))
        except urllib.error.HTTPError as ex:
            if ex.code != 404:
                raise
            return set()
        return {(d.findtext(f"{NS}groupId"), d.findtext(f"{NS}artifactId"))
                for d in root.iterfind(f"{NS}dependencies/{NS}dependency")
                if not closure or (d.findtext(f"{NS}scope") in (None, "compile", "runtime")
                                   and d.findtext(f"{NS}optional") != "true")}
    with ThreadPoolExecutor(CORES) as pool:
        jars = [jar for jar in pool.map(lambda gav: cached(*gav), spring) if jar]
        classpath = set().union(*pool.map(lambda gav: needs(gav, False), spring)) & set(boot)
        todo = classpath
        while todo:
            found = set().union(*pool.map(lambda ga: needs((*ga, boot[ga][0]), True), todo)) & set(boot)
            todo = found - classpath
            classpath |= found
    with ProcessPoolExecutor() as pool:  # parsing is CPU-bound
        return set().union(*pool.map(jar_refs, jars, chunksize=8)), classpath


def incompatible(prop, pairs):
    """japicmp's binary-incompatible changes to public API, as 'artifact: TYPE class#member', from one JVM per
    property."""
    with ThreadPoolExecutor(CORES) as pool:
        def both(ga):  # the jars before and after the upgrade
            return cached(*ga, pairs[ga][0]), pairs[ga][1] and cached(*ga, pairs[ga][1])
        jars = dict(zip(pairs, pool.map(both, pairs), strict=True))
    changes = {f"{a}: ARTIFACT_REMOVED {g}:{a}" for (g, a), (before, after) in jars.items() if before and not after}
    both = {ga: files for ga, files in jars.items() if all(files)}
    if not both:
        return sorted(changes)
    owner = {}
    for (_, a), files in both.items():
        for jar in files:
            with zipfile.ZipFile(jar) as z:
                for name in z.namelist():
                    if name.endswith(".class"):
                        owner.setdefault(re.sub(r"^META-INF/versions/\d+/", "", name)[:-6].replace("/", "."), a)
    batches, size = [[]], 0  # one JVM per batch of up to 100 MB of jars, which fits its default heap
    for files in both.values():
        if batches[-1] and size + files[0].stat().st_size > 100_000_000:
            batches.append([])
            size = 0
        batches[-1].append(files)
        size += files[0].stat().st_size
    tool = CACHE / "japicmp.jar"
    if not tool.exists():
        tool.write_bytes(get(JAPICMP))
    reports = []
    for batch in batches:
        names = ";".join(f"{b.name}>{n.name}" for b, n in batch)
        report = CACHE / "japicmp" / f"{prop}__{hashlib.sha256(names.encode()).hexdigest()[:16]}.xml"
        if not report.exists():
            report.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["java", "-jar", str(tool), "-o", ";".join(str(b) for b, _ in batch),
                            "-n", ";".join(str(n) for _, n in batch), "-b", "-a", "public",
                            "--ignore-missing-classes", "--xml-file", str(report)], capture_output=True, check=True)
        reports.append(report)
    for cls in (c for report in reports for c in ET.parse(report).getroot().iter("class")):
        name = cls.get("fullyQualifiedName")
        for element in cls.iter():
            for change in element.findall("compatibilityChanges/compatibilityChange"):
                if change.get("binaryCompatible") == "false":
                    member = element.get("name") if element is not cls else None
                    changes.add(f"{owner.get(name, '?')}: {change.get('type')} {name}{'#' + member if member else ''}")
    return sorted(changes)


def check_upgrade(prop, old, new, spring):
    """The changes, and the reasons that block the upgrade (empty when it may go ahead)."""
    refs, declared = spring
    pairs = {}
    for g, a, _, own, imported in entries("org.springframework.boot", "spring-boot-dependencies", BOOT):
        if own != prop:
            continue
        if imported:
            before = {(eg, ea): ev for eg, ea, ev, _, imp in entries(g, a, old) if not imp and "${" not in ev}
            after = {(eg, ea): ev for eg, ea, ev, _, imp in entries(g, a, new) if not imp and "${" not in ev}
            pairs.update({ga: (before[ga], after.get(ga)) for ga in before})
        else:
            pairs[(g, a)] = (old, new)
    if prop not in POLICY.get("lines", {}):
        # A patch release can break Spring only through an artifact on a Spring module's classpath.
        pairs = {ga: versions for ga, versions in pairs.items() if ga in declared}
    changes = incompatible(prop, pairs)
    reviewed = POLICY.get("reviewed", {})
    if prop in POLICY.get("lines", {}):  # a newer line: nothing public may break, test fixtures aside
        blocking = [c for c in changes if not re.match(r"[\w.-]+-tests?: ", c)]
    else:  # a patch release: nothing that Spring uses may break
        blocking = []
        for c in changes:
            kind, target = c.split(": ", 1)[1].split(" ", 1)
            cls, _, member = target.partition("#")
            if ((cls, member) in refs or (cls, "") in refs and not member) and c.split(": ", 1)[1] not in reviewed:
                blocking.append(c)
    return changes, blocking


def osv(packages):
    """OSV advisories for each (group, artifact, version)."""
    found = {}
    for i in range(0, len(packages), 500):
        batch = packages[i:i + 500]
        body = json.dumps({"queries": [{"package": {"ecosystem": "Maven", "name": f"{g}:{a}"}, "version": v}
                                       for g, a, v in batch]}).encode()
        request = urllib.request.Request("https://api.osv.dev/v1/querybatch", body,
                                         {"Content-Type": "application/json"})
        for package, result in zip(batch, json.loads(fetch(request)[1])["results"], strict=True):
            found[package] = [v["id"] for v in result.get("vulns", [])]
    return found


def update():
    """Choose each version property's newest release on its line, stop if one breaks an API that Spring uses,
    and return the upgrades and the third-party CVEs that they fix or leave, as cves/dependencies.json has them."""
    chosen = choose()
    print(f"{len(chosen)} version properties have a newer release on their line", flush=True)
    spring = spring_refs()
    with ThreadPoolExecutor(max(1, CORES // 2)) as pool:
        checked = dict(zip(chosen, pool.map(lambda prop: check_upgrade(prop, *chosen[prop], spring), chosen),
                           strict=True))
    upgrades, blocked = {}, []
    for prop, (old, new) in chosen.items():
        changes, blocking = checked[prop]
        if blocking:
            blocked.append(f"{prop} {old} -> {new}:\n    " + "\n    ".join(blocking[:20]))
        upgrades[prop] = {"from": old, "to": new, "line": prop in POLICY.get("lines", {}),
                          "public_changes": len(changes)}
        print(f"  {prop}: {old} -> {new}, {len(changes)} public changes, {len(blocking)} blocking", flush=True)
    if blocked:
        raise SystemExit("error: these upgrades change APIs that they must not; add the change to bom.reviewed "
                         "after checking it, or take the property out of bom.lines:\n  " + "\n  ".join(blocked))

    boot, ours = managed({}), managed({p: u["to"] for p, u in upgrades.items()})
    releases = published()
    republished = set(release.MODULES)
    packages = sorted({(g, a, v) for bom in [boot, ours, *releases.values()] for (g, a), (v, _) in bom.items()
                       if (g, a) not in republished})
    advisories = osv(packages)
    ids = sorted({vid for (g, a), (v, _) in boot.items() for vid in advisories.get((g, a, v), [])})
    with ThreadPoolExecutor(CORES) as pool:
        details = dict(zip(ids, pool.map(lambda vid: json.loads(get(f"https://api.osv.dev/v1/vulns/{vid}")), ids),
                           strict=True))
    cves, undecided = [], []
    for (g, a), (v, prop) in sorted(boot.items()):
        if (g, a) in republished:
            continue
        now = ours.get((g, a), (v, prop))[0]
        left = set(advisories.get((g, a, now), []))
        for vid in advisories.get((g, a, v), []):
            info = details[vid]
            cve = next((x for x in info.get("aliases", []) if x.startswith("CVE-")), vid)
            note = ""
            if vid in left:
                fixed = sorted({e["fixed"] for affected in info.get("affected", [])
                                if affected.get("package", {}).get("name") == f"{g}:{a}"
                                for r in affected.get("ranges", []) for e in r.get("events", []) if "fixed" in e},
                               key=key)
                note = POLICY.get("rejected", {}).get(prop) or ("no fixed release" if not fixed else "")
                if not note:
                    undecided.append(f"{g}:{a} {now}: {cve}, fixed in {', '.join(fixed)} ({prop})")
            # The first published release whose version of the artifact no longer has the advisory.
            released = next((r for r, bom in releases.items() if (g, a) in bom
                             and vid not in advisories.get((g, a, bom[(g, a)][0]), [])), None)
            cves.append({"id": cve, "advisory": f"https://osv.dev/vulnerability/{vid}", "artifact": f"{g}:{a}",
                         "severity": (info.get("database_specific") or {}).get("severity") or "",
                         "boot": v, "ltslab": now, "fixed": vid not in left, "released": released, "note": note})
    if undecided:
        raise SystemExit("error: these CVEs are fixed only on a newer line; add the property to bom.lines or "
                         "bom.rejected:\n  " + "\n  ".join(undecided))
    cves.sort(key=lambda c: (c["artifact"], c["id"]))
    return {"boot": BOOT, "properties": {p: u["to"] for p, u in upgrades.items()}, "upgrades": upgrades, "cves": cves}


def main(args):
    if args == ["update"]:
        OUT.write_text(json.dumps(update(), indent=1) + "\n")
        print(f"wrote {OUT.relative_to(ROOT)}; run scripts/verify.py index")
        return True
    if args == ["check"]:
        current = json.loads(OUT.read_text())
        fresh = update()
        if fresh != current:
            print("cves/dependencies.json is out of date: newer releases or advisories exist; run "
                  "scripts/dependencies.py update", file=sys.stderr)
            return False
        print("cves/dependencies.json is current")
        return True
    print(__doc__.strip(), file=sys.stderr)
    return False


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) else 1)
