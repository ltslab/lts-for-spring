pluginManagement {
    repositories {
        maven(providers.systemProperty("ltslab.repo").get())  // the train under test
        mavenCentral()
        gradlePluginPortal()
    }
}

plugins {
    id("io.github.ltslab.lts-for-spring") version "3.5.16-ltslab.1"
}

dependencyResolutionManagement {
    repositories {
        maven(providers.systemProperty("ltslab.repo").get())
        mavenCentral()
    }
}

rootProject.name = "gradle-sample"
