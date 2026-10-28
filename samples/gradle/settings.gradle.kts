dependencyResolutionManagement {
    repositories {
        maven(providers.systemProperty("ltslab.repo").get())  // the unzipped repository under test
        mavenCentral()
    }
}

rootProject.name = "gradle-sample"
