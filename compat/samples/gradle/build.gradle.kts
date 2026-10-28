plugins {
    java
    id("org.springframework.boot") version "3.5.16"
    id("io.spring.dependency-management") version "1.1.7"
}

// The usual Spring Boot setup manages Spring Boot's versions; the LTS for Spring BOM and plugin replace them.
dependencyManagement {
    imports {
        mavenBom("io.github.ltslab:spring-boot-dependencies:3.5.16-ltslab.1")
    }
}

java {
    toolchain.languageVersion = JavaLanguageVersion.of(17)
}

dependencies {
    implementation("org.springframework.boot:spring-boot-starter-webflux")
    implementation("org.springframework.boot:spring-boot-starter-security")
    implementation("org.springframework.boot:spring-boot-starter-batch")
}

// Prints every resolved runtime module, one group:artifact:version per line, for scripts/release.py.
tasks.register("resolvedModules") {
    val modules = configurations.runtimeClasspath.flatMap { it.incoming.resolutionResult.rootComponent }
    doLast {
        fun visit(component: org.gradle.api.artifacts.result.ResolvedComponentResult, seen: MutableSet<Any>) {
            if (!seen.add(component.id)) return
            (component.moduleVersion)?.let { println("module ${it.group}:${it.name}:${it.version}") }
            component.dependencies.filterIsInstance<org.gradle.api.artifacts.result.ResolvedDependencyResult>()
                .forEach { visit(it.selected, seen) }
        }
        visit(modules.get(), mutableSetOf())
    }
}
