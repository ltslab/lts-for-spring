package io.github.ltslab.ltsforspring;

import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.util.Properties;

import org.gradle.api.Plugin;
import org.gradle.api.initialization.Settings;

/**
 * Settings plugin that resolves upstream Spring and Reactor coordinates to the LTS for Spring
 * artifacts in every project of the build, and raises third-party versions to at least the LTS for
 * Spring versions.
 */
public class LtsForSpringPlugin implements Plugin<Settings> {

	@Override
	public void apply(Settings settings) {
		Properties coordinates = load();
		settings.getGradle().allprojects(project -> {
			project.getConfigurations().configureEach(configuration ->
					configuration.getResolutionStrategy().dependencySubstitution(substitution ->
							coordinates.forEach((upstream, ltslab) -> {
								if (!sameGroup((String) upstream, (String) ltslab)) {
									substitution.substitute(substitution.module((String) upstream))
											.using(substitution.module((String) ltslab))
											.because("LTS for Spring");
								}
							})));
			// After the build script, so this runs after the version rules of plugins such as
			// io.spring.dependency-management.
			project.afterEvaluate(evaluated -> evaluated.getConfigurations().configureEach(configuration ->
					configuration.getResolutionStrategy().eachDependency(details -> {
						String key = details.getRequested().getGroup() + ":" + details.getRequested().getName();
						String ltslab = coordinates.getProperty(key);
						if (ltslab != null && sameGroup(key, ltslab)) {
							String version = ltslab.split(":")[2];
							String current = details.getTarget().getVersion();
							if (current == null || current.isEmpty() || compare(current, version) < 0) {
								details.useVersion(version);
								details.because("LTS for Spring");
							}
						}
					})));
		});
	}

	static boolean sameGroup(String upstream, String ltslab) {
		return upstream.split(":")[0].equals(ltslab.split(":")[0]);
	}

	/** Compares versions of one line, such as 10.1.55 and 10.1.61 or 4.1.135.Final and 4.1.139.Final. */
	static int compare(String left, String right) {
		String[] a = left.split("[.\\-]");
		String[] b = right.split("[.\\-]");
		for (int i = 0; i < Math.max(a.length, b.length); i++) {
			String x = (i < a.length) ? a[i] : "";
			String y = (i < b.length) ? b[i] : "";
			int order = (x.matches("\\d+") && y.matches("\\d+")) ? Long.compare(Long.parseLong(x), Long.parseLong(y))
					: x.compareTo(y);
			if (order != 0) {
				return order;
			}
		}
		return 0;
	}

	static Properties load() {
		Properties coordinates = new Properties();
		try (InputStream in = LtsForSpringPlugin.class.getResourceAsStream("/lts-for-spring.properties")) {
			coordinates.load(in);
		}
		catch (IOException ex) {
			throw new UncheckedIOException(ex);
		}
		return coordinates;
	}

}
