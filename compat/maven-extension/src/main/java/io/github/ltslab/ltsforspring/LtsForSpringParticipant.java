package io.github.ltslab.ltsforspring;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Properties;
import java.util.Set;

import org.apache.maven.AbstractMavenLifecycleParticipant;
import org.apache.maven.artifact.versioning.ComparableVersion;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Dependency;
import org.apache.maven.project.MavenProject;
import org.eclipse.aether.DefaultRepositorySystemSession;
import org.eclipse.aether.artifact.Artifact;
import org.eclipse.aether.artifact.DefaultArtifact;
import org.eclipse.aether.collection.DependencyGraphTransformationContext;
import org.eclipse.aether.collection.DependencyGraphTransformer;
import org.eclipse.aether.graph.DependencyNode;
import org.eclipse.aether.util.graph.transformer.ChainedDependencyGraphTransformer;

/**
 * Core extension that resolves upstream Spring and Reactor coordinates, direct or transitive, to the
 * LTS for Spring artifacts. It renames nodes before Maven's own conflict resolution, so an upstream
 * and an LTS for Spring copy of the same module collapse into one. It also raises the third-party
 * versions that the project manages, from its parent or an imported BOM, to at least the LTS for
 * Spring versions.
 */
public class LtsForSpringParticipant extends AbstractMavenLifecycleParticipant {

	@Override
	public void afterSessionStart(MavenSession session) {
		DefaultRepositorySystemSession repositorySession = (DefaultRepositorySystemSession) session.getRepositorySession();
		repositorySession.setDependencyGraphTransformer(new ChainedDependencyGraphTransformer(
				new Redirect(load()), repositorySession.getDependencyGraphTransformer()));
	}

	@Override
	public void afterProjectsRead(MavenSession session) {
		Properties coordinates = load();
		for (MavenProject project : session.getProjects()) {
			if (project.getDependencyManagement() != null) {
				project.getDependencyManagement().getDependencies().forEach(dependency -> raise(dependency, coordinates));
			}
			project.getDependencies().forEach(dependency -> raise(dependency, coordinates));
		}
	}

	/** Raises a third-party version, one whose LTS for Spring coordinate keeps its group, but never lowers it. */
	static void raise(Dependency dependency, Properties coordinates) {
		String target = coordinates.getProperty(dependency.getGroupId() + ":" + dependency.getArtifactId());
		if (target == null || dependency.getVersion() == null) {
			return;
		}
		String[] gav = target.split(":");
		if (gav[0].equals(dependency.getGroupId())
				&& new ComparableVersion(dependency.getVersion()).compareTo(new ComparableVersion(gav[2])) < 0) {
			dependency.setVersion(gav[2]);
		}
	}

	static Properties load() {
		Properties coordinates = new Properties();
		try (InputStream in = LtsForSpringParticipant.class.getResourceAsStream("/lts-for-spring.properties")) {
			coordinates.load(in);
		}
		catch (IOException ex) {
			throw new UncheckedIOException(ex);
		}
		return coordinates;
	}

	static final class Redirect implements DependencyGraphTransformer {

		private final Properties coordinates;

		Redirect(Properties coordinates) {
			this.coordinates = coordinates;
		}

		@Override
		public DependencyNode transformGraph(DependencyNode node, DependencyGraphTransformationContext context) {
			redirect(node, Collections.newSetFromMap(new IdentityHashMap<>()));
			return node;
		}

		private void redirect(DependencyNode node, Set<DependencyNode> visited) {
			if (!visited.add(node)) {
				return;  // the graph can contain cycles
			}
			Artifact artifact = node.getArtifact();
			if (artifact != null) {
				String target = this.coordinates.getProperty(artifact.getGroupId() + ":" + artifact.getArtifactId());
				String[] gav = (target != null) ? target.split(":") : null;
				if (gav != null && !gav[0].equals(artifact.getGroupId())) {  // same group: a version, raised by afterProjectsRead
					// Keep the properties (type, build path flags): Maven uses them to build the classpath.
					node.setArtifact(new DefaultArtifact(gav[0], gav[1], artifact.getClassifier(),
							artifact.getExtension(), gav[2], artifact.getProperties(), (File) null));
				}
			}
			for (DependencyNode child : node.getChildren()) {
				redirect(child, visited);
			}
		}

	}

}
